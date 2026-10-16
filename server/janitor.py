"""清理器（spec §3.05.2 临时文件与保留策略）。

这个模块跑在用户的**生产数据盘**上，删错一次就是丢产物。所以「删什么」的判定
被拆成三道互相独立的闸门，任何一道不过就不删：

1. **形态闸** 目录/文件名必须匹配 ``^\\d+``——task_id 是纯数字（guards.py 的
   ``_check_task_id`` 要求 ``isdigit()``），用户手动放进去的 ``备份/``、``notes/``
   永远匹配不上。
2. **位置闸** 候选路径与数据根都做 ``resolve()`` 再用 guards.py 的 ``_within``
   判定。resolve 会展开 8.3 短名、跟到 junction/symlink 的真实目标，
   所以不能用 ``startswith``——那正是 L2 守卫要挡的绕过形态。
3. **同体闸** 判定用的是「严格在 …之下」，数据根本身与各子目录本身永远不是候选。

**位置闸复用 guards.py 的函数而不是抄一份**，两处边界判定必须一致；
抄一份就等于留了一个以后只改一边的分叉。

保留期（spec §3.05.2 的表）：

    work/<task_id>/      7 天      agent 工作区，与 uploads 同步删
    uploads/<task_id>/   7 天      上传原件
    logs/                14 天     单文件上限 64 MB，超出截断并轮转
    packages/*.zip       30 天     磁盘紧张时最旧优先提前回收
    audit_log            30 天     数据库行
    sessions             过期即删  每次启动清

轮转只截断不删，所以它不受「形态闸」约束；所有**删除**动作都要过三道闸。
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import db
import guards

# task_id 是纯数字，所以「以数字开头」既够用又不会误伤用户自建目录。
TASK_DIR_RE = re.compile(r"^\d+")

WORK_KEEP_DAYS = 7
UPLOAD_KEEP_DAYS = 7
LOG_KEEP_DAYS = 14
LOG_MAX_BYTES = 64 * 1024 * 1024
PACKAGE_KEEP_DAYS = 30
AUDIT_KEEP_DAYS = 30

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f"  # 与 db.now_iso / trg_tasks_touch 写入的格式一致


# --- 闸门 --------------------------------------------------------------------------


def _resolve(path: Path):
    """resolve() 但把失败收敛成 None——判不出来就当不可删，不能抛到删除路径上。"""
    try:
        return Path(guards._resolve(path))
    except guards.Violation:
        return None


def _within(cand: Path, base: Path) -> bool:
    """**直接复用 guards.py 的位置判定**，不另写一份更弱的 startswith。"""
    return bool(guards._within(cand, base))


def _eligible(root: Path, base: Path, entry: Path) -> bool:
    """三道闸：形态（^\\d+）、位置（resolve 后在 base 且 base 在 root 内）、同体（不能等于 base）。

    位置闸的语义是「解析后的真实路径必须落在数据根内」——落在根外的条目**完全不处理**，
    连它自己都不摘：留着比动它安全。
    """
    if not TASK_DIR_RE.match(entry.name):
        return False
    real = _resolve(entry)
    base_real = _resolve(base)
    root_real = _resolve(root)
    if real is None or base_real is None or root_real is None:
        return False
    if real == base_real or real == root_real:
        return False
    return _within(real, base_real) and _within(real, root_real)


def _inside(root: Path, base: Path, entry: Path) -> bool:
    """只判位置（用于截断类操作，不删东西）。"""
    real, base_real, root_real = _resolve(entry), _resolve(base), _resolve(root)
    if real is None or base_real is None or root_real is None:
        return False
    if real == base_real or real == root_real:
        return False
    return _within(real, base_real) and _within(real, root_real)


def _remove(path: Path) -> int:
    """删一个已经过闸的条目，返回释放的字节数。失败抛给调用方记账，不中断整轮清扫。"""
    size = _tree_size(path)
    if path.is_symlink():          # 兜底：符号链接/junction 只摘链接本身，绝不顺着递归进去
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return size


def _tree_size(path: Path) -> int:
    total = 0
    try:
        if path.is_dir() and not path.is_symlink():
            for p in path.rglob("*"):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except OSError:
                    continue
        else:
            total = path.stat().st_size
    except OSError:
        return total
    return total


def _iterdir(base: Path):
    """列出目录内容；读不到就当空的，不让一次权限错误中止整轮清扫。"""
    try:
        return sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return []


# --- 时间 --------------------------------------------------------------------------


def _as_now(now) -> datetime:
    if now is None:
        return datetime.now()
    if isinstance(now, str):
        return datetime.fromisoformat(now)
    return now


def _iso(dt: datetime) -> str:
    return dt.strftime(_ISO_FMT)


def _expired(path: Path, cutoff_ts: float) -> bool:
    """按 mtime 判过期。读不到 mtime 一律当作没过期（宁可不删）。"""
    try:
        return path.stat().st_mtime < cutoff_ts
    except OSError:
        return False


def _cutoff(now: datetime, days: int) -> float:
    return (now - timedelta(days=days)).timestamp()


def _free_bytes(root: Path):
    try:
        return shutil.disk_usage(str(root)).free
    except OSError:
        return None


# --- 各区域清扫 --------------------------------------------------------------------


def _sweep_task_dirs(root: Path, sub: str, keep_days: int, key: str, stats: dict,
                     now: datetime, protected: set):
    """work/ 与 uploads/：终态后整目录保留 N 天，到期删掉整个 task_id 目录。

    ``protected`` 里的 task_id 永不删——哪怕它已经跑了 7 天（超长任务不该被
    清掉工作区）。库不可达时 protected 为空，退回纯 mtime 判定。
    """
    base = root / sub
    if not base.is_dir():
        return
    cutoff = _cutoff(now, keep_days)
    for entry in _iterdir(base):
        if not _expired(entry, cutoff) or not _eligible(root, base, entry):
            continue
        if entry.name in protected:
            continue              # 任务还没进终态，工作区不能碰
        try:
            stats["freed_bytes"] += _remove(entry)
            stats[key] += 1
        except OSError as e:
            stats["errors"].append(f"删除 {sub}/{entry.name} 失败：{e}")


def _active_task_ids(stats: dict) -> set:
    """仍在主链上的 task_id（queued/running/packaging）——只读查询，不改别人的表。"""
    try:
        c = db.connect()
    except Exception as e:                       # noqa: BLE001 - 库不可达不该带走文件清扫
        stats["errors"].append(f"数据库不可达：{e}")
        return set()
    try:
        rows = c.execute(
            "SELECT id FROM tasks WHERE status NOT IN ('done','failed')").fetchall()
        return {str(r["id"]) for r in rows}
    except Exception as e:                       # noqa: BLE001
        stats["errors"].append(f"读取任务状态失败：{e}")
        return set()
    finally:
        c.close()


def _cap_log(path: Path) -> bool:
    """日志超过上限：截掉最旧的一段（留下最新的尾部），轮转为 ``<name>.log.1``，
    原位置开一个空文件。返回是否发生了轮转。

    留尾部而不是留头部：日志越新的越有排查价值。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= LOG_MAX_BYTES:
        return False
    keep = max(LOG_MAX_BYTES // 2, 1)
    with open(path, "rb") as src:
        src.seek(size - keep, os.SEEK_SET)
        tail = src.read()
    rotated = path.with_name(path.name + ".1")
    if rotated.exists():
        rotated.unlink()          # 只留一代轮转，不无限叠 .1 .2 .3
    rotated.write_bytes(tail)
    path.write_bytes(b"")
    return True


def _sweep_logs(root: Path, stats: dict, now: datetime):
    base = root / "logs"
    if not base.is_dir():
        return
    cutoff = _cutoff(now, LOG_KEEP_DAYS)
    for entry in _iterdir(base):
        name = entry.name
        is_log = name.endswith(".log")
        if not (is_log or name.endswith(".log.1")):
            continue          # 管理员自己放的说明之类一律不碰
        if not _inside(root, base, entry):
            continue
        if _expired(entry, cutoff):
            if not _eligible(root, base, entry):
                continue      # 删除仍要过形态闸
            try:
                stats["freed_bytes"] += _remove(entry)
                stats["log_files"] += 1
            except OSError as e:
                stats["errors"].append(f"删除 logs/{name} 失败：{e}")
            continue
        if is_log:
            try:
                if _cap_log(entry):
                    stats["logs_rotated"] += 1
            except OSError as e:
                stats["errors"].append(f"轮转 logs/{name} 失败：{e}")


def _reclaim_packages(root: Path, base: Path, stats: dict, target_free_bytes: int):
    """磁盘紧张时按**最旧优先**提前回收产物包，够用就立刻停手。"""
    free = _free_bytes(root)
    if free is None or free >= target_free_bytes:
        return
    entries = []
    for entry in _iterdir(base):
        if not entry.name.endswith(".zip") or not _eligible(root, base, entry):
            continue
        try:
            entries.append((entry.stat().st_mtime, entry))
        except OSError:
            continue
    for _, entry in sorted(entries, key=lambda t: t[0]):   # 最旧优先
        if free >= target_free_bytes:
            break
        try:
            freed = _remove(entry)
        except OSError as e:
            stats["errors"].append(f"回收 packages/{entry.name} 失败：{e}")
            continue
        stats["packages"] += 1
        stats["freed_bytes"] += freed
        free += freed


def _sweep_packages(root: Path, stats: dict, now: datetime, target_free_bytes):
    base = root / "packages"
    if not base.is_dir():
        return
    cutoff = _cutoff(now, PACKAGE_KEEP_DAYS)
    for entry in _iterdir(base):
        if not entry.name.endswith(".zip"):
            continue
        if not _expired(entry, cutoff) or not _eligible(root, base, entry):
            continue
        try:
            stats["freed_bytes"] += _remove(entry)
            stats["packages"] += 1
        except OSError as e:
            stats["errors"].append(f"删除 packages/{entry.name} 失败：{e}")
    if target_free_bytes is not None:
        _reclaim_packages(root, base, stats, target_free_bytes)


def _sweep_db(stats: dict, now: datetime):
    """audit_log 保留 30 天；sessions 每次启动清过期行。"""
    try:
        c = db.connect()
    except Exception as e:                       # noqa: BLE001 - 库不可达不该带走文件清扫
        stats["errors"].append(f"数据库不可达：{e}")
        return
    try:
        cur = c.execute("DELETE FROM audit_log WHERE created_at < ?",
                        (_iso(now - timedelta(days=AUDIT_KEEP_DAYS)),))
        stats["audit_rows"] = max(cur.rowcount, 0)
        cur = c.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
        stats["sessions"] = max(cur.rowcount, 0)
        c.commit()
    except Exception as e:                       # noqa: BLE001
        c.rollback()
        stats["errors"].append(f"数据库清理失败：{e}")
    finally:
        c.close()


# --- 入口 --------------------------------------------------------------------------


def _empty_stats(errors=None) -> dict:
    return {
        "work_dirs": 0,
        "upload_dirs": 0,
        "log_files": 0,
        "logs_rotated": 0,
        "packages": 0,
        "audit_rows": 0,
        "sessions": 0,
        "freed_bytes": 0,
        "errors": list(errors or []),
    }


def sweep(data_root=None, now=None, *, target_free_bytes=None) -> dict:
    """扫一轮，返回删除统计。

    data_root 缺省读 ``PROTFORGE_WEB_DATA_ROOT``（测试指 tmp_path，生产指 E 盘）。
    target_free_bytes 非 None 时启用「磁盘紧张 → 产物包最旧优先回收」。

    全程只读磁盘结构、只删本数据根下过三道闸的路径；目录不存在就跳过，**不创建**。
    """
    root = _resolve(Path(data_root) if data_root is not None else db.data_root())
    if root is None:
        return _empty_stats(["数据根无法解析，未执行任何删除"])
    if not root.is_dir():
        return _empty_stats()
    now = _as_now(now)

    stats = _empty_stats()
    protected = _active_task_ids(stats)
    _sweep_task_dirs(root, "work", WORK_KEEP_DAYS, "work_dirs", stats, now, protected)
    _sweep_task_dirs(root, "uploads", UPLOAD_KEEP_DAYS, "upload_dirs", stats, now, protected)
    _sweep_logs(root, stats, now)
    _sweep_packages(root, stats, now, target_free_bytes)
    _sweep_db(stats, now)
    return stats


__all__ = [
    "sweep",
    "LOG_MAX_BYTES",
    "WORK_KEEP_DAYS",
    "UPLOAD_KEEP_DAYS",
    "LOG_KEEP_DAYS",
    "PACKAGE_KEEP_DAYS",
    "AUDIT_KEEP_DAYS",
    "TASK_DIR_RE",
]
