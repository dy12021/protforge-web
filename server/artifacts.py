"""产物发现与打包（spec §8.2 两条路径都跑 / §5.3 打包走子进程 / §3.05.3 峰值）。

三个公开动作：

    discover(task_id, started_at, ended_at) -> list[dict]   找产物
    package(task_id, out_zip=None, progress_cb=None) -> str 打成 zip（子进程）
    verify(task_id, artifact_id, ok=True) -> dict           管理员确认时间窗产物

**发现为什么要两条路径都跑。** agent 完全自主，系统提示要求 job 名带 ``t{id}_``
前缀**不构成强制**。多工具接力时第一个守规矩写了 ``t{id}_rfdiffusion``、第二个没守
规矩写了 ``bindcraft_run``——如果实现成「前缀命中就不扫时间窗」，第二个工具的产物就
**永久丢失**。所以这里是「两条都跑 + 按路径去重」，前缀优先：

    前缀    outputs\\t{task_id}_*        source='prefix'     verified=1  可下载
    时间窗  outputs\\ 下 mtime 落在起止区间 source='timewindow' verified=0  仅可见

时间窗命中项**可见但不可下载**：并发跑时它可能误收别人的产物，所以「先显示、再确认」
比「自动采纳」安全（管理员面板 §18 的确认区）。

mtime 取的是**目录与其全部子孙文件的最大值**，不是目录自身：NTFS 只在增删条目时更新
目录 mtime，一个跑了几小时、目录建于任务开始之前的 job 会被只看目录 mtime 的实现漏掉。

**打包为什么必须 fork。** zip 一个 20 GB 的目录是长时间同步 IO，``zipfile`` 跑在主服务
进程里会拖垮所有请求（FastAPI 的 async 也救不了同步 IO）。所以本文件**同时是打包
worker**：``python artifacts.py --pack <spec.json>`` 独立起一个进程做 zip，通过 stdout
按文件数比例吐 JSON 行回报进度；主服务只读 stdout，全程不持有那个 zip 的文件句柄。
不传 ``progress_cb`` 时进度直接写进 ``events``（kind='package'），前端 SSE 即可推。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import db
import guards
import store

# spec §11：单任务 20 GB 上限。**按「产物 + zip」合计算**（§3.05.3）——打包期间
# 源目录还在，峰值是两份，不是一份。
TASK_QUOTA_BYTES = 20 * 1024 * 1024 * 1024

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f"  # 与 db.now_iso / trg_tasks_touch 一致

# 进度事件最多写这么多条（按百分比去重后天然 ≤101）。产物可有十万文件，
# 每个文件写一条事件会把用户时间线刷成瀑布，还会拖慢 SSE 的游标扫描。
_PROGRESS_STEP = 1  # 百分比跳变阈值


class PackageError(Exception):
    """打包失败。message 恒为中文，直接作为 API 的 detail。"""


class QuotaExceeded(PackageError):
    """按「产物 + zip」合算超了单任务配额，在起子进程之前就拒绝。"""


# ---------------------------------------------------------------- 基础工具


def outputs_root() -> Path:
    """ProtForge 的 outputs 根。复用 guards 的区域判定，环境变量只作覆盖。"""
    return Path(guards.regions(1)["outputs"])


def default_package_path(task_id: int) -> Path:
    return db.data_root() / "packages" / f"task_{int(task_id)}.zip"


def _parse_iso(value) -> float | None:
    """ISO 串 → epoch 秒。本地朴素时间，与 janitor 用同一套 ``.timestamp()`` 换算。

    解析不了返回 None 而不是抛：调用方把 None 当作「这一侧不设限」，比让一条脏
    时间戳把整个产物发现打挂好。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        pass
    for fmt in (_ISO_FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _measure(root: Path) -> tuple[int, float]:
    """目录的总字节数与「最新 mtime」。返回 ``(size, newest_mtime)``。

    mtime 取整棵树的最大值（见模块 docstring）。stat 不到的文件跳过——打包期间
    agent 还在写是常态，不能因为一个文件被删就让整个发现崩掉。
    """
    total = 0
    newest = 0.0
    try:
        newest = float(root.stat().st_mtime)
    except OSError:
        return 0, 0.0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fn in filenames:
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            total += st.st_size
            if st.st_mtime > newest:
                newest = st.st_mtime
    return total, newest


def _set_verified(task_id: int, artifact_id: int, verified: int) -> None:
    """直写 verified。

    ``store.artifacts.add`` 恒写 ``verified=1``（它的语义是「登记即已确认」），
    时间窗产物必须自己降回 0，所以这里绕开 store 直接 UPDATE 一列。
    """
    c = db.connect()
    try:
        with c:
            c.execute("UPDATE artifacts SET verified = ? WHERE id = ? AND task_id = ?",
                      (1 if verified else 0, int(artifact_id), int(task_id)))
    finally:
        c.close()


# ---------------------------------------------------------------- 发现


def _window(task: dict, started_at, ended_at) -> tuple[float, float]:
    """时间窗兜底：不给起点就用 tasks.created_at，不给终点就用现在。

    起止传反了取并集（交换上下界），不抛——宁可多收几个给人确认，也不要静默漏掉。
    """
    lo = _parse_iso(started_at) if started_at else _parse_iso(task.get("created_at"))
    hi = _parse_iso(ended_at) if ended_at else time.time()
    if lo is None:
        lo = float("-inf")
    if hi is None:
        hi = float("inf")
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def discover(task_id: int, started_at=None, ended_at=None) -> list[dict]:
    """扫 outputs 找产物，登记进 artifacts 表，返回**本次发现**的行。

    返回的 dict 来自库（不是内存对象），所以 ``verified`` 是落库后的真值。

    行按目录名排序返回；一个目录同时命中两条路径时**只留 prefix 一行**——否则
    下载列表里同一个目录会显示两次。
    """
    tid = int(task_id)
    task = store.get_task(tid, is_admin=True)  # 不存在 → NotFound（对外是 404 不是 403）
    root = outputs_root()
    lo, hi = _window(task, started_at, ended_at)
    prefix = f"t{tid}_"

    if not root.is_dir():
        return []

    # 读旧值必须在任何写入之前：store.add 每次都把 verified 刷成 1，
    # 管理员已经确认/否决的结论只能靠这份快照保住。
    before = {r["path"]: r for r in store.artifacts.list(tid)}

    found: list[tuple[str, str, str, int, int]] = []  # path, name, source, size, verified
    seen: set[str] = set()

    for d in sorted(root.iterdir(), key=lambda p: p.name):
        # 只认目录：outputs/ 下的散文件是工具的临时残留。
        # 跳过 symlink/junction：跟着它 zip 会把 outputs 之外的东西拖进下载包。
        if d.is_symlink() or not d.is_dir():
            continue
        size, newest = _measure(d)
        is_prefix = d.name.startswith(prefix)
        if not is_prefix and not (lo <= newest <= hi):
            continue

        key = os.path.normcase(str(d))
        if key in seen:
            continue
        seen.add(key)

        source = "prefix" if is_prefix else "timewindow"
        path = str(d)
        aid = store.artifacts.add(tid, path, d.name, kind="dir", size=size,
                                  source=source)
        verified = 1
        if source == "timewindow":
            # store.add 恒写 verified=1，这里把它降回 0；但管理员此前明确确认过的
            # （before 快照里 verified=1）保持 1，不反复要人再点一次。
            prev = before.get(path)
            if not (prev and int(prev["verified"]) == 1):
                verified = 0
                _set_verified(tid, aid, 0)
        found.append((path, d.name, source, size, verified))

    ids = {r["id"] for r in store.artifacts.list(tid) if r["path"] in
           {f[0] for f in found}}
    # 按发现顺序（目录名序）返回，不按 id——两条路径混在一起时顺序对得上目录。
    rows = {r["id"]: r for r in store.artifacts.list(tid)}
    ordered = [r for r in rows.values() if r["id"] in ids]
    ordered.sort(key=lambda r: r["name"])
    return ordered


def downloadable(task_id: int) -> list[dict]:
    """可下载的产物（verified=1）。时间窗产物在管理员确认前不进这里。"""
    return [r for r in store.artifacts.list(int(task_id)) if int(r["verified"]) == 1]


def verify(task_id: int, artifact_id: int, ok: bool = True) -> dict:
    """管理员确认/否决一条时间窗产物。返回更新后的行。

    不写 events：这是管理员动作，记不记时间线由 API 层决定（store.add_event 的
    kind 由调用方语义选，不该在数据层替 API 选）。
    """
    tid = int(task_id)
    aid = int(artifact_id)
    row = store.artifacts.get(tid, aid)  # 不存在 → NotFound
    _set_verified(tid, aid, 1 if ok else 0)
    out = dict(row)
    out["verified"] = 1 if ok else 0
    return out


# ---------------------------------------------------------------- 打包


def preflight(task_id: int, out_zip=None) -> dict:
    """打包前的体积估算。峰值 = 产物 + zip（spec §3.05.3），不是只算产物。

    zip 大小按「与产物等大」估——那是 deflate 在二进制/已压缩数据上的上界，也是
    唯一能保证不超额的估法；目标 zip 已存在（重打包）时按现存大小算。
    """
    tid = int(task_id)
    out = Path(out_zip) if out_zip else default_package_path(tid)
    rows = downloadable(tid)
    artifact_bytes = sum(int(r["size"] or 0) for r in rows)
    try:
        existing = out.stat().st_size
    except OSError:
        existing = 0
    zip_estimate = max(artifact_bytes, existing)
    return {
        "task_id": tid,
        "out_zip": str(out),
        "artifact_count": len(rows),
        "artifact_bytes": artifact_bytes,
        "zip_estimate_bytes": zip_estimate,
        "peak_bytes": artifact_bytes + zip_estimate,
        "quota_bytes": TASK_QUOTA_BYTES,
        "ok": artifact_bytes > 0,
    }


def package(task_id: int, out_zip=None, progress_cb=None,
            quota_bytes: int = TASK_QUOTA_BYTES, timeout: float | None = None) -> str:
    """把已确认的产物打成 zip，返回 zip 路径。**实际压缩在子进程里做。**

    只打 ``verified=1`` 的产物：zip 就是用户下载到的东西，未确认的产物混进去
    等于绕过「先显示、再确认」。不传 out_zip 时落在 ``data/packages/task_<id>.zip``。

    ``progress_cb`` 收 dict：``{event, pid, total, done, skipped, path, message}``，
    event ∈ start / progress / done / log。**不传就自己写进 events**（kind='package'，
    按百分比去重，不会刷屏）。抛 ``PackageError`` / ``QuotaExceeded``，message 恒中文。
    """
    tid = int(task_id)
    rows = downloadable(tid)
    if not rows:
        raise PackageError("没有已确认的产物，无法打包")
    out = Path(out_zip) if out_zip else default_package_path(tid)

    pre = preflight(tid, out)
    if quota_bytes and pre["peak_bytes"] > int(quota_bytes):
        raise QuotaExceeded(
            f"产物与压缩包合计 {pre['peak_bytes']} 字节，超过单任务上限 {int(quota_bytes)} 字节")

    # 过一遍 L2 守卫：产物路径是库里的数据，可能被 agent 写过，越界就不该打进下载包。
    sources = []
    for r in rows:
        try:
            src = guards.resolve(r["path"], tid, write=False)
        except guards.Violation as e:
            raise PackageError(f"产物路径越界，已跳过：{e}") from e
        if not src.is_dir():
            raise PackageError(f"产物目录不存在：{src}")
        sources.append({"src": str(src), "arcname": r["name"]})

    work = db.data_root() / "work" / str(tid)
    work.mkdir(parents=True, exist_ok=True)
    spec_path = work / "pack_spec.json"
    spec_path.write_text(json.dumps({"out_zip": str(out), "sources": sources},
                                    ensure_ascii=False), encoding="utf-8")
    try:
        return _run_pack_worker(tid, spec_path, out, progress_cb, timeout)
    finally:
        try:
            spec_path.unlink()
        except OSError:
            pass


def _emit(task_id: int, payload: dict, progress_cb, last_pct: list[int]) -> None:
    """进度出口：给了回调走回调，没给就写 events（按百分比去重）。"""
    if progress_cb is not None:
        progress_cb(payload)
        return
    kind = payload.get("event")
    if kind == "start":
        store.add_event(task_id, "package", "system",
                        f"开始打包（共 {payload.get('total', 0)} 个文件）")
    elif kind == "progress":
        done, total = int(payload.get("done", 0)), int(payload.get("total", 0))
        pct = int(done * 100 / total) if total else 100
        if pct - last_pct[0] < _PROGRESS_STEP:
            return
        last_pct[0] = pct
        store.add_event(task_id, "package", "system", f"打包中 {pct}%（{done}/{total}）")
    elif kind == "done":
        store.add_event(task_id, "package", "system",
                        f"打包完成（{payload.get('files', 0)} 个文件）")
    elif kind == "log":
        store.add_event(task_id, "package", "system", str(payload.get("message", ""))[:200])


def _run_pack_worker(task_id: int, spec_path: Path, out: Path, progress_cb,
                     timeout: float | None) -> str:
    """起独立 python 进程执行 zip，只读它的 stdout 收进度。"""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--pack", str(spec_path)]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=creationflags,
    )
    last_pct = [-100]
    failure = ""
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                payload = {"event": "log", "message": line}
            if payload.get("event") == "error":
                failure = str(payload.get("message") or "打包子进程报告失败")
                continue
            _emit(task_id, payload, progress_cb, last_pct)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raise PackageError("打包超时，已终止打包进程") from None
    finally:
        if proc.stdout:
            proc.stdout.close()

    stderr = ""
    if proc.stderr:
        stderr = proc.stderr.read() or ""
        proc.stderr.close()
    if rc != 0:
        raise PackageError(failure or f"打包子进程异常退出（退出码 {rc}）"
                           + (f"：{stderr.strip()[:300]}" if stderr.strip() else ""))
    if not out.exists():
        raise PackageError("打包子进程已退出，但没有产出压缩包")
    return str(out)


