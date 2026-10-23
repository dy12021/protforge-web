r"""三张新表 + tasks 六列的迁移（规格 4.1/4.2/4.3 + 2026-10-23 的验收三列）。

只测新库等于没测——线上那个 <数据盘根>\web.db 早在本次改动之前
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

#: **靠 ALTER TABLE ADD COLUMN 补出来的列**（迁移库会把它们排在末尾）。
#: 前三列是工作流那轮加的，后三列是 2026-10-23 的验收那轮加的
#: （acceptance / declared / followup）。
NEW_TASK_COLUMNS = {"workflow_id", "workflow_row", "workflow_archived",
                    "acceptance", "declared", "followup"}

#: 迁移库末尾的列，**顺序就是 _MISSING_TASK_COLUMNS 里的顺序**
#: （ALTER TABLE 只能追加，所以它们全排在最后）。
MIGRATED_TAIL = ["workflow_id", "workflow_row", "workflow_archived",
                 "acceptance", "declared", "followup"]


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


def _task_column_order(db_path: Path) -> list[str]:
    """按表里真实的物理顺序返回列名。迁移库和新库的顺序对不上，只能靠这个看。"""
    c = sqlite3.connect(db_path)
    try:
        return [r[1] for r in c.execute("PRAGMA table_info(tasks)")]
    finally:
        c.close()


def _task_index_names(db_path: Path) -> set[str]:
    c = sqlite3.connect(db_path)
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks'")}
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
    """新库走 SCHEMA 里的完整定义。

    只断「三列三表都在」；新库与迁移库的**列名集合**是否一致，由下面
    test_fresh_and_migrated_tasks_agree_on_names_not_order 单独钉住
    （两者的物理顺序一定不同，合并断言会掩盖真正的契约）。
    """
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


# --- 新库 vs 迁移库：契约是「列名集合相同」，不是「顺序相同」 --------------

def test_fresh_and_migrated_tasks_agree_on_names_not_order(tmp_path, monkeypatch):
    """两条路的列名集合必须**完全相同**；但顺序一定不同，代码不得依赖顺序。

    ALTER TABLE ADD COLUMN 只能追加，所以迁移库的 workflow_* 三列与
    验收那三列永远排在 created_at/updated_at/done_at 之后，而 SCHEMA 里它们
    写在 session_id 之后。这个顺序分叉是结构性的、消不掉（要消只能重建表 +
    搬数据），所以真正的契约是列名集合相同——这里断集合，顺带把分叉显式
    钉出来，免得将来有人照着 docstring「两条路得到同一个结果」写出按位置
    解包的代码。
    """
    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(fresh))
    import db
    db.init_db()

    legacy = tmp_path / "legacy.db"
    _make_legacy(legacy)
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(legacy))
    db.init_db()

    fresh_order = _task_column_order(fresh)
    migrated_order = _task_column_order(legacy)

    assert set(fresh_order) == set(migrated_order), "两条路的列名集合必须一样"
    # 顺序确实不同，且分叉点正是被 ALTER 追加到末尾的那几列
    assert fresh_order != migrated_order
    assert migrated_order[-len(MIGRATED_TAIL):] == MIGRATED_TAIL
    assert fresh_order.index("workflow_id") < fresh_order.index("created_at")


def test_workflow_index_needs_no_migration(tmp_path, monkeypatch):
    """索引不用进 _MISSING_TASK_COLUMNS：CREATE INDEX IF NOT EXISTS 自己会补建。

    这条同时钉住两件事：新库有 idx_tasks_workflow，旧库迁移后也有；
    以及 SCHEMA 里那句 CREATE INDEX 对已存在的表**不是**空操作。
    """
    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(fresh))
    import db
    db.init_db()

    legacy = tmp_path / "legacy.db"
    _make_legacy(legacy)
    assert "idx_tasks_workflow" not in _task_index_names(legacy), "夹具本身不该有这索引"
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(legacy))
    db.init_db()

    assert "idx_tasks_workflow" in _task_index_names(fresh)
    assert "idx_tasks_workflow" in _task_index_names(legacy)


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


# --- _enable_wal 的两个 raise 分支 ------------------------------------------

class _WALSpy:
    """包一层连接，数 `PRAGMA journal_mode=WAL` 真的执行了几次。

    围栏必须**数次数**：只断「抛了」不够——把重试循环整个删掉也照样抛，
    看起来是绿的，但那正是这里要防的静默失效。所以每次 WAL 语句要么抛预置的
    错、要么透传给真连接。
    """

    def __init__(self, inner: sqlite3.Connection, errors: list[Exception]) -> None:
        self._inner = inner
        self._errors = list(errors)
        self.wal_calls = 0

    def execute(self, sql, *args):
        if "journal_mode=WAL" in sql:
            self.wal_calls += 1
            if self._errors:
                raise self._errors.pop(0)
        return self._inner.execute(sql, *args)


def test_enable_wal_reraises_non_lock_error_without_retrying():
    """非锁错必须**立刻**上抛，一次都不许重试——这是 _enable_wal 里第一个 raise 的围栏。

    把它换成 pass，重试会一路空转到循环结束，connect() 照样交出一条 journal mode
    根本没切成 WAL 的连接：这正是本项目最熟的那种静默失效形状（迁移失败却报告成功）。
    只读库（`attempt to write a readonly database`）是这条分支的真实触发场景，
    但它依赖平台的文件属性语义，所以这里用 spy 稳定地把它逼出来。
    """
    import db
    inner = sqlite3.connect(":memory:")          # :memory: 的 journal_mode 是 memory
    try:
        spy = _WALSpy(inner, [sqlite3.OperationalError("disk I/O error")])
        with pytest.raises(sqlite3.OperationalError):
            db._enable_wal(spy, attempts=50, delay=0)
        assert spy.wal_calls == 1, "非锁错不该重试"
    finally:
        inner.close()


def test_enable_wal_reraises_when_retries_exhausted():
    """锁一直占着就必须明着失败，不能静默返回——这是第二个 raise 的围栏。

    attempts=1 让它确定性地走到耗尽分支，不必真去撞 50 次锁。
    """
    import db
    inner = sqlite3.connect(":memory:")
    try:
        spy = _WALSpy(inner, [sqlite3.OperationalError("database is locked")])
        with pytest.raises(sqlite3.OperationalError):
            db._enable_wal(spy, attempts=1, delay=0)
        assert spy.wal_calls == 1
    finally:
        inner.close()


def test_enable_wal_retries_until_it_succeeds():
    """反过来也钉住：真的锁冲突必须重试，不能第一次失败就上抛。

    前两条围栏会把「重试循环整个删掉」也判红，这条防的是反方向——
    有人图省事把 except 整个改成 raise。
    """
    import db
    inner = sqlite3.connect(":memory:")
    try:
        spy = _WALSpy(inner, [sqlite3.OperationalError("database is locked")] * 2)
        db._enable_wal(spy, attempts=5, delay=0)     # 第 3 次成功，不该抛
        assert spy.wal_calls == 3
    finally:
        inner.close()


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
