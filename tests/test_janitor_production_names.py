r"""janitor 对**生产文件名**的钉死项。

为什么单独一个文件
------------------
`tests/test_janitor.py` 全部用纯数字名造样本（`1.log` / `1.zip`），而生产代码产出的是
`agent-12.log` / `agent-12.err.log` / `task_12.zip`。于是那批断言**恒真**：把形态闸
改成任何东西它们照样全绿，却给出"保留策略已钉死"的假安全感。

后果已经付过一次：形态闸只认 `^\d+`，logs/ 与 packages/ 的每个文件都被挡在门外，
**保留策略从未真正执行过一次删除**，而"磁盘紧张时最旧优先回收"这个唯一的安全阀
是死代码。E 盘只增不减，单任务配额 20 GB。

这一份全部用生产者的真实命名，并加一条"哨兵会响"的断言。
"""
from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

import pytest

import db
import janitor


def _age(path: Path, days: float) -> Path:
    """把文件与所在目录的 mtime 一起推到 days 天前（_expired 取目录 mtime）。"""
    t = time.time() - days * 86400
    for p in (path, path.parent):
        os.utime(p, (t, t))
    return path


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(tmp_path / "data" / "web.db"))
    import db as _db
    _db.init_db()
    r = tmp_path / "data"
    for sub in ("logs", "packages", "work", "uploads"):
        (r / sub).mkdir(parents=True, exist_ok=True)
    return r


# ---------------------------------------------------------------- 日志

def test_agent_log_is_actually_deleted_when_expired(root):
    """dispatcher._spawn 产出的就是这个名字。过期就得删掉。"""
    f = root / "logs" / "agent-12.log"
    f.write_text("x", encoding="utf-8")
    _age(f, janitor.LOG_KEEP_DAYS + 5)

    stats = janitor.sweep(root, now=datetime.datetime.now())

    assert not f.exists(), "过期的 agent 日志没被删——形态闸和生产命名对不上"
    assert stats["log_files"] == 1, stats


def test_agent_err_log_is_actually_deleted_when_expired(root):
    f = root / "logs" / "agent-12.err.log"
    f.write_text("boom", encoding="utf-8")
    _age(f, janitor.LOG_KEEP_DAYS + 5)

    janitor.sweep(root, now=datetime.datetime.now())

    assert not f.exists(), "过期的 agent stderr 日志没被删"


def test_rotated_log_is_deleted(root):
    f = root / "logs" / "agent-12.log.1"
    f.write_text("old", encoding="utf-8")
    _age(f, janitor.LOG_KEEP_DAYS + 5)

    janitor.sweep(root, now=datetime.datetime.now())

    assert not f.exists(), "轮转出来的 .log.1 没被回收"


def test_fresh_agent_log_is_kept(root):
    f = root / "logs" / "agent-12.log"
    f.write_text("刚写的", encoding="utf-8")

    janitor.sweep(root, now=datetime.datetime.now())

    assert f.exists(), "没到保留期的日志不该被删"


def test_foreign_file_in_logs_is_never_touched(root):
    """管理员自己放的东西一律不碰——闸门的作用之一就是这个。"""
    f = root / "logs" / "readme.txt"
    f.write_text("说明", encoding="utf-8")
    _age(f, janitor.LOG_KEEP_DAYS * 3)

    janitor.sweep(root, now=datetime.datetime.now())

    assert f.exists()


# ---------------------------------------------------------------- 产物包

def test_package_is_actually_deleted_when_expired(root):
    """artifacts.default_package_path 产出的就是 task_12.zip。"""
    f = root / "packages" / "task_12.zip"
    f.write_bytes(b"PK\x03\x04" + b"0" * 200)
    _age(f, janitor.PACKAGE_KEEP_DAYS + 5)

    stats = janitor.sweep(root, now=datetime.datetime.now())

    assert not f.exists(), "过期的产物包没被删——磁盘只增不减就是这么来的"
    assert stats["packages"] == 1, stats


def test_fresh_package_is_kept(root):
    f = root / "packages" / "task_12.zip"
    f.write_bytes(b"PK\x03\x04")

    janitor.sweep(root, now=datetime.datetime.now())

    assert f.exists(), "没到保留期的产物包不该被删"


def test_reclaim_packages_actually_reclaims(root, monkeypatch):
    """磁盘紧张时最旧优先回收——这是唯一的兜底安全阀，必须真的能回收。"""
    for i in (1, 2, 3):
        f = root / "packages" / f"task_{i}.zip"
        f.write_bytes(b"PK\x03\x04" + b"0" * 1000)
        _age(f, 10 + i)                       # task_1 最旧
    monkeypatch.setattr(janitor, "_free_bytes", lambda _r: 0)

    stats = janitor.sweep(root, now=datetime.datetime.now(), target_free_bytes=10**9)

    assert stats["packages"] >= 1, "磁盘紧张时一个包都没回收，安全阀是死代码"
    assert not (root / "packages" / "task_1.zip").exists(), "最旧的包没被优先回收"


# ---------------------------------------------------------------- 任务目录

def test_expired_task_dir_is_deleted(root):
    """work/12 是纯数字名——这一条原本就是对的，但要和上面几条放一起才看得出问题。"""
    d = root / "work" / "12"
    d.mkdir()
    (d / "brief.md").write_text("b", encoding="utf-8")
    _age(d, janitor.WORK_KEEP_DAYS + 5)

    janitor.sweep(root, now=datetime.datetime.now())

    assert not d.exists()


def test_non_numeric_dir_in_work_is_never_deleted(root):
    d = root / "work" / "not-a-task"
    d.mkdir()
    _age(d, janitor.WORK_KEEP_DAYS * 3)

    janitor.sweep(root, now=datetime.datetime.now())

    assert d.exists(), "形状闸的职责就是不碰不像任务目录的东西"


# ---------------------------------------------------------------- 哨兵

def test_naming_drift_is_reported_not_silent(root):
    """形状又和生产者对不上时，必须吵。

    这是本次事故的直接对策：闸门的形状漂了，sweep 要在 errors 里说出来，
    而不是安静地什么都不删。
    """
    f = root / "packages" / "result_2026.zip"      # 换了个命名
    f.write_bytes(b"PK\x03\x04")
    _age(f, janitor.PACKAGE_KEEP_DAYS * 4)

    stats = janitor.sweep(root, now=datetime.datetime.now())

    assert any("形态闸" in e for e in stats["errors"]), \
        f"命名漂移没有被报出来：{stats['errors']}"


def test_shapes_cover_every_production_prefix():
    """形态表必须覆盖生产者真的会写出来的名字。"""
    assert janitor._shape_for(Path("work")).match("12")
    assert janitor._shape_for(Path("uploads")).match("12")
    assert janitor._shape_for(Path("logs")).match("agent-12.log")
    assert janitor._shape_for(Path("logs")).match("agent-12.err.log")
    assert janitor._shape_for(Path("logs")).match("agent-12.log.1")
    assert janitor._shape_for(Path("packages")).match("task_12.zip")


def test_shapes_do_not_match_production_prefix_with_wrong_id():
    """不能靠前缀蒙混过关：id 必须是数字。"""
    assert not janitor._shape_for(Path("packages")).match("task_abc.zip")
    assert not janitor._shape_for(Path("logs")).match("agent-x.log")
    # 也不能带路径分隔符
    assert not janitor._shape_for(Path("packages")).match("../evil.zip")
