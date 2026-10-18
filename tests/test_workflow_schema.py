"""三张新表 + tasks 三列的迁移（规格 4.1/4.2/4.3）。

只测新库等于没测——线上那个 data\web.db 早在本次改动之前
就建好了，`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作。所以这里手写一份
**旧 schema** 的库来跑迁移，不用字符串手术去改 SCHEMA（那种写法在 SCHEMA 一变
就悄悄失配）。
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# 与 db.SCHEMA 里改动前的 tasks/users 完全一致（逐字照抄，不是拼出来的）
LEGACY_SCHEMA = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL, salt TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user', disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, CHECK (role IN ('user','admin'))
);
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL, brief TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  step TEXT NOT NULL DEFAULT '',
  owner_id INTEGER NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  uploads_dir TEXT NOT NULL DEFAULT '', output_dir TEXT NOT NULL DEFAULT '',
  package_path TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, done_at TEXT,
  CHECK (status IN ('queued','running','packaging','done','failed'))
);
CREATE INDEX idx_tasks_owner ON tasks(owner_id, status);
"""

NEW_TASK_COLUMNS = {"workflow_id", "workflow_row", "workflow_archived"}


def _make_legacy(path: Path) -> None:
    c = sqlite3.connect(path)
    try:
        c.executescript(LEGACY_SCHEMA)
        c.commit()
    finally:
        c.close()


def _task_columns(db_path: Path) -> set[str]:
    c = sqlite3.connect(db_path)
    try:
        return {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    finally:
        c.close()


def _table_names(db_path: Path) -> set[str]:
    c = sqlite3.connect(db_path)
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


# --- 旧库迁移 ---------------------------------------------------------------

def test_legacy_db_gains_the_three_columns(tmp_path, monkeypatch):
    """这是整个任务的核心：旧库里三列必须真的出现。"""
    legacy = tmp_path / "legacy.db"
    _make_legacy(legacy)
    assert not (NEW_TASK_COLUMNS & _task_columns(legacy)), "夹具本身就带新列，测了个空"

    monkeypatch.setenv("PROTFORGE_WEB_DB", str(legacy))
    import db
    db.init_db()

    assert NEW_TASK_COLUMNS <= _task_columns(legacy)


def test_legacy_db_gains_the_three_tables(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.db"
    _make_legacy(legacy)
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(legacy))
    import db
    db.init_db()
    names = _table_names(legacy)
    assert {"workflows", "workflow_columns", "workflow_skips"} <= names


def test_fresh_db_has_them_too(tmp_path, monkeypatch):
    """新库走 SCHEMA 里的完整定义——两条路必须得到同一个结果。"""
    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(fresh))
    import db
    db.init_db()
    assert NEW_TASK_COLUMNS <= _task_columns(fresh)
    assert {"workflows", "workflow_columns", "workflow_skips"} <= _table_names(fresh)


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    """调两次不许炸，也不许重复加。"""
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(tmp_path / "twice.db"))
    import db
    db.init_db()
    db.init_db()
    assert NEW_TASK_COLUMNS <= _task_columns(tmp_path / "twice.db")


# --- 竞态容忍：确定性测这两个分支 -------------------------------------------

def test_add_column_tolerates_duplicate():
    """两个进程同时补列，后到的那个拿到 duplicate column —— 必须吞掉。

    这是**确定性**的：直接对同一个连接加两次同一列，不靠"起几个进程碰碰运气"。
    概率复现不算围栏（那意味着回归有 N% 概率溜过去）。
    """
    import db
    c = db.connect()
    try:
        db._add_column(c, "workflow_row INTEGER")
        db._add_column(c, "workflow_row INTEGER")   # 再来一次，不该抛
    finally:
        c.close()


def test_add_column_reraises_other_errors():
    """宽泛的 except 会把磁盘满、库被锁、语法错一起吞掉，迁移失败却报告成功。"""
    import db
    c = db.connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            db._add_column(c, "这根本不是合法 SQL,")
    finally:
        c.close()


def test_three_processes_migrate_same_legacy_db(tmp_path):
    """真起三个进程同时 init_db。整合检查，不是唯一防线（上面两条才是）。"""
    legacy = tmp_path / "race.db"
    _make_legacy(legacy)
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import db; db.init_db(); print('ok')\n" % str(REPO / "server")
    )
    env = {
        "PROTFORGE_WEB_DB": str(legacy),
        "PROTFORGE_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PATH": "",
        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "C:\\Windows"),
    }
    procs = [
        subprocess.Popen([sys.executable, "-c", code], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(3)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        outs.append((p.returncode, out.decode("utf-8", "replace"),
                     err.decode("utf-8", "replace")))
    for rc, out, err in outs:
        assert rc == 0, f"某个进程迁移失败 rc={rc}\n{out}\n{err}"
    assert NEW_TASK_COLUMNS <= _task_columns(legacy)


# --- 表约束 -----------------------------------------------------------------

def test_role_check_rejects_unknown():
    import db
    c = db.connect()
    try:
        with c:
            c.execute("INSERT INTO users (username, password_hash, salt, role,"
                      " disabled, created_at) VALUES ('u','h','s','user',0,'t')")
            uid = c.execute("SELECT id FROM users WHERE username='u'").fetchone()[0]
            c.execute("INSERT INTO workflows (owner_id, name, prompt_template,"
                      " created_at, updated_at) VALUES (?, 'w', 't', 't', 't')", (uid,))
            wid = c.execute("SELECT id FROM workflows WHERE name='w'").fetchone()[0]
            for bad in ("contextt", "", "CONTEXT"):
                with pytest.raises(sqlite3.IntegrityError):
                    c.execute("INSERT INTO workflow_columns (workflow_id,"
                              " col_index, role) VALUES (?, 0, ?)", (wid, bad))
            c.execute("INSERT INTO workflow_columns (workflow_id, col_index,"
                      " role) VALUES (?, 1, 'context')", (wid,))
    finally:
        c.close()
