"""janitor.py 的测试（spec §3.05.2 临时文件与保留策略）。

两条主线：
1. **该删的删干净** —— work/uploads 7 天、logs 14 天 + 64 MB 单文件上限、
   packages 30 天、audit_log 30 天、过期 sessions。
2. **不该删的一个都不删** —— 数据根之外的路径、非 `<数字>` 开头的目录名、
   磁盘不紧张时的产物包，都必须原样留下。

数据根由 conftest 的 autouse 夹具指到 tmp_path，绝不碰真实的 <数据盘根>。
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import db  # noqa: E402
import janitor  # noqa: E402

NOW = datetime(2026, 10, 16, 12, 0, 0)


def ago(days: float = 0, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


def stamp(path: Path, when: datetime) -> Path:
    """把 mtime 改成 when——保留策略按 mtime 判定，测试必须能造出「8 天前」的文件。"""
    t = when.timestamp()
    os.utime(str(path), (t, t))
    return path


def mkfile(path: Path, size: int = 4, when: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size)
    if when is not None:
        stamp(path, when)
    return path


def mkdir(path: Path, when: datetime | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if when is not None:
        stamp(path, when)
    return path


@pytest.fixture
def root(tmp_path) -> Path:
    """数据根。conftest 已把环境变量指向 tmp_path/data，这里只负责把目录结构铺出来。"""
    r = tmp_path / "data"
    for sub in ("work", "uploads", "logs", "packages"):
        (r / sub).mkdir(parents=True, exist_ok=True)
    return r


# --- plan Step 1 的两条守护用例 ----------------------------------------------------


def test_sweep_only_deletes_own_root(tmp_path, root):
    outsider = tmp_path / "elsewhere"
    outsider.mkdir()
    (outsider / "x").write_text("keep")
    janitor.sweep(root, NOW)
    assert (outsider / "x").exists()  # 数据根之外的一律不碰


def test_manually_added_file_in_work_survives(tmp_path, root):
    """work/<task_id> 内非 <数字>_ 形态的目录不得被删。"""
    manual = root / "work" / "我手动放的资料"
    mkfile(manual / "notes.md")
    stamp(manual, ago(days=90))          # 足够老：不是「太新所以没删」
    janitor.sweep(root, NOW)
    assert (manual / "notes.md").exists()


# --- work / uploads：终态后保留 7 天，两处同步删 ----------------------------------


@pytest.mark.parametrize("sub", ["work", "uploads"])
def test_task_dir_older_than_7_days_deleted(root, sub):
    old = mkdir(root / sub / "1")
    mkfile(old / "payload.pdb")
    stamp(old, ago(days=8))          # 内容写完再改 mtime，否则建文件会把它刷新成现在
    janitor.sweep(root, NOW)
    assert not (root / sub / "1").exists()


@pytest.mark.parametrize("sub", ["work", "uploads"])
def test_task_dir_within_7_days_kept(root, sub):
    fresh = mkdir(root / sub / "1", ago(days=3))
    janitor.sweep(root, NOW)
    assert fresh.exists()


def test_work_and_uploads_removed_together(root):
    """uploads 与 work 同寿期，work 被删时 uploads 必须同步消失，不能留半截。"""
    for sub in ("work", "uploads"):
        stamp(mkdir(root / sub / "7"), ago(days=8))
    janitor.sweep(root, NOW)
    assert not (root / "work" / "7").exists()
    assert not (root / "uploads" / "7").exists()


def test_running_task_work_dir_never_deleted(root):
    """超长任务（跑了 8 天还没进终态）的工作区不能被清掉——这是最贵的一种误删。"""
    c = db.connect()
    try:
        c.execute(
            "INSERT INTO tasks(title,owner_id,status,created_at,updated_at)"
            " VALUES('长跑',1,'running',?,?)",
            (NOW.strftime("%Y-%m-%dT%H:%M:%S.%f"),) * 2)
        c.commit()
        tid = str(c.execute("SELECT id FROM tasks").fetchone()["id"])
    finally:
        c.close()
    for sub in ("work", "uploads"):
        d = mkdir(root / sub / tid)
        mkfile(d / "intermediate.cif")
        stamp(d, ago(days=8))
    janitor.sweep(root, NOW)
    assert (root / "work" / tid / "intermediate.cif").exists()
    assert (root / "uploads" / tid).exists()


def test_terminal_task_work_dir_is_reclaimed(root):
    """进终态（done）满 7 天的任务，工作区与上传件照样回收。"""
    c = db.connect()
    try:
        c.execute(
            "INSERT INTO tasks(title,owner_id,status,created_at,updated_at)"
            " VALUES('已完成',1,'done',?,?)",
            (ago(days=30).strftime("%Y-%m-%dT%H:%M:%S.%f"),) * 2)
        c.commit()
        tid = str(c.execute("SELECT id FROM tasks").fetchone()["id"])
    finally:
        c.close()
    for sub in ("work", "uploads"):
        d = mkdir(root / sub / tid)
        mkfile(d / "result.pdb")
        stamp(d, ago(days=8))
    janitor.sweep(root, NOW)
    assert not (root / "work" / tid).exists()
    assert not (root / "uploads" / tid).exists()


# --- logs：保留 14 天 + 单文件 64 MB 上限 ----------------------------------------


def test_log_older_than_14_days_deleted(root):
    old = mkfile(root / "logs" / "agent-1.log", when=ago(days=15))
    janitor.sweep(root, NOW)
    assert not old.exists()


def test_log_within_14_days_kept(root):
    fresh = mkfile(root / "logs" / "agent-1.log", when=ago(days=13))
    janitor.sweep(root, NOW)
    assert fresh.exists()


def test_rotated_log_also_expires(root):
    rot = mkfile(root / "logs" / "agent-1.log.1", when=ago(days=15))
    janitor.sweep(root, NOW)
    assert not rot.exists()


def test_oversized_log_truncated_and_rotated(root, monkeypatch):
    monkeypatch.setattr(janitor, "LOG_MAX_BYTES", 1000)
    log = mkfile(root / "logs" / "agent-2.log", size=4000)
    raw = log.read_bytes()
    janitor.sweep(root, NOW)
    assert log.exists() and log.stat().st_size == 0          # 原位置开新文件
    rot = root / "logs" / "agent-2.log.1"
    assert rot.exists() and rot.stat().st_size <= 1000       # 截断到上限内
    assert raw.endswith(rot.read_bytes())                   # 留存的是最新的尾部


def test_small_log_is_not_rotated(root, monkeypatch):
    monkeypatch.setattr(janitor, "LOG_MAX_BYTES", 1000)
    log = mkfile(root / "logs" / "agent-3.log", size=10)
    janitor.sweep(root, NOW)
    assert not (root / "logs" / "agent-3.log.1").exists()
    assert log.stat().st_size == 10


def test_logs_manual_file_kept(root):
    """logs 下非 .log 的文件（比如管理员放的说明）不参与轮转与过期删除。"""
    keep = mkfile(root / "logs" / "readme.txt", when=ago(days=99))
    janitor.sweep(root, NOW)
    assert keep.exists()


# --- packages：保留 30 天，磁盘紧张时最旧优先 ------------------------------------


def test_package_older_than_30_days_deleted(root):
    old = mkfile(root / "packages" / "task_1.zip", when=ago(days=31))
    janitor.sweep(root, NOW)
    assert not old.exists()


def test_package_within_30_days_kept(root):
    fresh = mkfile(root / "packages" / "task_1.zip", when=ago(days=29))
    janitor.sweep(root, NOW)
    assert fresh.exists()


def test_reclaim_packages_oldest_first_when_disk_tight(root, monkeypatch):
    """磁盘吃紧时提前回收，且严格按最旧优先，不能挑新的删。

    空闲空间是打桩的：本机 13 个 worker 并行写盘，真实 disk_usage 一直在动，
    拿它做断言就是随机失败。这里只桩掉「剩多少空间」，回收顺序仍走真实逻辑。
    """
    old = mkfile(root / "packages" / "task_100.zip", size=100, when=ago(days=5))
    mid = mkfile(root / "packages" / "task_101.zip", size=200, when=ago(days=3))
    new = mkfile(root / "packages" / "task_102.zip", size=300, when=ago(days=1))
    monkeypatch.setattr(janitor, "_free_bytes", lambda p: 0)   # 假装一个字节都不剩

    janitor.sweep(root, NOW, target_free_bytes=150)

    assert not old.exists()      # 最旧的先走
    assert not mid.exists()      # 100 < 150 不够，接着删次旧的
    assert new.exists()          # 100+200 = 300 >= 150，收手


def test_reclaim_does_nothing_when_already_free_enough(root, monkeypatch):
    pkg = mkfile(root / "packages" / "task_100.zip", when=ago(days=1))
    monkeypatch.setattr(janitor, "_free_bytes", lambda p: 10 ** 12)
    janitor.sweep(root, NOW, target_free_bytes=100)
    assert pkg.exists()


def test_no_reclaim_without_target(root, monkeypatch):
    """不传 target_free_bytes 就只按 30 天过期删，不做提前回收。"""
    pkg = mkfile(root / "packages" / "100.zip", size=100, when=ago(days=1))
    monkeypatch.setattr(janitor, "_free_bytes", lambda p: 0)
    janitor.sweep(root, NOW)
    assert pkg.exists()


# --- 数据库行：audit_log 30 天、过期 sessions -------------------------------------


def _seed_rows(root: Path):
    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
    c = db.connect()
    try:
        c.execute(
            "INSERT INTO users(username,password_hash,salt,created_at)"
            " VALUES('u','h','s',?)", (iso(NOW),))
        uid = c.execute("SELECT id FROM users").fetchone()["id"]
        for d in (31, 10):
            c.execute(
                "INSERT INTO audit_log(task_id,kind,actor,created_at)"
                " VALUES(NULL,'x','human',?)", (iso(ago(days=d)),))
        c.execute(
            "INSERT INTO sessions(token,user_id,created_at,expires_at,last_seen_at)"
            " VALUES('stale',?,?,?,?)",
            (uid, iso(ago(days=3)), iso(ago(days=2)), iso(ago(days=2))))
        c.execute(
            "INSERT INTO sessions(token,user_id,created_at,expires_at,last_seen_at)"
            " VALUES('live',?,?,?,?)",
            (uid, iso(ago(days=1)), iso(NOW + timedelta(days=1)),
             iso(ago(days=0, hours=1))))
        c.commit()
    finally:
        c.close()


def test_audit_log_older_than_30_days_deleted(root):
    _seed_rows(root)
    janitor.sweep(root, NOW)
    c = db.connect()
    try:
        rows = [r["created_at"] for r in c.execute("SELECT created_at FROM audit_log")]
    finally:
        c.close()
    assert len(rows) == 1 and rows[0].startswith("2026-10-06")


def test_expired_sessions_deleted_live_kept(root):
    _seed_rows(root)
    janitor.sweep(root, NOW)
    c = db.connect()
    try:
        tokens = [r["token"] for r in c.execute("SELECT token FROM sessions")]
    finally:
        c.close()
    assert tokens == ["live"]


# --- 边界：只删本项目自己的东西 ----------------------------------------------------


def test_symlink_out_of_root_not_followed(tmp_path, root):
    """work\\1 是个指向数据根之外的链接——删它只能摘链接，绝不能顺着删掉外面的东西。

    这是三道闸里「位置闸」真正在干的事：resolve 跟到真实目标后它落在 root 之外，
    于是整条候选被判不合格。
    """
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "irreplaceable.cif").write_text("do not touch")
    stamp(precious / "irreplaceable.cif", ago(days=99))

    link = root / "work" / "5"
    try:
        os.symlink(str(precious), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本机无创建符号链接的权限")
    stamp(link, ago(days=8))

    janitor.sweep(root, NOW)

    assert (precious / "irreplaceable.cif").exists()   # 外面的一律不碰
    assert link.exists()   # 链接本身也不摘：解析后落在 root 外 = 完全不处理


def test_manual_dir_in_uploads_and_packages_survives(root):
    a = mkfile(root / "uploads" / "备份", when=ago(days=99))
    b = mkfile(root / "packages" / "手工导出.zip.bak", when=ago(days=99))
    janitor.sweep(root, NOW)
    assert a.exists() and b.exists()


def test_sweep_does_not_create_missing_dirs(root):
    shutil.rmtree(root / "logs")
    janitor.sweep(root, NOW)
    assert not (root / "logs").exists()


def test_sweep_on_bare_root_creates_nothing(tmp_path):
    """完全空的数据根：清理器是只读的扫描器，不能顺手把目录结构建出来。"""
    bare = tmp_path / "bare"
    bare.mkdir()
    janitor.sweep(bare, NOW)
    assert list(bare.iterdir()) == []


def test_sweep_is_idempotent(root):
    """第二轮必须是空操作——重复跑不会二次删除，也不会把统计算乱。"""
    stamp(mkdir(root / "work" / "1"), ago(days=8))
    stamp(mkfile(root / "logs" / "agent-1.log"), ago(days=20))
    stamp(mkfile(root / "packages" / "task_1.zip"), ago(days=40))
    first = janitor.sweep(root, NOW)
    second = janitor.sweep(root, NOW)
    assert first["work_dirs"] == first["log_files"] == first["packages"] == 1
    assert second["errors"] == []
    assert sum(v for k, v in second.items() if k != "errors") == 0


def test_stats_report_counts(root):
    stamp(mkdir(root / "work" / "1"), ago(days=8))
    stamp(mkfile(root / "logs" / "agent-1.log"), ago(days=20))
    stamp(mkfile(root / "packages" / "task_1.zip"), ago(days=40))
    s = janitor.sweep(root, NOW)
    assert s["work_dirs"] == 1
    assert s["log_files"] == 1
    assert s["packages"] == 1
    assert s["errors"] == []