def _kill_tree(proc) -> None:
    """Windows 上杀进程树，否则孙进程会变孤儿继续占着文件句柄。"""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, text=True, errors="replace")
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------- worker 模式


def _worker_main(spec_path: str) -> int:
    """``python artifacts.py --pack <spec.json>`` 的入口。stdout 每行一个 JSON。

    计数走一遍、加文件走一遍：十万个路径全缓在内存里不划算。
    """
    def emit(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        out = Path(spec["out_zip"])
        sources = spec["sources"]
    except Exception as e:  # spec 坏掉也要说人话
        emit({"event": "error", "message": f"打包参数不可用：{e}"})
        return 2

    roots = []
    for s in sources:
        src = Path(s["src"])
        if not src.is_dir():
            emit({"event": "error", "message": f"产物目录不存在：{src}"})
            return 2
        roots.append((src, s["arcname"]))

    total = 0
    for src, _arc in roots:
        for _d, _dn, fns in os.walk(src, followlinks=False):
            total += len(fns)
    emit({"event": "start", "pid": os.getpid(), "total": total, "sources": len(roots)})

    out.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    skipped = 0
    last_pct = -1
    # allowZip64 必须显式开：单任务上限 20 GB，普通 zip 的 4 GB 上限会直接写爆。
    # compresslevel=1 是速度优先的取舍——产物体积以 GB 计，deflate 9 的时间不划算。
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True,
                             compresslevel=1) as zf:
            for src, arc in roots:
                for dirpath, _dn, fns in os.walk(src, followlinks=False):
                    for fn in sorted(fns):
                        p = Path(dirpath) / fn
                        arcname = f"{arc}/{p.relative_to(src).as_posix()}"
                        try:
                            zf.write(p, arcname)
                        except OSError:
                            skipped += 1  # 打包途中 agent 又在写/删，跳过不中断
                            continue
                        done += 1
                        pct = int(done * 100 / total) if total else 100
                        if pct != last_pct:
                            last_pct = pct
                            emit({"event": "progress", "done": done, "total": total})
    except Exception as e:
        emit({"event": "error", "message": f"打包失败：{e}"})
        return 2

    emit({"event": "done", "path": str(out), "files": done, "skipped": skipped,
          "total": total})
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--pack":
        sys.exit(_worker_main(sys.argv[2]))
    sys.exit("用法：python artifacts.py --pack <spec.json>")


__all__ = [
    "discover",
    "package",
    "verify",
    "preflight",
    "downloadable",
    "outputs_root",
    "default_package_path",
    "PackageError",
    "QuotaExceeded",
    "TASK_QUOTA_BYTES",
]
