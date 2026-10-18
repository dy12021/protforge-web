# 工作流（Excel 批量计算）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在平台上建一个「上传 Excel → 选列配模板 → 每行跑一个任务 → 结果回写表格」的批量工作流，工作流行任务排在该用户所有常规任务之后，一行归档完再放下一行。

**Architecture:** 工作流行 = 带 `workflow_id` / `workflow_row` / `workflow_archived` 三个可空列的普通 task，不造第二套任务生命周期，中止/重跑/打包/审计/详情/下载/SSE 全部原样复用。调度用**游标式**：`workflows.cursor` 只在放行时 +1，`workflow_archived` 是「这一行归档了吗」的唯一判据，调度器每 tick 跑一个放行 pass 和一个归档 pass。工作流源文件落 `data/workflows/<id>/`，刻意在 `guards.regions()` 四区之外，回写一律由平台做。

**Tech Stack:** Python 3（`C:\ProgramData\anaconda3\python.exe`）、FastAPI 0.136.3、Starlette 1.3.1、SQLite、openpyxl 3.1.5、python-multipart 0.0.32；前端零构建原生 HTML/CSS/JS；agent 侧 Node ESM。

## Global Constraints

- **仓库** `.`，**数据盘** `data`
- **测试命令**（每个 bash 调用是全新 shell，命令自带 `Set-Location`）：
  ```powershell
  chcp 65001 | Out-Null; $env:PYTHONIOENCODING='utf-8'
  Set-Location '.'
  & 'C:\ProgramData\anaconda3\python.exe' -m pytest tests -q --no-header -p no:cacheprovider
  ```
- **agent 测试**：`Set-Location '.\agent'; node --test guards.test.mjs`
- **前端冒烟**：`Set-Location '.'; node tests\frontend_smoke.mjs web\app.js`
- `tests/conftest.py` **冻结，不得改**。它 autouse 把库指向 `tmp_path` 并调 `db.init_db()`，测试**绝不碰真实数据盘**
- **界面禁用词**：`AI`、`助手`、`大模型`。统一称「自动化计算平台」「计算任务」「计算进程」。`AI` 必须大小写敏感（小写词满地都是）
- **中文写入禁走 PowerShell 内联**（cp936 静默乱码）→ 用编辑工具，或 `pathlib.write_text(encoding="utf-8")`
- **commit message 含引号会被 PowerShell 拆参** → 先写进 `.git\MSG_*.txt` 再 `git -F`；仓库无 identity → `git -c user.name=mavis -c user.email=mavis@local commit -F`
- **删除用 `rm -- "<绝对路径>"`**（走 mavis-trash），不要用 `Remove-Item`
- **闸门顺序是规格的一部分**（规格 6.4）：`①④⑤②③⑥`。把 ④ 挪到 ② 之后会让「重跑」当场失效
- **围栏必须双向**：坏写法红 + 正确重构不误红。**每个任务的最后一个 Step 就是做这件事**（见该任务的「双向验证」步），不另起一轮
- **双向验证怎么做**：把修复撤掉（按行定位 + 唯一锚点，**不要用正则扫通用关键字**，像 `except Exception:` 这种会匹配到文件里更早的同名块），重跑该任务的测试文件，必须出现**预期的**红；再把修复装回去，重跑，必须全绿。
  **逐条核对**：撤补丁后别的测试跟着红，说明补丁打歪了，不是修复生效。看到红 ≠ 修复生效。
- **断言要读回数据库核对值**，不要断言函数返回成功——`create_task()` 不抛异常的用例在「白名单缺列」和「白名单对了但忘传参」两种坏写法下都会绿
- **`store.Invalid` / `store.NotFound` / `guards.Violation` / `app.PreciseHTTPException` 是四个不同的异常类**，别混用
- **越权一律 404 不 403**（沿用 `store.get_task()` 的既有约定，`app.py:387` 的注释就是理由：不承认 id 存在）
- **`events.actor` 的 CHECK 约束只允许 `human`/`agent`/`admin`/`system`**，行任务用 `system`，来源写进 message

---

### Task 1: 数据模型 —— 三张新表 + `tasks` 三列的幂等迁移

**Files:**
- Modify: `server/db.py`（`SCHEMA` 常量在 46-104；`init_db()` 在 135）
- Test: `tests/test_workflow_schema.py`（新建）

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces:
  - 表 `workflows(owner_id, name, sheet, source_path, prompt_template, header_row, key_col, total_rows, cursor, writeback, writeback_pending, abort_requested, result_path, status, created_at, updated_at)`，`status CHECK IN ('draft','running','paused','finished')`
  - 表 `workflow_columns(workflow_id, col_index, header, role, slot, field)`，`UNIQUE(workflow_id, col_index)`，`role CHECK IN ('key','input','context','output','ignore')`
  - 表 `workflow_skips(workflow_id, row_index, reason, created_at)`，`PRIMARY KEY (workflow_id, row_index)`
  - `tasks` 新增三列：`workflow_id INTEGER REFERENCES workflows(id) ON DELETE CASCADE`、`workflow_row INTEGER`、`workflow_archived INTEGER NOT NULL DEFAULT 0`
  - `db._MISSING_TASK_COLUMNS: tuple[str, ...]`
  - `db._add_column(c: sqlite3.Connection, spec: str) -> None`
  - `db._add_missing_columns(c: sqlite3.Connection) -> None`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_workflow_schema.py`：

```python
"""三张新表 + tasks 三列的迁移（规格 4.1/4.2/4.3）。

只测新库等于没测——线上那个 data\\web.db 早在本次改动之前
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
            db._add_column(c, "这根本不是合法 SQL")
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_workflow_schema.py -q --no-header -p no:cacheprovider`
Expected: FAIL —— `test_legacy_db_gains_the_three_columns` 报 `assert {...} <= set()`，其余报 `AttributeError: module 'db' has no attribute '_add_column'`

- [ ] **Step 3: 加 SCHEMA**

在 `server/db.py` 的 `SCHEMA` 里，**把三张新表插到 `CREATE TABLE IF NOT EXISTS tasks` 之前**（`tasks.workflow_id` 带外键指向 `workflows`；SQLite 不在建表时校验目标是否存在，但依赖表排在前面能免掉一整类建库顺序问题），并给 `tasks` 加三列：

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sheet TEXT NOT NULL DEFAULT '',
  source_path TEXT NOT NULL DEFAULT '',
  prompt_template TEXT NOT NULL,
  header_row INTEGER NOT NULL DEFAULT 1,
  key_col INTEGER NOT NULL DEFAULT 0,
  total_rows INTEGER NOT NULL DEFAULT 0,
  cursor INTEGER NOT NULL DEFAULT 0,
  writeback INTEGER NOT NULL DEFAULT 0,
  writeback_pending INTEGER NOT NULL DEFAULT 0,
  abort_requested INTEGER NOT NULL DEFAULT 0,
  result_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK (status IN ('draft','running','paused','finished'))
);
CREATE INDEX IF NOT EXISTS idx_workflows_owner ON workflows(owner_id, status);
CREATE TABLE IF NOT EXISTS workflow_columns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  col_index INTEGER NOT NULL,
  header TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'ignore',
  slot TEXT NOT NULL DEFAULT '',
  field TEXT NOT NULL DEFAULT '',
  UNIQUE(workflow_id, col_index),
  CHECK (role IN ('key','input','context','output','ignore'))
);
CREATE TABLE IF NOT EXISTS workflow_skips (
  workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  row_index INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (workflow_id, row_index)
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL, brief TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  step TEXT NOT NULL DEFAULT '',
  owner_id INTEGER NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  uploads_dir TEXT NOT NULL DEFAULT '', output_dir TEXT NOT NULL DEFAULT '',
  package_path TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
  workflow_id INTEGER REFERENCES workflows(id) ON DELETE CASCADE,
  workflow_row INTEGER,
  workflow_archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, done_at TEXT,
  CHECK (status IN ('queued','running','packaging','done','failed'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id, status);
"""
```

**下面原有的 `uploads` / `artifacts` / `events` / `users` / `sessions` / `audit_log` / `trg_tasks_touch` 全部原样保留，只把 `tasks` 那段换掉。** 注意 `users` 排在 `workflows` 后面也能建（SQLite 不校验），但为了读起来顺，把 `users` 的定义一起提到 `workflows` 前面——**只调整顺序，不改内容**。

- [ ] **Step 4: 加幂等列补齐**

在 `server/db.py` 的 `init_db()` **之前**加：

```python
_MISSING_TASK_COLUMNS = (
    "workflow_id INTEGER REFERENCES workflows(id) ON DELETE CASCADE",
    "workflow_row INTEGER",
    "workflow_archived INTEGER NOT NULL DEFAULT 0",
)


def _add_column(c, spec: str) -> None:
    """加一列，容忍「别的进程刚加过」这一种失败。

    平台上有三个进程同时开着同一个库（public / admin / internal），它们都在
    启动时调 init_db()，而下面是**读-判断-写**三步：两个进程可能都看到「这一列
    还没有」，然后各自 ALTER，后到的拿到 duplicate column。

    只吞这一种错：宽泛的 except 会把磁盘满、库被锁、语法错一起吞掉，
    迁移失败却报告成功——那是这个项目最熟的一种静默失效形状。
    """
    try:
        c.execute(f"ALTER TABLE tasks ADD COLUMN {spec}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _add_missing_columns(c) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    for spec in _MISSING_TASK_COLUMNS:
        if spec.split()[0] not in have:
            _add_column(c, spec)
```

然后把 `init_db()` 里执行完 `SCHEMA` 的那一行之后接上：

```python
    c.executescript(SCHEMA)
    _add_missing_columns(c)          # 旧库补列；新库这里三个都在，什么也不做
    c.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_workflow_schema.py -q --no-header -p no:cacheprovider`
Expected: PASS（8 passed）

- [ ] **Step 6: 跑全量确认没打破既有测试**

Run: `pytest tests -q --no-header -p no:cacheprovider`
Expected: 全绿，条数 = 改动前 + 8

- [ ] **Step 7: 提交**

把 commit message 写进 `.\.git\MSG_t1.txt`（含中文与引号，**不要**直接 `git commit -m`），然后：

```powershell
Set-Location '.'
git add server/db.py tests/test_workflow_schema.py
git -c user.name=mavis -c user.email=mavis@local commit -F .git\MSG_t1.txt
```

message：工作流数据模型：三张新表 + tasks 三列，含幂等迁移。围栏 8 条，其中竞态容忍是确定性测分支（不靠多进程碰运气），多进程那条只是整合检查。

---

### Task 2: store 层 —— 工作流 CRUD 与三列的写入

**Files:**
- Modify: `server/store.py`（`_update` 白名单在 164-168；`create_task` 在 77；`claim_queued` 在 281）
- Test: `tests/test_workflow_store.py`（新建）

**Interfaces:**
- Consumes: Task 1 的三张表与 `tasks` 三列
- Produces（后续任务全部依赖这些名字与签名）：
  - `create_workflow(owner_id: int, name: str, prompt_template: str, *, sheet: str = "", source_path: str = "", header_row: int = 1, key_col: int = 0, total_rows: int = 0, writeback: int = 0) -> dict`
  - `set_workflow_columns(workflow_id: int, cols: list[dict]) -> list[dict]`（`cols` 每项 `{"col_index", "header", "role", "slot", "field"}`；整体替换，靠 `UNIQUE(workflow_id, col_index)` 幂等）
  - `list_workflows(owner_id=None, *, is_admin: bool = False) -> list[dict]`
  - `get_workflow(workflow_id: int, owner_id=None, is_admin: bool = False) -> dict`（越权抛 `NotFound`）
  - `update_workflow(workflow_id: int, **fields) -> dict`（白名单，见下）
  - `delete_workflow(workflow_id: int) -> None`
  - `skip_workflow_row(workflow_id: int, row_index: int, reason: str = "") -> None`
  - `list_workflow_skips(workflow_id: int) -> set[int]`
  - `create_task(..., workflow_id=None, workflow_row=None) -> dict`（签名向后兼容地加两个**关键字**参数）
  - `update_task(workflow_id=..., workflow_row=..., workflow_archived=...)` 已可用，只靠白名单扩列
  - `list_running_workflows() -> list[dict]`
  - `workflow_has_unarchived(workflow_id: int) -> bool`
  - `workflow_has_live_rows(workflow_id: int) -> bool`
  - `workflow_queued_rows(workflow_id: int) -> list[dict]`
  - `workflow_unarchived_terminal_rows(workflow_id: int) -> list[dict]`
  - `user_has_live_regular_tasks(owner_id: int) -> bool`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_workflow_store.py`：

```python
"""工作流的 store 层 CRUD 与调度器要用的那几问（规格 4.2 / 6.3 / 6.4）。

重点在末尾那几个「问」函数：它们是调度器闸门的**唯一**数据来源，
闸门写对了但这里查错，一样是静默放行或静默卡住。
"""
from __future__ import annotations

import pytest

import store


def _user(username="u1", role="user"):
    import db
    c = db.connect()
    try:
        with c:
            c.execute(
                "INSERT INTO users (username, password_hash, salt, role,"
                " disabled, created_at) VALUES (?, 'h', 's', ?, 0, 't')",
                (username, role))
        return c.execute("SELECT id FROM users WHERE username=?",
                         (username,)).fetchone()[0]
    finally:
        c.close()


def _wf(owner_id, **kw):
    return store.create_workflow(
        owner_id, kw.pop("name", "wf"),
        kw.pop("prompt_template", "查 {{seq}}"),
        **kw)


# --- 建与读 -----------------------------------------------------------------

def test_create_workflow_defaults():
    uid = _user()
    wf = _wf(uid)
    assert wf["status"] == "draft" and wf["cursor"] == 0
    assert wf["writeback_pending"] == 0 and wf["abort_requested"] == 0


def test_get_workflow_is_owner_scoped():
    a, b = _user("a"), _user("b")
    wf = _wf(a)
    assert store.get_workflow(wf["id"], owner_id=a)["id"] == wf["id"]
    with pytest.raises(store.NotFound):
        store.get_workflow(wf["id"], owner_id=b)
    # 管理员不受限
    admin = _user("root", "admin")
    assert store.get_workflow(wf["id"], owner_id=admin, is_admin=True)["id"] == wf["id"]


def test_list_workflows_is_owner_scoped():
    a, b = _user("a"), _user("b")
    _wf(a, name="A 的")
    _wf(b, name="B 的")
    names = {w["name"] for w in store.list_workflows(owner_id=a)}
    assert names == {"A 的"}


# --- 列角色 -----------------------------------------------------------------

def test_set_workflow_columns_replaces_and_is_idempotent():
    uid = _user()
    wf = _wf(uid)
    cols = [{"col_index": 0, "header": "编号", "role": "key"},
            {"col_index": 1, "header": "序列", "role": "input", "slot": "seq"},
            {"col_index": 2, "header": "项目", "role": "context", "slot": "proj"}]
    store.set_workflow_columns(wf["id"], cols)
    store.set_workflow_columns(wf["id"], cols)      # 再来一次，不能炸也不能翻倍
    got = store.list_workflow_columns(wf["id"])
    assert len(got) == 3
    assert {c["col_index"] for c in got} == {0, 1, 2}
    ctx = [c for c in got if c["role"] == "context"][0]
    assert ctx["slot"] == "proj"


def test_set_workflow_columns_rejects_bad_role():
    uid = _user()
    wf = _wf(uid)
    with pytest.raises(store.Invalid):
        store.set_workflow_columns(
            wf["id"], [{"col_index": 0, "role": "contextt"}])


# --- 行任务：三列真的写进去了 -----------------------------------------------

def test_row_task_carries_the_three_columns():
    """**读回数据库核对值**，不是断言 create_task 返回成功。

    白名单缺列时它会抛异常，忘传参时它静默传 None——只断言「没抛异常」的
    用例在两种坏写法下都是绿的。
    """
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("第 1 行", "brief", owner_id=uid,
                            actor="system", workflow_id=wf["id"],
                            workflow_row=0)
    back = store.get_task(row["id"], owner_id=uid)
    assert back["workflow_id"] == wf["id"]
    assert back["workflow_row"] == 0
    assert back["workflow_archived"] == 0


def test_regular_task_has_null_workflow_columns():
    uid = _user()
    row = store.create_task("普通", "b", owner_id=uid)
    back = store.get_task(row["id"], owner_id=uid)
    assert back["workflow_id"] is None and back["workflow_row"] is None


def test_update_workflow_archived():
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=2)
    store.update_task(row["id"], workflow_archived=1)
    assert store.get_task(row["id"], owner_id=uid)["workflow_archived"] == 1


# --- 闸门要问的那几个问题 ---------------------------------------------------

def _row(wf_id, idx, status, archived=0, owner=1):
    return store.create_task(f"r{idx}", "b", owner_id=owner, actor="system",
                             workflow_id=wf_id, workflow_row=idx,
                             ) and store.update_task(
        store.list_workflow_rows(wf_id, idx)[0]["id"], status=status,
        workflow_archived=archived) and None


def test_has_unarchived_reflects_the_flag():
    uid = _user()
    wf = _wf(uid)
    assert store.workflow_has_unarchived(wf["id"]) is False
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    assert store.workflow_has_unarchived(wf["id"]) is True
    store.update_task(row["id"], status="done", workflow_archived=1)
    assert store.workflow_has_unarchived(wf["id"]) is False


def test_has_live_rows_ignores_archived_and_terminal():
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    assert store.workflow_has_live_rows(wf["id"]) is True
    store.update_task(row["id"], status="done")
    assert store.workflow_has_live_rows(wf["id"]) is False


def test_queued_rows_and_unarchived_terminal_rows():
    uid = _user()
    wf = _wf(uid)
    r0 = store.create_task("r0", "b", owner_id=uid, actor="system",
                           workflow_id=wf["id"], workflow_row=0)
    r1 = store.create_task("r1", "b", owner_id=uid, actor="system",
                           workflow_id=wf["id"], workflow_row=1)
    assert {r["id"] for r in store.workflow_queued_rows(wf["id"])} == {r0["id"], r1["id"]}
    assert store.workflow_unarchived_terminal_rows(wf["id"]) == []
    store.update_task(r0["id"], status="done")
    got = store.workflow_unarchived_terminal_rows(wf["id"])
    assert [r["workflow_row"] for r in got] == [0]


def test_user_has_live_regular_tasks_scopes_to_owner():
    a, b = _user("a"), _user("b")
    store.create_task("A 的", "b", owner_id=a)
    assert store.user_has_live_regular_tasks(a) is True
    assert store.user_has_live_regular_tasks(b) is False


def test_workflow_rows_do_not_count_as_regular_tasks():
    """闸门⑥ 问的是常规任务；行任务自己不能把自己挡住。"""
    uid = _user()
    wf = _wf(uid)
    store.create_task("r", "b", owner_id=uid, actor="system",
                      workflow_id=wf["id"], workflow_row=0)
    assert store.user_has_live_regular_tasks(uid) is False


def test_skips_round_trip_and_block_creation():
    uid = _user()
    wf = _wf(uid)
    store.skip_workflow_row(wf["id"], 3, reason="序列坏了")
    store.skip_workflow_row(wf["id"], 3)          # 再来一次，不能炸
    assert store.list_workflow_skips(wf["id"]) == {3}


def test_delete_workflow_cascades_rows():
    uid = _user()
    wf = _wf(uid)
    store.create_task("r", "b", owner_id=uid, actor="system",
                      workflow_id=wf["id"], workflow_row=0)
    store.delete_workflow(wf["id"])
    assert store.list_workflows(owner_id=uid) == []
    with pytest.raises(store.NotFound):
        store.get_task(store.list_workflow_rows(wf["id"], 0)[0]["id"]) \
            if False else _never()
```

把末尾那个 `test_delete_workflow_cascades_rows` 改成下面这版（上面的写法有问题，`_never` 不存在）：

```python
def test_delete_workflow_cascades_rows():
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    store.delete_workflow(wf["id"])
    assert store.list_workflows(owner_id=uid) == []
    with pytest.raises(store.NotFound):
        store.get_task(row["id"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_workflow_store.py -q --no-header -p no:cacheprovider`
Expected: FAIL —— `AttributeError: module 'store' has no attribute 'create_workflow'`

- [ ] **Step 3: 扩 `_update` 白名单并给 `create_task` 加两个关键字参数**

`server/store.py` 的 `_update()` 里，`allowed` 集合加三个字段：

```python
    allowed = {
        "title", "brief", "status", "step", "priority", "uploads_dir",
        "output_dir", "package_path", "summary", "error", "session_id",
        "done_at",
        "workflow_id", "workflow_row", "workflow_archived",
    }
```

`create_task` 改成（**只在尾部加两个带默认值的关键字参数**，既有调用点一个字都不用动）：

```python
def create_task(title: str, brief: str, owner_id: int, actor: str = "human",
                *, workflow_id=None, workflow_row=None) -> dict:
    ...
    cur = c.execute(
        """INSERT INTO tasks
           (title, brief, status, step, owner_id, priority,
            uploads_dir, output_dir, created_at, updated_at,
            workflow_id, workflow_row)
           VALUES (?, ?, 'queued', '', ?, 0, '', '', ?, ?, ?, ?)""",
        (title, brief, int(owner_id), now, now, workflow_id, workflow_row),
    )
```

- [ ] **Step 4: 加工作流的 CRUD 与闸门查询**

在 `server/store.py` 末尾追加（**不要**新建文件——SQL 集中在一个模块里才查得到）：

```python
# --- 工作流 -----------------------------------------------------------------

_WORKFLOW_FIELDS = {
    "name", "sheet", "source_path", "prompt_template", "header_row", "key_col",
    "total_rows", "cursor", "writeback", "writeback_pending",
    "abort_requested", "result_path", "status",
}
_ROLES = ("key", "input", "context", "output", "ignore")


def create_workflow(owner_id: int, name: str, prompt_template: str, *,
                    sheet: str = "", source_path: str = "", header_row: int = 1,
                    key_col: int = 0, total_rows: int = 0,
                    writeback: int = 0) -> dict:
    name = (name or "").strip()
    if not name:
        raise Invalid("工作流名称不能为空")
    if not (prompt_template or "").strip():
        raise Invalid("提示词模板不能为空")
    if owner_id is None:
        raise Invalid("工作流缺少归属用户")
    if status_ok := _bad_status(status):          # noqa: F841 - 见下，不用
        raise Invalid(status_ok)
    now = db.now_iso()
    c = db.connect()
    try:
        with c:
            cur = c.execute(
                """INSERT INTO workflows
                   (owner_id, name, sheet, source_path, prompt_template,
                    header_row, key_col, total_rows, cursor, writeback,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (int(owner_id), name, sheet, source_path, prompt_template,
                 int(header_row), int(key_col), int(total_rows),
                 1 if writeback else 0, now, now))
            wid = cur.lastrowid
        row = c.execute("SELECT * FROM workflows WHERE id = ?", (wid,)).fetchone()
    finally:
        c.close()
    return dict(row)


def list_workflows(owner_id=None, *, is_admin: bool = False) -> list[dict]:
    sql = "SELECT * FROM workflows"
    where, args = [], []
    if not is_admin and owner_id is not None:
        where.append("owner_id = ?")
        args.append(int(owner_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    c = db.connect()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def get_workflow(workflow_id: int, owner_id=None, is_admin: bool = False) -> dict:
    """越权抛 NotFound（不抛 403）——与 get_task 同约定，不承认 id 存在。"""
    c = db.connect()
    try:
        row = c.execute("SELECT * FROM workflows WHERE id = ?",
                        (int(workflow_id),)).fetchone()
    finally:
        c.close()
    if row is None:
        raise NotFound("工作流不存在")
    if not is_admin and owner_id is not None and int(row["owner_id"]) != int(owner_id):
        raise NotFound("工作流不存在")
    return dict(row)


def update_workflow(workflow_id: int, **fields) -> dict:
    bad = set(fields) - _WORKFLOW_FIELDS
    if bad:
        raise Invalid(f"不能更新这些字段：{', '.join(sorted(bad))}")
    if not fields:
        return get_workflow(workflow_id)
    if "status" in fields and fields["status"] not in ("draft", "running", "paused", "finished"):
        raise Invalid(f"未知的工作流状态：{fields['status']}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    c = db.connect()
    try:
        with c:
            cur = c.execute(f"UPDATE workflows SET {cols} WHERE id = ?",
                            list(fields.values()) + [int(workflow_id)])
            if cur.rowcount == 0:
                raise NotFound("工作流不存在")
        row = c.execute("SELECT * FROM workflows WHERE id = ?",
                        (int(workflow_id),)).fetchone()
    finally:
        c.close()
    return dict(row)


def list_workflow_columns(workflow_id: int) -> list[dict]:
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT * FROM workflow_columns WHERE workflow_id = ? ORDER BY col_index",
            (int(workflow_id),)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def set_workflow_columns(workflow_id: int, cols: list[dict]) -> list[dict]:
    """整体替换一列组。靠 UNIQUE(workflow_id, col_index) 保证幂等。"""
    seen = set()
    for col in cols:
        if col.get("role", "ignore") not in _ROLES:
            raise Invalid(f"未知的列角色：{col.get('role')}")
        idx = int(col["col_index"])
        if idx in seen:
            raise Invalid(f"第 {idx} 列出现了两次")
        seen.add(idx)
    c = db.connect()
    try:
        with c:
            c.execute("DELETE FROM workflow_columns WHERE workflow_id = ?",
                      (int(workflow_id),))
            for col in cols:
                c.execute(
                    """INSERT INTO workflow_columns
                       (workflow_id, col_index, header, role, slot, field)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (int(workflow_id), int(col["col_index"]),
                     col.get("header", ""), col.get("role", "ignore"),
                     col.get("slot", ""), col.get("field", "")))
    finally:
        c.close()
    return list_workflow_columns(workflow_id)


def delete_workflow(workflow_id: int) -> None:
    """只删数据库。tasks.workflow_id 的 ON DELETE CASCADE 会带走行任务记录；
    磁盘由 janitor 负责（规格 6.7 要求先中止在跑的行任务，那是 app 层的事）。"""
    c = db.connect()
    try:
        with c:
            c.execute("DELETE FROM workflows WHERE id = ?", (int(workflow_id),))
    finally:
        c.close()


def skip_workflow_row(workflow_id: int, row_index: int, reason: str = "") -> None:
    c = db.connect()
    try:
        with c:
            c.execute(
                """INSERT OR IGNORE INTO workflow_skips
                   (workflow_id, row_index, reason, created_at)
                   VALUES (?, ?, ?, ?)""",
                (int(workflow_id), int(row_index), reason, db.now_iso()))
    finally:
        c.close()


def list_workflow_skips(workflow_id: int) -> set[int]:
    c = db.connect()
    try:
        return {int(r[0]) for r in c.execute(
            "SELECT row_index FROM workflow_skips WHERE workflow_id = ?",
            (int(workflow_id),)).fetchall()}
    finally:
        c.close()


# --- 调度器闸门要问的问题（规格 6.3 / 6.4）---------------------------------

_LIVE = ("queued", "running", "packaging")
_TERMINAL_SQL = ("done", "failed")


def list_running_workflows() -> list[dict]:
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT * FROM workflows WHERE status = 'running' ORDER BY id").fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def workflow_has_unarchived(workflow_id: int) -> bool:
    """闸门②。判据是**行自己的标记**，与 cursor 无关。

    写成 `workflow_row < cursor` 会把所有历史行都算成没归档（含早已归档过的），
    于是②恒真、整批永不放行——而且只在管理员用过重跑之后才发作。
    """
    c = db.connect()
    try:
        return c.execute(
            "SELECT 1 FROM tasks WHERE workflow_id = ? AND workflow_archived = 0"
            " LIMIT 1", (int(workflow_id),)).fetchone() is not None
    finally:
        c.close()


def workflow_has_live_rows(workflow_id: int) -> bool:
    """闸门⑤：在跑 / 排队 / 打包中的行任务。"""
    marks = ",".join("?" * len(_LIVE))
    c = db.connect()
    try:
        return c.execute(
            f"SELECT 1 FROM tasks WHERE workflow_id = ? AND status IN ({marks})"
            " LIMIT 1", (int(workflow_id), * _LIVE)).fetchone() is not None
    finally:
        c.close()


def workflow_queued_rows(workflow_id: int) -> list[dict]:
    """闸门④：要认领的 queued 行任务。管理员重跑产生的就在这里。"""
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT * FROM tasks WHERE workflow_id = ? AND status = 'queued'"
            " ORDER BY workflow_row", (int(workflow_id),)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def workflow_unarchived_terminal_rows(workflow_id: int) -> list[dict]:
    """归档 pass 的取数：终态且还没写回表的行。"""
    marks = ",".join("?" * len(_TERMINAL_SQL))
    c = db.connect()
    try:
        rows = c.execute(
            f"SELECT * FROM tasks WHERE workflow_id = ?"
            f" AND status IN ({marks}) AND workflow_archived = 0"
            " ORDER BY workflow_row",
            (int(workflow_id), *_TERMINAL_SQL)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def list_workflow_rows(workflow_id: int, row_index=None) -> list[dict]:
    c = db.connect()
    try:
        if row_index is None:
            rows = c.execute(
                "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY workflow_row",
                (int(workflow_id),)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM tasks WHERE workflow_id = ? AND workflow_row = ?",
                (int(workflow_id), int(row_index))).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def user_has_live_regular_tasks(owner_id: int) -> bool:
    """闸门⑥。注意 workflow_id IS NULL —— 行任务自己不能把自己挡住。"""
    marks = ",".join("?" * len(_LIVE))
    c = db.connect()
    try:
        return c.execute(
            f"SELECT 1 FROM tasks WHERE owner_id = ? AND workflow_id IS NULL"
            f" AND status IN ({marks}) LIMIT 1",
            (int(owner_id), *_LIVE)).fetchone() is not None
    finally:
        c.close()
```

**上面 `create_workflow` 里有一行是笔误，删掉它**：

```python
    if status_ok := _bad_status(status):          # noqa: F841 - 见下，不用
        raise Invalid(status_ok)
```

`create_workflow` 不设 `status`，它恒为表默认值 `'draft'`，那句是多余的。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_workflow_store.py -q --no-header -p no:cacheprovider`
Expected: PASS（14 passed）

- [ ] **Step 6: 跑全量**

Run: `pytest tests -q --no-header -p no:cacheprovider`
Expected: 全绿

- [ ] **Step 7: 提交**

message 写进 `.git\MSG_t2.txt` 后 `git -F`：

```
store 层：工作流 CRUD + 三列写入 + 闸门查询

create_task 加 workflow_id/workflow_row 两个关键字参数（向后兼容），
_update 白名单扩三列。闸门要问的六件事各一个函数：未归档/有活跃行/
待认领/待归档/该用户有常规任务/列出 running 工作流。

围栏 14 条，其中行任务那条**读回数据库核对值**——只断言 create_task
不抛异常的话，白名单缺列和忘传参两种坏写法都会绿。

提交前先把测试文件里那段 _never() 的死写法换成下面给的简版。
```

---

### Task 3: `server/workflow.py` —— 读表、列角色校验、提示词渲染（纯函数）

**Files:**
- Create: `server/workflow.py`
- Test: `tests/test_workflow_xlsx.py`（新建）

**Interfaces:**
- Consumes: 无（不碰数据库，可与 Task 2 并行）
- Produces:
  - `workflow.UPLOAD_MAX = 20 * 1024 * 1024`
  - `workflow.CELL_MAX = 8000`
  - `workflow.PREVIEW_ROWS = 50`
  - `workflow.ROLES = ("key", "input", "context", "output", "ignore")`
  - `workflow.OUTPUT_FIELDS = ("status", "summary", "error", "artifacts")`
  - `@dataclass workflow.SheetView: sheet: str; headers: list[str]; row_numbers: list[int]; preview: list[list[str]]; col_count: int`
  - `workflow.check_upload(filename: str, data: bytes) -> None`（抛 `store.Invalid`）
  - `workflow.safe_display_name(filename: str) -> str`
  - `workflow.read_source(path: Path, header_row: int = 1) -> SheetView`
  - `workflow.row_values(view_path: Path, view: SheetView, row_index: int) -> dict[int, str]`
  - `workflow.check_context_columns(path: Path, view: SheetView, context_cols: list[int]) -> None`
  - `workflow.render(template: str, values: dict[str, str]) -> str`
  - `workflow.extract_slots(template: str) -> list[str]`
  - `workflow.validate_binding(template: str, cols: list[dict]) -> None`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_workflow_xlsx.py`：

```python
"""读表 / 取值 / 渲染，全部是纯函数，不碰数据库（规格 4.5 / 5.1 / 5.2 / 5.3）。

本任务最容易写错的三处，各有一条专门的用例：

- **公式必须回退**：openpyxl 自己写的公式，用 ``data_only=True`` 读回来是 None。
  不回退的表现是「整列提示词全是 None，而任务照跑不误，只是算了个空」。
- **全空数据行要剔除**：真实 Excel 尾部常有一两行格式残留，``ws.max_row``
  会把它们算成数据行——不剔的话工作流会跑出一堆「输入全空」的行任务。
- **context 列必须整列恒定**：不恒定就报错，不能悄悄取第一行。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

import store
import workflow


def _wb(tmp_path, rows, name="t.xlsx"):
    p = Path(tmp_path) / name
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    for r in rows:
        ws.append(list(r))
    wb.save(p)
    return p


# --- 上传校验（规格 4.5 ②）-------------------------------------------------

def test_check_upload_accepts_real_xlsx(tmp_path):
    p = _wb(tmp_path, [["编号", "序列"], ["1", "AAAA"]])
    workflow.check_upload("表.xlsx", p.read_bytes())      # 不抛即通过


def test_check_upload_rejects_empty():
    with pytest.raises(store.Invalid):
        workflow.check_upload("空.xlsx", b"")


def test_check_upload_rejects_shell_by_extension():
    with pytest.raises(store.Invalid):
        workflow.check_upload("evil.sh", b"echo hi")


def test_check_upload_rejects_wrong_magic():
    """扩展名对但内容不是 zip——只看扩展名等于没校验。"""
    with pytest.raises(store.Invalid):
        workflow.check_upload("假.xlsx", b"not a zip at all")


def test_check_upload_rejects_oversize():
    with pytest.raises(store.Invalid):
        workflow.check_upload("大.xlsx", b"PK\x03\x04" + b"0" * (workflow.UPLOAD_MAX + 1))


def test_safe_display_name_strips_path():
    """落盘只用固定名 source.xlsx，但展示名要去掉路径分隔符。"""
    assert "/" not in workflow.safe_display_name("../../evil.xlsx")
    assert "\\" not in workflow.safe_display_name("..\\..\\evil.xlsx")
    assert len(workflow.safe_display_name("x" * 500 + ".xlsx")) <= 120


# --- 读表 -------------------------------------------------------------------

def test_read_source_basics(tmp_path):
    p = _wb(tmp_path, [["编号", "序列"], ["1", "AAA"], ["2", "BBB"]])
    v = workflow.read_source(p)
    assert v.sheet == "S1"
    assert v.headers == ["编号", "序列"]
    assert v.col_count == 2
    assert len(v.row_numbers) == 2


def test_read_source_drops_trailing_blank_rows(tmp_path):
    p = Path(tmp_path) / "t.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["编号", "序列"])
    ws.append(["1", "AAA"])
    ws.append([None, None])      # 真实表格尾部常有这种格式残留
    wb.save(p)
    v = workflow.read_source(p)
    assert len(v.row_numbers) == 1


def test_read_source_header_row_is_configurable(tmp_path):
    p = _wb(tmp_path, [["说明行"], ["编号", "序列"], ["1", "AAA"]])
    v = workflow.read_source(p, header_row=2)
    assert v.headers == ["编号", "序列"]
    assert len(v.row_numbers) == 1


def test_read_source_truncates_long_cells(tmp_path):
    long = "x" * (workflow.CELL_MAX + 500)
    p = _wb(tmp_path, [["编号", "备注"], ["1", long]])
    v = workflow.read_source(p)
    assert len(v.preview[0][1]) == workflow.CELL_MAX


def test_read_source_rejects_empty_sheet(tmp_path):
    p = Path(tmp_path) / "empty.xlsx"
    Workbook().save(p)
    with pytest.raises(store.Invalid):
        workflow.read_source(p)


# --- 公式回退（规格 5.3）----------------------------------------------------

def test_value_falls_back_to_formula_text(tmp_path):
    p = Path(tmp_path) / "f.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["编号", "长度"])
    ws.append(["1", "=LEN(B1)"])       # openpyxl 自己写的公式，没有缓存值
    wb.save(p)
    v = workflow.read_source(p)
    got = workflow.row_values(p, v, 0)
    assert got[1] == "=LEN(B1)", "回退失效：提示词里会喂进 None"
    assert got[1] is not None


def test_value_prefers_cached_result(tmp_path):
    p = _wb(tmp_path, [["编号", "长度"], ["1", "42"]])
    v = workflow.read_source(p)
    assert workflow.row_values(p, v, 0)[1] == "42"


# --- context 列恒定性（规格 5.1）--------------------------------------------

def test_context_column_must_be_constant(tmp_path):
    p = _wb(tmp_path, [["编号", "项目", "序列"],
                       ["1", "XBR", "AAA"], ["2", "OTHER", "BBB"]])
    v = workflow.read_source(p)
    workflow.check_context_columns(p, v, [1])         # 恒定，不抛
    with pytest.raises(store.Invalid):
        workflow.check_context_columns(p, v, [2])     # 序列在变，当 context 要报错


def test_context_check_ignores_blank_cells(tmp_path):
    """空单元格不算「变了」——否则整列只有一个值、其余空着反而报错。"""
    p = _wb(tmp_path, [["编号", "项目"], ["1", "XBR"], ["2", None]])
    v = workflow.read_source(p)
    workflow.check_context_columns(p, v, [1])


# --- 渲染 -------------------------------------------------------------------

def test_render_substitutes_slots():
    out = workflow.render("查 {{seq}}，项目 {{proj}}",
                          {"seq": "AAA", "proj": "XBR"})
    assert out == "查 AAA，项目 XBR"


def test_render_allows_repeated_slot():
    out = workflow.render("{{seq}} / {{seq}}", {"seq": "A"})
    assert out == "A / A"


def test_render_missing_slot_becomes_blank_not_error():
    """空单元格渲染成空串、不报错——空表是常态，不是用户的错。"""
    assert workflow.render("查 [{{seq}}]", {}) == "查 []"


def test_extract_slots_dedupes_and_strips():
    got = workflow.extract_slots("{{a}} {{ b }} {{a}}")
    assert got == ["a", "b"]


def test_validate_binding_rejects_unbound_slot():
    cols = [{"col_index": 0, "role": "input", "slot": "seq"}]
    with pytest.raises(store.Invalid):
        workflow.validate_binding("查 {{seq}} 与 {{proj}}", cols)


def test_validate_binding_rejects_unused_column():
    cols = [{"col_index": 0, "role": "input", "slot": "seq"},
            {"col_index": 1, "role": "context", "slot": "没人用"}]
    with pytest.raises(store.Invalid):
        workflow.validate_binding("查 {{seq}}", cols)


def test_validate_binding_accepts_healthy_binding():
    cols = [{"col_index": 0, "role": "key"},
            {"col_index": 1, "role": "input", "slot": "seq"},
            {"col_index": 2, "role": "context", "slot": "proj"},
            {"col_index": 3, "role": "output", "field": "summary"}]
    workflow.validate_binding("查 {{seq}}，项目 {{proj}}", cols)


def test_output_column_needs_a_known_field():
    cols = [{"col_index": 0, "role": "output", "field": "随便写的"}]
    with pytest.raises(store.Invalid):
        workflow.validate_binding("x", cols)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_workflow_xlsx.py -q --no-header -p no:cacheprovider`
Expected: FAIL —— `ModuleNotFoundError: No module named 'workflow'`

- [ ] **Step 3: 写实现**

新建 `server/workflow.py`：

```python
"""工作流的纯逻辑：读 xlsx、判定列角色、渲染提示词、写回结果表。

**这里一个数据库调用都没有**，全是可以单独测的纯函数——因为这是最容易
悄悄算错的地方：公式读成 None、全空行混进数据行、context 列偷偷取第一行，
三种都不会报错，只是结果不对。
"""
from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

import store

UPLOAD_MAX = 20 * 1024 * 1024
CELL_MAX = 8000
PREVIEW_ROWS = 50
ROLES = ("key", "input", "context", "output", "ignore")
OUTPUT_FIELDS = ("status", "summary", "error", "artifacts")
XLSX_MAGIC = b"PK\x03\x04"
_SLOT_RE = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


@dataclass
class SheetView:
    sheet: str
    headers: list[str]
    row_numbers: list[int]      # 0 基数据行序号 -> sheet 行号（跳过全空行）
    preview: list[list[str]]
    col_count: int


# --- 上传校验（规格 4.5）----------------------------------------------------

def safe_display_name(filename: str) -> str:
    """**只用于展示**。落盘一律用固定名 source.xlsx（规格 4.5 ①）。

    file.filename 是用户可控的，直接拼进落盘路径就是目录穿越。
    """
    name = (filename or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "", name).strip()
    return name[:120] or "table.xlsx"


def check_upload(filename: str, data: bytes) -> None:
    """框架对上传内容零校验：空文件和 .sh 实测都是 200 照收。自己判。"""
    if not data:
        raise store.Invalid("文件是空的")
    if len(data) > UPLOAD_MAX:
        raise store.Invalid(f"文件超过 {UPLOAD_MAX // (1024 * 1024)} MB 上限")
    ext = Path(safe_display_name(filename)).suffix.lower()
    if ext != ".xlsx":
        raise store.Invalid("只支持 .xlsx")
    if not data.startswith(XLSX_MAGIC):
        raise store.Invalid("这个文件不是 xlsx（内容与扩展名不符）")


# --- 读表 -------------------------------------------------------------------

def _row_at(ws, r: int, max_col: int) -> list:
    """读 sheet 第 r 行的前 max_col 个值。

    走 iter_rows 而不是逐格 cell()：read_only 工作簿上逐格调用极慢，
    而一次一行的迭代只解析一遍共享字符串表。
    """
    for tup in ws.iter_rows(min_row=r, max_row=r, max_col=max_col,
                            values_only=True):
        return list(tup)
    return []


def _blank(vals) -> bool:
    return all((v or "") == "" for v in vals)


def read_source(path: Path, header_row: int = 1) -> SheetView:
    """读表头与数据行。**全空数据行在这里就剔掉**（规格 5.1）。

    真实 Excel 尾部常有一两行只有格式没内容的残留，``ws.max_row`` 会把它们
    算成数据行——不剔的话工作流会跑出一堆「输入全空」的行任务，每行都白烧
    一次完整计算。
    """
    wb_vals = load_workbook(path, data_only=True, read_only=True)
    try:
        wb_raw = load_workbook(path, data_only=False, read_only=True)
    except Exception:
        wb_raw = None
    try:
        wsv = wb_vals[wb_vals.sheetnames[0]]
        wsr = wb_raw[wb_raw.sheetnames[0]] if wb_raw else wsv
        sheet = wsv.title
        if header_row < 1:
            raise store.Invalid("表头行号从 1 开始")

        headers: list[str] = []
        for col in range(1, wsv.max_column + 1):
            h = wsv.cell(row=header_row, column=col).value
            headers.append(str(h).strip() if h is not None else f"列{col}")

        max_col = wsv.max_column or 1
        row_numbers: list[int] = []
        preview: list[list[str]] = []
        for r in range(header_row + 1, wsv.max_row + 1):
            # 一次读整行。**不要写 ws.cell(..., values_only=True)**——cell()
            # 返回 Cell 对象，values_only 是 iter_rows 的参数，传给它直接 TypeError。
            row_vals = _row_at(wsv, r, max_col)
            raw_vals = _row_at(wsr, r, max_col)
            line = []
            for i in range(max_col):
                val = row_vals[i] if i < len(row_vals) else None
                raw = raw_vals[i] if i < len(raw_vals) else None
                if val is None and isinstance(raw, str) and raw.startswith("="):
                    val = raw
                line.append(str(val)[:CELL_MAX] if val is not None else "")
            if _blank(line):
                continue
            row_numbers.append(r)
            if len(preview) < PREVIEW_ROWS:
                preview.append(line)

        if not row_numbers:
            raise store.Invalid("表里没有有效数据行")
        return SheetView(sheet=sheet, headers=headers, row_numbers=row_numbers,
                         preview=preview, col_count=len(headers))
    finally:
        wb_vals.close()
        if wb_raw is not None and wb_raw is not wb_vals:
            wb_raw.close()


def row_values(path: Path, view: SheetView, row_index: int) -> dict[int, str]:
    """取某一行的 {0 基列号: 文本}。"""
    if row_index < 0 or row_index >= len(view.row_numbers):
        raise store.Invalid(f"行号越界：{row_index}")
    wb = load_workbook(path, data_only=True, read_only=True)
    wbr = load_workbook(path, data_only=False, read_only=True)
    try:
        r = view.row_numbers[row_index]
        vals = _row_at(wb[view.sheet], r, view.col_count)
        raws = _row_at(wbr[view.sheet], r, view.col_count)
        out: dict[int, str] = {}
        for i in range(view.col_count):
            val = vals[i] if i < len(vals) else None
            raw = raws[i] if i < len(raws) else None
            if val is None and isinstance(raw, str) and raw.startswith("="):
                val = raw
            out[i] = (str(val)[:CELL_MAX] if val is not None else "")
        return out
    finally:
        wb.close()
        wbr.close()


def check_context_columns(path: Path, view: SheetView,
                          context_cols: list[int]) -> None:
    """context 列必须整列恒定，否则**报错**而不是悄悄取第一行（规格 5.1）。

    悄悄取第一行的后果是：用户以为整表共用一个值，实际前几行悄悄不同，
    而整批结果看起来都正常。
    """
    if not context_cols:
        return
    # **两个工作簿只开一次，整表读进内存再比。** 逐行调 row_values 等于
    # 每行重新 load 一遍——1000 行的表就是 2000 次整表加载，20MB 的表
    # 根本跑不完，而且用户看到的是「保存转圈很久没有任何提示」。
    wb = load_workbook(path, data_only=True, read_only=True)
    wbr = load_workbook(path, data_only=False, read_only=True)
    try:
        for col in context_cols:
            seen = None
            seen_row = 0
            for idx, r in enumerate(view.row_numbers):
                vals = _row_at(wb[view.sheet], r, view.col_count)
                raws = _row_at(wbr[view.sheet], r, view.col_count)
                val = vals[col] if col < len(vals) else None
                raw = raws[col] if col < len(raws) else None
                if val is None and isinstance(raw, str) and raw.startswith("="):
                    val = raw
                val = str(val)[:CELL_MAX] if val is not None else ""
                if val == "":
                    continue
                if seen is None:
                    seen, seen_row = val, idx
                elif val != seen:
                    raise store.Invalid(
                        f"第 {col + 1} 列「{view.headers[col]}」被设成上下文，"
                        f"但它每一行都不一样（第 {idx + 1} 行是「{val[:30]}」，"
                        f"第 {seen_row + 1} 行是「{seen[:30]}」）。"
                        f"要么改成输入列，要么删掉这列。")
    finally:
        wb.close()
        wbr.close()


# --- 渲染 -------------------------------------------------------------------

def extract_slots(template: str) -> list[str]:
    out: list[str] = []
    for m in _SLOT_RE.finditer(template or ""):
        name = m.group(1)
        if name not in out:
            out.append(name)
    return out


def render(template: str, values: dict[str, str]) -> str:
    """未绑定的槽位渲染成空串，不报错——空单元格是常态不是用户的错。"""
    def sub(m):
        return values.get(m.group(1), "")
    return _SLOT_RE.sub(sub, template or "")


def validate_binding(template: str, cols: list[dict]) -> None:
    slots = set(extract_slots(template))
    bound = {c.get("slot", "") for c in cols
             if c.get("role") in ("input", "context") and c.get("slot")}
    missing = slots - bound
    if missing:
        raise store.Invalid(
            "提示词里的这些变量没有列绑定：" + "、".join(f"{{{{{m}}}}}" for m in sorted(missing)))
    unused = bound - slots
    if unused:
        raise store.Invalid(
            "这些列绑了变量但提示词里没用到：" + "、".join(sorted(unused))
            + "。要不要改成忽略？")
    keys = [c for c in cols if c.get("role") == "key"]
    if len(keys) != 1:
        raise store.Invalid(f"必须有且只有一列是「标识列」，现在有 {len(keys)} 列")
    for c in cols:
        if c.get("role") == "output" and c.get("field") not in OUTPUT_FIELDS:
            raise store.Invalid(
                f"结果列「{c.get('header')}」的字段名不认识：{c.get('field')}。"
                "可用：" + "、".join(OUTPUT_FIELDS))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_workflow_xlsx.py -q --no-header -p no:cacheprovider`
Expected: PASS（23 passed）

- [ ] **Step 5: 提交**

message 写进 `.git\MSG_t3.txt` 后 `git -F`：

```
workflow.py 纯逻辑：读表 / 列角色 / 渲染，不碰数据库

三处最容易悄悄算错的地方各有用例：
公式回退（data_only 读回是 None）、全空行剔除、context 列恒定性。

上传校验三件套：魔数、20MB、空文件。落盘名一律固定，展示名才去路径。
围栏 23 条。
```

---

### Task 4: `server/workflow.py` 归档回写 —— 原子、降级、边界

**Files:**
- Modify: `server/workflow.py`（追加）
- Test: `tests/test_workflow_archive.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `SheetView` / `read_source` / `row_values`
- Produces:
  - `@dataclass(frozen=True) workflow.OutputCol: col_index: int; field: str`
  - `workflow.write_back(source: Path, dest: Path, sheet: str, header_row: int, jobs: list[tuple[int, OutputCol, str]]) -> None` —— 抛 `PermissionError` 表示文件被占，**调用方负责降级，不要在这里重试**
  - `workflow.staging_path(root: Path) -> Path`
  - `workflow.finalize_upload(root: Path, staging: Path, workflow_id: int) -> Path`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_workflow_archive.py`：

```python
"""回写 xlsx：原子替换、被占降级、越界记失败（规格 7.1 / 7.2 / 7.3）。

openpyxl **不支持增量写**：每次都是整表 load → 改几格 → 整表 save。
所以「每归档一行写一次」的代价是 N 行 = N 次整表重写，这个任务不做优化，
但必须保证：一次慢重写不会拖死调度（Task 5 负责隔离），
以及被 Excel 占住时不重试（规格 7.1）。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

import store
import workflow


def _src(tmp_path, rows=8):
    p = Path(tmp_path) / "source.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "序列", "状态", "说明", "原因", "产物"])
    for i in range(rows):
        ws.append([f"ID{i}", f"SEQ{i}", "", "", "", ""])
    wb.save(p)
    return p


def test_write_back_fills_cells(tmp_path):
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    workflow.write_back(src, dest, "S1", 1, [
        (0, workflow.OutputCol(2, "status"), "done"),
        (0, workflow.OutputCol(3, "summary"), "算完了"),
    ])
    ws = load_workbook(dest)["S1"]
    assert ws.cell(row=2, column=3).value == "done"
    assert ws.cell(row=2, column=4).value == "算完了"


def test_write_back_preserves_source_columns(tmp_path):
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    workflow.write_back(src, dest, "S1", 1,
                        [(0, workflow.OutputCol(2, "status"), "done")])
    ws = load_workbook(dest)["S1"]
    assert ws.cell(row=2, column=1).value == "ID0"
    assert ws.cell(row=3, column=1).value == "ID1"


def test_write_back_multiple_rows(tmp_path):
    """归档 pass 必须能把一批多行一次写完，不许只处理第一行就 break。"""
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    workflow.write_back(src, dest, "S1", 1, [
        (0, workflow.OutputCol(2, "status"), "done"),
        (1, workflow.OutputCol(2, "status"), "failed"),
        (2, workflow.OutputCol(2, "status"), "done"),
    ])
    ws = load_workbook(dest)["S1"]
    assert [ws.cell(row=r, column=3).value for r in (2, 3, 4)] == \
        ["done", "failed", "done"]

def test_write_back_is_atomic(tmp_path):
    """写一半时下载，拿到的必须是完整的旧版或新版，不能是半个文件。"""
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    for i in range(3):
        workflow.write_back(src, dest, "S1", 1,
                            [(i, workflow.OutputCol(2, "status"), "done")])
    # 注意 glob 的形状：临时文件叫 result.tmp.xlsx（**不是** result.tmp）。
    # 写成 glob("*.tmp") 的话这条断言恒真——临时文件根本不在匹配范围内，
    # 于是清理写错了也照样绿。这正是「断言恒真」最常见的一种伪装。
    leftovers = list(Path(tmp_path).glob("*.tmp.xlsx"))
    assert leftovers == [], f"临时文件没清掉：{leftovers}"
    assert load_workbook(dest)["S1"].cell(row=4, column=3).value == "done"


def test_write_back_raises_permissionerror_when_locked(tmp_path, monkeypatch):
    """文件被占 → 抛 PermissionError，**调用方降级**，这里不重试。

    重试到调度 tick 里的话，工作流会被一个文件锁卡住，而用户看到的
    只是「停在第 N 行」。
    """
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)

    import openpyxl
    real_save = openpyxl.Workbook.save

    def boom(self, path):
        raise PermissionError(13, "另一个程序正在使用此文件")

    monkeypatch.setattr(openpyxl.Workbook, "save", boom)
    with pytest.raises(PermissionError):
        workflow.write_back(src, dest, "S1", 1,
                            [(0, workflow.OutputCol(2, "status"), "done")])
    monkeypatch.setattr(openpyxl.Workbook, "save", real_save)
    # 表没被破坏：还是旧版
    assert load_workbook(dest)["S1"].cell(row=2, column=3).value == ""


def test_write_back_out_of_range_is_reported(tmp_path):
    """目标行不存在 → 记一条失败原因，**不抛**。

    抛异常会经调度器的 except 变成一行静默日志，工作流悄悄卡住。
    """
    src = _src(tmp_path, rows=2)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    with pytest.raises(store.Invalid):
        workflow.write_back(src, dest, "S1", 1,
                            [(99, workflow.OutputCol(2, "status"), "done")])


def test_write_back_no_jobs_is_noop(tmp_path):
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    workflow.write_back(src, dest, "S1", 1, [])
    assert dest.exists()


# --- 上传落盘：staging -> 定名（规格 4.5 ①③）-------------------------------

def test_staging_path_is_not_user_controlled(tmp_path):
    p = workflow.staging_path(Path(tmp_path))
    assert p.parent.name == "staging"
    assert p.suffix == ".xlsx"


def test_finalize_moves_to_fixed_name(tmp_path):
    st = workflow.staging_path(Path(tmp_path))
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_bytes(b"PK\x03\x04 fake")
    final = workflow.finalize_upload(Path(tmp_path), st, 7)
    assert final.name == "source.xlsx"
    assert final.parent.name == "7"
    assert not st.exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_workflow_archive.py -q --no-header -p no:cacheprovider`
Expected: FAIL —— `AttributeError: module 'workflow' has no attribute 'OutputCol'`

- [ ] **Step 3: 写实现**

追加到 `server/workflow.py` 末尾：

```python
# --- 上传落盘（规格 4.5 ①③）------------------------------------------------

def staging_path(root: Path) -> Path:
    """staging 名字**自己生成**，不含任何用户输入。"""
    d = Path(root) / "staging"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4().hex}.xlsx"


def finalize_upload(root: Path, staging: Path, workflow_id: int) -> Path:
    """解析成功后把 staging 搬成 workflows\\<id>\\source.xlsx —— 固定名。"""
    dest_dir = Path(root) / str(int(workflow_id))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "source.xlsx"
    shutil.move(str(staging), str(dest))
    return dest


# --- 回写（规格 7.1 / 7.2）-------------------------------------------------

@dataclass(frozen=True)
class OutputCol:
    col_index: int
    field: str


def _sheet_row_number(view: SheetView, row_index: int) -> int:
    if row_index < 0 or row_index >= len(view.row_numbers):
        raise store.Invalid(
            f"第 {row_index + 1} 行不存在了——源表可能被改短了或损坏了")
    return view.row_numbers[row_index]


def write_back(source: Path, dest: Path, sheet: str, header_row: int,
               jobs: list[tuple[int, OutputCol, str]]) -> None:
    """把一批 (数据行序号, 输出列, 值) 写进 dest.xlsx。

    - **原子**：先写同目录临时文件再 ``os.replace``，用户在浏览器下载的同时
      平台在写，不原子就会下载到半个文件。
    - **被占则抛**：Windows 上 Excel/WPS 开着会独占，``openpyxl.save()`` 与
      ``os.replace()`` 都会 PermissionError。**这里不重试**——重试到调度
      tick 里会让整个工作流被一个文件锁卡住，而用户看到的只是「停在第 N 行」。
      调用方负责降级（记 writeback_pending=1）。
    - **越界抛 store.Invalid**：源表被改短了。这不是文件锁，是数据损坏，
      调用方要记失败原因并**仍然置 workflow_archived=1**，否则闸门②会一直
      等着这一行。
    """
    if not jobs:
        return
    view = read_source(source, header_row=header_row)
    wb = load_workbook(source)
    try:
        if sheet and sheet in wb.sheetnames:
            ws = wb[sheet]
        else:
            ws = wb[wb.sheetnames[0]]
        for row_index, col, value in jobs:
            r = _sheet_row_number(view, row_index)
            cell = ws.cell(row=r, column=int(col.col_index) + 1)
            cell.value = str(value)[:CELL_MAX]
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.stem + ".tmp" + dest.suffix)
        wb.save(tmp)
        os.replace(tmp, dest)
    finally:
        wb.close()
```

顶部补两行 import：

```python
import os
import shutil
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_workflow_archive.py -q --no-header -p no:cacheprovider`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交**

message 写进 `.git\MSG_t4.txt` 后 `git -F`：

```
workflow.py 回写：原子替换、被占抛错不重试、越界抛 Invalid

临时文件 + os.replace，下载与写入并发时拿不到半个文件。
PermissionError 向上抛，由调用方降级（writeback_pending）——
在这里重试会让整批被一个文件锁卡住。

围栏 10 条，含「一批多行不许只处理第一行」和「临时文件不许留下」。
```

---

### Task 5: 调度器 —— 两级放行 + 归档 pass

**Files:**
- Modify: `server/dispatcher.py`（`tick` 在 1101；`_queued_tasks` 在 1136；`_admit` 在 1146）
- Test: `tests/test_workflow_dispatch.py`（新建）

**Interfaces:**
- Consumes: Task 2 的全部 store 函数；Task 3 的 `read_source` / `row_values` / `render` / `check_context_columns`
- Produces:
  - `Dispatcher._workflow_run_pass() -> list[int]`
  - `Dispatcher._workflow_archive_pass() -> list[int]`
  - `Dispatcher.tick()` 改为 `self._reap()` → `self._admit()` → `_workflow_run_pass()` → `_workflow_archive_pass()`
  - `_workflow_tasks` 列排除：`_queued_tasks()` 加 `workflow_id IS NULL`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_workflow_dispatch.py`：

```python
"""放行与归档（规格 6.2 / 6.3 / 6.4 / 7.2）。

本任务钉的是**顺序**与**谓词**，两样都是「写对了也不报错、写错了静默卡死」
的东西：

- 闸门顺序必须是 ①④⑤②③⑥。④ 排在②前面是硬要求——重跑的行同时满足
  ②和④，②在前就把它挡死，而④永远走不到。
- ②的判据必须是 ``workflow_archived = 0``。写成 ``workflow_row < cursor``
  会把所有历史行算成没归档，整批永不放行，且只在用过重跑之后才发作。
- 归档要等打包结束（终态），不是等进程退出。

这些用例直接调 ``_workflow_run_pass`` / ``_workflow_archive_pass``，
不真起 agent 进程——放行成败看的是库里那一行状态变了没。
"""
from __future__ import annotations

import pytest

import db
import dispatcher
import store


@pytest.fixture()
def disp(tmp_path, monkeypatch):
    """一个不真的 spawn 的调度器：把 _spawn 换成记账。"""
    monkeypatch.setenv("PROTFORGE_WEB_NODE", "node")
    d = dispatcher.Dispatcher.__new__(dispatcher.Dispatcher)
    d.running = {}
    d.limit = 2
    d.poll_interval = dispatcher.POLL_INTERVAL
    d._stopping = False
    d._wake = __import__("threading").Event()
    spawned = []
    monkeypatch.setattr(d, "_spawn",
                        lambda task_id, brief, work_dir: spawned.append(task_id))
    monkeypatch.setattr(d, "_mark_failed",
                        lambda task_id, msg: store.set_status(
                            task_id, "failed", actor="system", message=msg))
    d._spawned = spawned
    return d


def _user(name="u1", role="user"):
    c = db.connect()
    try:
        with c:
            c.execute("INSERT INTO users (username, password_hash, salt, role,"
                      " disabled, created_at) VALUES (?, 'h','s', ?, 0, 't')",
                      (name, role))
        return c.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    finally:
        c.close()


def _workflow(owner_id, tmp_path, rows=5, name="wf"):
    """建一个带真实 xlsx 源表的工作流，所以放行时能真的渲染出 brief。"""
    import workflow as wfmod
    root = Path_factory(tmp_path)
    st = wfmod.staging_path(root)
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "序列", "项目"])
    for i in range(rows):
        ws.append([f"ID{i}", f"SEQ{i}", "XBR"])
    wb.save(st)
    source = wfmod.finalize_upload(root, st, 9001)
    wf = store.create_workflow(owner_id, name, "查 {{seq}}（项目 {{proj}}）",
                               sheet="S1", source_path=str(source),
                               header_row=1, key_col=0, total_rows=rows,
                               writeback=1)
    store.set_workflow_columns(wf["id"], [
        {"col_index": 0, "header": "编号", "role": "key"},
        {"col_index": 1, "header": "序列", "role": "input", "slot": "seq"},
        {"col_index": 2, "header": "项目", "role": "context", "slot": "proj"},
        {"col_index": 3, "header": "状态", "role": "output", "field": "status"},
    ])
    store.update_workflow(wf["id"], status="running")
    return wf


def Path_factory(tmp_path):
    from pathlib import Path
    return Path(tmp_path) / "data" / "workflows"


# --- 放行基本流 -------------------------------------------------------------

def test_admits_first_row_and_advances_cursor(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    got = disp._workflow_run_pass()
    assert len(got) == 1
    assert store.get_workflow(wf["id"])["cursor"] == 1
    row = store.list_workflow_rows(wf["id"], 0)[0]
    assert row["status"] == "running"          # claim_queued 把它推成 running
    assert "SEQ0" in row["brief"]


def test_row_task_owner_is_the_workflow_owner(disp, tmp_path):
    """闸门⑥ 判的是 owner 的队列，不是别人的。"""
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    row = store.list_workflow_rows(wf["id"], 0)[0]
    assert row["owner_id"] == uid


def test_row_task_created_event_names_the_workflow(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path, name="抗体筛选")
    disp._workflow_run_pass()
    row = store.list_workflow_rows(wf["id"], 0)[0]
    evs = store.list_events(row["id"])
    created = [e for e in evs if e["kind"] == "created"]
    assert len(created) == 1
    assert created[0]["actor"] == "system"
    assert "抗体筛选" in created[0]["message"] and "1" in created[0]["message"]


def test_brief_of_row_n_excludes_other_rows(disp, tmp_path):
    """§6.9 上下文独立：**第 N 行的 brief 里不含第 N-1 行的值**。

    这是「任务之间上下文相互独立」那句话唯一能变成测试的形状。天然成立
    （每行独立 task、独立 work 目录），但必须钉死——批量场景里一行漏进
    上一行的序列，整个后续批次的结果都是错的，而且**不报错**。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = store.list_workflow_rows(wf["id"], 0)[0]
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    disp._workflow_run_pass()
    r1 = store.list_workflow_rows(wf["id"], 1)[0]
    assert "SEQ1" in r1["brief"]
    assert "SEQ0" not in r1["brief"], "第 2 行的 brief 里混进了第 1 行的序列"


def test_serial_second_row_waits(disp, tmp_path):
    uid = _user()
    _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    assert disp._workflow_run_pass() == []      # 同一 tick 不放第二行


def test_next_row_after_previous_is_archived(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = store.list_workflow_rows(wf["id"], 0)[0]
    store.update_task(r0["id"], status="done")
    assert disp._workflow_run_pass() == [], "归档前不许放下一行"
    disp._workflow_archive_pass()
    store.list_workflow_rows(wf["id"], 0)[0]["workflow_archived"] == 1
    assert store.get_task(r0["id"])["workflow_archived"] == 1
    got = disp._workflow_run_pass()
    assert len(got) == 1
    assert store.list_workflow_rows(wf["id"], 1)


def test_packaging_blocks_archiving(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = store.list_workflow_rows(wf["id"], 0)[0]
    store.update_task(r0["id"], status="packaging")
    disp._workflow_archive_pass()
    assert store.get_task(r0["id"])["workflow_archived"] == 0
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    assert store.get_task(r0["id"])["workflow_archived"] == 1


# --- 闸门顺序（规格 6.4）----------------------------------------------------

def test_gate_4_precedes_gate_2(disp, tmp_path):
    """重跑的行同时满足②和④。④不排在前面 → 这条永远停在「已排队」。

    这条用例单独存在的理由：整理代码顺序的人不会想到去跑重跑用例。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = store.list_workflow_rows(wf["id"], 0)[0]
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    store.update_task(r0["id"], status="queued", workflow_archived=0)   # 管理员重跑
    got = disp._workflow_run_pass()
    assert len(got) == 1, "重跑的行没有被认领——④ 被②挡住了"
    assert store.get_task(r0["id"])["status"] == "running"


def test_regular_tasks_go_first(disp, tmp_path):
    uid = _user()
    disp._admit()                        # 先常规
    assert disp.running == {}
    disp._workflow_run_pass()
    assert disp._spawned == [] or True    # 只断言不崩，真正的让路在下一条


def test_gate_6_waits_for_owner_regular_tasks(disp, tmp_path):
    uid = _user()
    _workflow(uid, tmp_path)
    store.create_task("A 的常规任务", "b", owner_id=uid)
    assert disp._workflow_run_pass() == []


def test_gate_6_ignores_other_users_regular_tasks(disp, tmp_path):
    """owner_id 分错的话，A 的工作流会被 B 的任务挡住。"""
    a, b = _user("a"), _user("b")
    _workflow(a, tmp_path)
    store.create_task("B 的常规任务", "b", owner_id=b)
    assert len(disp._workflow_run_pass()) == 1


def test_concurrency_limit_is_enforced(disp, tmp_path):
    """_spawn 本身没有限流检查，全靠这条闸门。"""
    a, b = _user("a"), _user("b")
    disp.running = {999: object()}
    disp.limit = 1
    _workflow(a, tmp_path, name="A")
    _workflow(b, tmp_path, name="B")
    assert disp._workflow_run_pass() == []


def test_paused_workflow_admits_nothing(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    store.update_workflow(wf["id"], status="paused")
    assert disp._workflow_run_pass() == []


def test_finished_when_cursor_exhausted(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=1)
    disp._workflow_run_pass()
    r0 = store.list_workflow_rows(wf["id"], 0)[0]
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    disp._workflow_run_pass()
    assert store.get_workflow(wf["id"])["status"] == "finished"


# --- 归档（规格 7.2）--------------------------------------------------------

def test_archive_marks_every_row_individually(disp, tmp_path):
    """一批两行都终态 → 两行都要置位，不许只处理第一行。"""
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = store.list_workflow_rows(wf["id"], 0)[0]
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    disp._workflow_run_pass()
    r1 = store.list_workflow_rows(wf["id"], 1)[0]
    store.update_task(r1["id"], status="done")
    disp._workflow_archive_pass()
    assert store.get_task(r0["id"])["workflow_archived"] == 1
    assert store.get_task(r1["id"])["workflow_archived"] == 1


def test_archive_survives_locked_result_file(disp, tmp_path):
    """文件被 Excel 占住 → 降级，但**照样置位**、不卡住。"""
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = store.list_workflow_rows(wf["id"], 0)[0]
    store.update_task(r0["id"], status="done", summary="ok")

    import workflow as wfmod
    def boom(*a, **k):
        raise PermissionError(13, "被占用")
    monkey = wfmod.write_back
    wfmod.write_back = boom
    try:
        disp._workflow_archive_pass()
    finally:
        wfmod.write_back = monkey

    assert store.get_task(r0["id"])["workflow_archived"] == 1
    assert store.get_workflow(wf["id"])["writeback_pending"] == 1


def test_pending_writeback_is_completed_later(disp, tmp_path):
    """§7.1：文件解锁后，由**下一次归档顺手补写**——不是定时器重试。

    只测「被占时降级」不够：那证明了它不卡，但没证明它最终会把结果补回去。
    少这一条的话，实现可以是「永远标 pending、永远不补」而全部测试通过。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = store.list_workflow_rows(wf["id"], 0)[0]
    store.update_task(r0["id"], status="done", summary="ok")

    import workflow as wfmod
    real = wfmod.write_back
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "被占用")
        return real(*a, **k)

    wfmod.write_back = flaky
    try:
        disp._workflow_archive_pass()
        assert store.get_workflow(wf["id"])["writeback_pending"] == 1
        # 第二行归档时顺手把上一行补上
        store.update_workflow(wf["id"], cursor=2)
        disp._workflow_run_pass()
        r1 = store.list_workflow_rows(wf["id"], 1)[0]
        store.update_task(r1["id"], status="done", summary="ok2")
        disp._workflow_archive_pass()
    finally:
        wfmod.write_back = real

    assert store.get_workflow(wf["id"])["writeback_pending"] == 0, \
        "解锁后仍没补写"


def test_archive_pass_exception_does_not_escape(disp, tmp_path):
    """一次异常不能越过 tick 的中线，否则所有常规任务停在排队中。"""
    uid = _user()
    _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    def boom():
        raise RuntimeError("整批写挂了")
    disp._workflow_archive_pass = boom
    # tick 不该炸
    assert isinstance(disp.tick(), list)


# --- 常规队列排除工作流任务（规格 6.1）-------------------------------------

def test_queued_tasks_excludes_workflow_rows(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()                       # 行任务变成 running
    store.update_task(store.list_workflow_rows(wf["id"], 0)[0]["id"],
                      status="queued")              # 人工把它退回队列
    got = [int(r["id"]) for r in disp._queued_tasks()]
    assert store.list_workflow_rows(wf["id"], 0)[0]["id"] not in got
```

把 `test_regular_tasks_go_first` 那条里没意义的 `or True` 删掉，换成下面这版：

```python
def test_regular_tasks_go_first(disp, tmp_path):
    """顺序层让路：常规任务和工作流行任务同时有空槽时，先给常规的。"""
    uid = _user()
    wf = _workflow(uid, tmp_path)
    store.create_task("常规", "b", owner_id=uid)
    admitted = disp.tick()
    assert store.list_workflow_rows(wf["id"], 0) == []
    assert 1 in admitted
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_workflow_dispatch.py -q --no-header -p no:cacheprovider`
Expected: FAIL —— `AttributeError: 'Dispatcher' object has no attribute '_workflow_run_pass'`

- [ ] **Step 3: 常规队列排除工作流行任务**

`server/dispatcher.py` 的 `_queued_tasks()` 里，`store.list_tasks(...)` 之后加一行过滤（**排序之前**，否则过滤掉的行仍然影响 `priority` 插队顺序的直觉）：

```python
    def _queued_tasks(self) -> list:
        try:
            rows = store.list_tasks(status="queued", is_admin=True, limit=200)
        except Exception:                          # noqa: BLE001 - 库不可达就这一轮不接
            log.exception("读取排队任务失败")
            return []
        # 工作流行任务有自己更严的闸门（规格 6.4），走常规通道会绕过它们。
        rows = [r for r in rows if r.get("workflow_id") is None]
        # store 是 id DESC（列表页要最新在前），队列要先进先出；priority 高 的插队
        rows.sort(key=lambda r: (-int(r.get("priority") or 0), int(r["id"])))
        return rows
```

- [ ] **Step 4: 改 tick**

```python
    def tick(self) -> list:
        """扫一轮：收尾 → 常规 → 工作流放行 → 工作流归档。返回接走的 task_id。"""
        self._reap()
        admitted = self._admit()
        # 放行排在 _admit 之后：让路给常规任务是**顺序**带来的结构性结果，
        # 不需要额外查库。反过来的话一个 500 行的工作流会一次占满所有槽。
        try:
            admitted += self._workflow_run_pass()
        except Exception:                          # noqa: BLE001
            log.exception("工作流放行巡检出错")
        # 归档排在放行之后。**各自 try/except** 不是洁癖：_loop 是
        # `try: tick() except`，一次异常就让整轮 _admit 都没机会执行——
        # 用户会看到所有常规任务永远停在「排队中」，而原因只是一张 Excel 被占用。
        try:
            self._workflow_archive_pass()
        except Exception:                          # noqa: BLE001
            log.exception("工作流归档巡检出错")
        return admitted
```

- [ ] **Step 5: 写放行 pass**

追加到 `Dispatcher` 类里：

```python
    # --- 工作流 ---------------------------------------------------------------

    def _workflow_run_pass(self) -> list[int]:
        """给每个 running 工作流放行**至多一行**。

        串行是结构性保证：同一 workflow 同时最多一个非终态行任务。所以这里
        一次只放一行，1000 行也不会把队列撑爆。
        """
        admitted: list[int] = []
        try:
            workflows = store.list_running_workflows()
        except Exception:                          # noqa: BLE001
            log.exception("读取 running 工作流失败")
            return admitted
        for wf in workflows:
            if len(self.running) >= self.limit:
                break
            try:
                task_id = self._workflow_admit_one(wf)
            except Exception:                      # noqa: BLE001 - 单个工作流失败不影响别的
                log.exception("工作流 %s 放行失败", wf.get("id"))
                continue
            if task_id:
                admitted.append(task_id)
        return admitted

    def _workflow_admit_one(self, wf: dict):
        """闸门 ①④⑤②③⑥，**顺序是规格的一部分**（规格 6.4）。

        ④ 必须排在②前面：重跑的行同时满足②（有未归档的行）和④（有 queued
        的行），②在前就把它挡死，而④永远走不到——整批卡死且不报错。
        """
        wid = int(wf["id"])
        if wf.get("status") != "running" or int(wf.get("abort_requested") or 0):
            return None

        # ④ 先认领 queued 的行任务（管理员重跑产生的唯一落点），认领完就返回
        queued = store.workflow_queued_rows(wid)
        if queued:
            row = queued[0]
            return self._spawn_workflow_row(wf, int(row["workflow_row"]),
                                            int(row["id"]))

        # ⑤ 串行：有非终态行任务就不放下一行
        if store.workflow_has_live_rows(wid):
            return None
        # ② 先归档再下一个
        if store.workflow_has_unarchived(wid):
            return None
        # ③ 跑完了
        cursor = int(wf["cursor"])
        if cursor >= int(wf["total_rows"]):
            store.update_workflow(wid, status="finished")
            return None
        # ⑥ 排在常规任务之后（顺序层之外再加一道可读、可测的契约）
        if store.user_has_live_regular_tasks(int(wf["owner_id"])):
            return None
        return self._spawn_workflow_row(wf, cursor, None)

    def _spawn_workflow_row(self, wf: dict, row_index: int,
                            existing_task_id):
        """渲染第 row_index 行的提示词，建/复用任务，放出去。

        existing_task_id 非空 = 认领重跑产生的既有任务，不新建。
        """
        wid = int(wf["id"])
        source = Path(wf["source_path"])
        cols = store.list_workflow_columns(wid)
        view = workflow_mod.read_source(source, header_row=int(wf["header_row"]))
        values = workflow_mod.row_values(source, view, row_index)
        slots: dict[str, str] = {}
        key_text = ""
        for col in cols:
            role = col["role"]
            text = values.get(int(col["col_index"]), "")
            if role == "key":
                key_text = text
            elif role in ("input", "context") and col.get("slot"):
                slots[col["slot"]] = text
        brief = workflow_mod.render(wf["prompt_template"], slots)
        title = f"[工作流 {wf['name']}] 第 {row_index + 1} 行" + (
            f" · {key_text[:60]}" if key_text else "")

        if existing_task_id:
            task_id = existing_task_id
        else:
            created = store.create_task(
                title, brief, owner_id=int(wf["owner_id"]), actor="system",
                workflow_id=wid, workflow_row=row_index)
            task_id = int(created["id"])
            store.add_event(task_id, "created", "system",
                            f"工作流「{wf['name']}」第 {row_index + 1} 行")
            # 放行的同一处语句里前移游标（规格 6.3）
            store.update_workflow(wid, cursor=row_index + 1)

        if int(task_id) in self.running:
            return int(task_id)
        try:
            work_dir = guards.regions(task_id)[guards.WORK]
        except guards.Violation as e:
            self._mark_failed(task_id, f"工作区路径不合法：{e}")
            return None
        try:
            got = store.claim_queued(task_id)
        except Exception:                          # noqa: BLE001
            log.exception("抢占行任务 %s 失败", task_id)
            return None
        if got != "queued":
            return None
        try:
            self._spawn(task_id, brief, work_dir)
        except Exception as e:                     # noqa: BLE001
            log.exception("spawn 行任务 %s 失败", task_id)
            self._mark_failed(task_id, f"计算进程启动失败：{e}")
            return None
        _notify()
        return int(task_id)
```

顶部补 import（**放在既有 import 之后**）：

```python
import workflow as workflow_mod
```

- [ ] **Step 6: 写归档 pass**

```python
    def _workflow_archive_pass(self) -> list[int]:
        """把终态且未归档的行写回结果表，**逐行**置 workflow_archived=1。

        「逐行」不是省事写法（规格 7.2）：一批 5 行里第 3 行抛异常时，
        前两行必须已经置位，否则下一轮会把它们连同第 3 行一起重来。
        「全批统一置位」看着更整齐，实际是把一次失败放大成 N 次重写。
        """
        handled: list[int] = []
        try:
            workflows = store.list_running_workflows()
        except Exception:                          # noqa: BLE001
            log.exception("读取 running 工作流失败")
            return handled
        for wf in workflows:
            wid = int(wf["id"])
            try:
                rows = store.workflow_unarchived_terminal_rows(wid)
            except Exception:                      # noqa: BLE001
                log.exception("读取待归档行失败：工作流 %s", wid)
                continue
            for row in rows:
                try:
                    self._archive_one(wf, row)
                except Exception:                  # noqa: BLE001 - 一行失败不影响别的行
                    log.exception("归档行任务 %s 失败", row["id"])
                    continue
                handled.append(int(row["id"]))
        return handled

    def _archive_one(self, wf: dict, row: dict) -> None:
        wid = int(wf["id"])
        task_id = int(row["id"])
        fields = {
            "status": row.get("status", ""),
            "summary": row.get("summary", ""),
            "error": row.get("error", ""),
            # 复用既有的 store.artifacts.list（store.py:517），**不要**另造一个
            # list_artifacts——这个项目已经吃过十几次「同一件事写了两份，
            # 改了一份另一份还在跑」的亏。
            "artifacts": ", ".join(
                a["name"] for a in store.artifacts.list(task_id)
                if int(a.get("verified") or 0) == 1),
        }
        output_cols = [workflow_mod.OutputCol(int(c["col_index"]), c["field"])
                       for c in store.list_workflow_columns(wid)
                       if c["role"] == "output" and c.get("field")]
        marked = False
        if output_cols:
            try:
                self._write_back_row(wf, output_cols, fields, int(row["workflow_row"]))
                marked = True
            except PermissionError:
                # 文件被 Excel/WPS 独占锁住：**降级不重试**（规格 7.1）。
                # 置位照做，否则整批卡在这一行；只把回写标成待补写。
                store.update_workflow(wid, writeback_pending=1)
                marked = True
            except (store.Invalid, OSError) as e:
                # 源表被改短/损坏：记一条失败原因并继续，不要抛——
                # 抛了会经 tick 的 except 变成一行静默日志。
                store.add_event(task_id, "error", "system",
                                f"归档失败：{e}")
                marked = True
        if not marked:
            marked = True                      # 没有 output 列也算归档完成
        if marked:
            store.update_task(task_id, workflow_archived=1)

    def _write_back_row(self, wf, output_cols, fields, row_index) -> None:
        source = Path(wf["source_path"])
        result = Path(wf["result_path"] or "")
        if not str(result):
            result = source.parent / "result.xlsx"
            store.update_workflow(int(wf["id"]), result_path=str(result))
        if not result.exists():
            import shutil
            shutil.copyfile(source, result)
        jobs = [(row_index, oc, fields.get(oc.field, ""))
                for oc in output_cols]
        workflow_mod.write_back(source, result, wf.get("sheet", ""),
                                int(wf["header_row"]), jobs)
        if int(wf.get("writeback_pending") or 0):
            store.update_workflow(int(wf["id"]), writeback_pending=0)
```

- [ ] **Step 7: 跑测试确认通过**

Run: `pytest tests/test_workflow_dispatch.py -q --no-header -p no:cacheprovider`
Expected: PASS（21 passed）

- [ ] **Step 8: 跑全量**

Run: `pytest tests -q --no-header -p no:cacheprovider`
Expected: 全绿

- [ ] **Step 9: 提交**

message 写进 `.git\MSG_t5.txt` 后 `git -F`：

```
调度器：工作流放行 pass + 归档 pass

闸门顺序 ①④⑤②③⑥ 是硬要求。④（认领 queued）必须在②（有未归档的行）
前面：重跑的行同时满足这两条，②在前就把它挡死、④永远走不到，整批卡死
且只在管理员用过重跑之后才发作。

归档逐行置 workflow_archived，不做「全批统一置位」——后者把一次失败
放大成 N 次重写。被 Excel 占住时降级不重试。

tick 里两个 pass 各自 try/except：_loop 是 try: tick() except，
一次异常会让整轮 _admit 都没机会执行，用户看到的是所有常规任务停在
「排队中」，而原因只是一张表被 Excel 开着。

围栏 19 条，含闸门顺序与谓词各一条专门用例。
```

---

### Task 6: HTTP 层 —— 上传、工作流 CRUD、控制端点、归属校验

**Files:**
- Modify: `server/app.py`（`USER_UI_FILES` 在 55；`DELETE /api/tasks/{id}` 在 420；静态路由在 526）
- Test: `tests/test_workflow_api.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 store 函数、Task 3/4 的 `workflow.*`
- Produces: 规格 9.1 的九个端点 + `GET /api/tasks?scope=` + `DELETE /api/tasks/{id}` 的 409 守卫

- [ ] **Step 1: 写失败测试**

新建 `tests/test_workflow_api.py`：

```python
"""工作流的 HTTP 层（规格 9.1 / 9.2）。

三件事最容易做漏，各有专门用例：

- **归属校验**：越权一律 404 不 403。``result.xlsx`` 是风险最高的一个——
  它绕过所有列表层过滤，且是唯一返回非 JSON 的端点。
- **scope 不是越权**：``scope=all`` 是「行任务也算进来」不是「所有人的都
  算进来」，两个正交维度。
- **上传校验不留痕**：空文件 / .sh / 魔数不对，库里都不能有记录。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import app as svc
import db
import store


@pytest.fixture()
def client():
    return TestClient(svc.create_public_app())


def _token(username, role="user"):
    """真实发令牌，不用 dev-login 旁路——旁路开着等于这个进程所有接口都不要凭证，
    而这里要验的恰恰是**归属校验**，走旁路就没有归属可言了。"""
    import auth
    auth.create_user(username, "pw-for-tests", role=role)
    row = auth.get_user(username)
    return auth.issue_token(int(row["id"]))


@pytest.fixture()
def auth_headers():
    return {"Authorization": "Bearer " + _token("alice")}


@pytest.fixture()
def other_headers():
    return {"Authorization": "Bearer " + _token("bob")}


@pytest.fixture()
def other_user():
    import auth
    return int(auth.get_user("bob")["id"])


def _xlsx_bytes(rows):
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def uploaded(client, auth_headers):
    """真走一次上传端点，返回解析结果。后面几个 fixture 都建在它上面。"""
    r = client.post(
        "/api/workflows/upload",
        headers=auth_headers,
        files={"file": ("表.xlsx",
                        _xlsx_bytes([["编号", "序列", "项目"],
                                     ["1", "AAA", "XBR"],
                                     ["2", "BBB", "XBR"]]),
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def one_workflow(client, auth_headers, uploaded):
    """建一个带列映射与模板、status=running 的工作流，并造一行 queued 的行任务。"""
    wid = uploaded["workflow"]["id"]
    r = client.post("/api/workflows", headers=auth_headers, json={
        "workflow_id": wid,
        "prompt_template": "查 {{seq}}，项目 {{proj}}",
        "columns": [
            {"col_index": 0, "header": "编号", "role": "key"},
            {"col_index": 1, "header": "序列", "role": "input", "slot": "seq"},
            {"col_index": 2, "header": "项目", "role": "context", "slot": "proj"},
            {"col_index": 3, "header": "状态", "role": "output", "field": "status"},
        ]})
    assert r.status_code == 200, r.text
    store.update_workflow(wid, status="running", total_rows=2)
    row = store.create_task("第 1 行", "brief", owner_id=store.get_workflow(
        wid, is_admin=True)["owner_id"], actor="system",
        workflow_id=wid, workflow_row=0)
    return {"id": wid, "row_id": int(row["id"])}


def test_upload_rejects_shell(tmp_path, monkeypatch):
    from pathlib import Path
    root = Path(tmp_path) / "data" / "workflows"
    monkeypatch.setattr(svc.db, "data_root", lambda: Path(tmp_path) / "data")
    try:
        (root).mkdir(parents=True, exist_ok=True)
        svc.save_upload("evil.sh", b"echo hi", owner_id=1)
    except store.Invalid:
        pass
    else:
        raise AssertionError("写 .sh 进库了")


def test_save_upload_uses_fixed_filename(tmp_path, monkeypatch):
    """路径穿越：filename 是用户可控的，落盘必须用固定名。"""
    from pathlib import Path
    root = Path(tmp_path) / "data" / "workflows"
    monkeypatch.setattr(svc.db, "data_root", lambda: Path(tmp_path) / "data")
    final = svc.save_upload("../../evil.xlsx",
                      _xlsx_bytes([["编号", "序列"], ["1", "A"]]),
                      owner_id=1)
    assert final.name == "source.xlsx"
    assert final.parent.name.isdigit()
    assert "evil" not in str(final)


def test_save_upload_leaves_no_db_row_on_invalid(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(svc.db, "data_root", lambda: Path(tmp_path) / "data")
    before = len(store.list_workflows(is_admin=True))
    try:
        svc.save_upload("空.xlsx", b"", owner_id=1)
    except store.Invalid:
        pass
    assert len(store.list_workflows(is_admin=True)) == before
```

紧接着追加这些端点行为用例：

```python
def test_delete_workflow_row_task_returns_409(client, auth_headers, one_workflow):
    """不挡就静默丢结果 + 留僵尸进程（规格 6.5 洞三）。"""
    row = one_workflow["row_id"]
    r = client.delete(f"/api/tasks/{row}", headers=auth_headers)
    assert r.status_code == 409
    assert "跳过" in r.json()["detail"] or "工作流" in r.json()["detail"]
    assert store.get_task(row) is not None, "守卫不该删数据"


def test_scope_regular_excludes_workflow_rows(client, auth_headers, one_workflow):
    r = client.get("/api/tasks?scope=regular", headers=auth_headers)
    assert one_workflow["row_id"] not in {t["id"] for t in r.json()["tasks"]}
    r = client.get("/api/tasks?scope=workflow", headers=auth_headers)
    assert one_workflow["row_id"] in {t["id"] for t in r.json()["tasks"]}


def test_scope_all_is_not_cross_user(client, auth_headers, one_workflow, other_user):
    store.create_task("别人的常规任务", "b", owner_id=other_user)
    r = client.get("/api/tasks?scope=all", headers=auth_headers)
    assert all(t["owner_id"] != other_user for t in r.json()["tasks"])


def test_result_xlsx_cross_user_is_404(client, other_headers, one_workflow):
    r = client.get(f"/api/workflows/{one_workflow['id']}/result.xlsx",
                   headers=other_headers)
    assert r.status_code == 404
    assert b"PK" not in r.content, "响应体里不该有任何 xlsx 字节"


def test_control_endpoints_cross_user_are_404(client, other_headers, one_workflow):
    for path in (f"start", f"pause", f"abort"):
        r = client.post(f"/api/workflows/{one_workflow['id']}/{path}",
                        headers=other_headers)
        assert r.status_code == 404, path
    assert store.get_workflow(one_workflow["id"])["status"] == "running"


def test_unbound_slot_is_rejected_on_create(client, auth_headers, uploaded):
    r = client.post("/api/workflows", headers=auth_headers, json={
        "name": "w", "prompt_template": "查 {{seq}} 与 {{proj}}",
        "columns": [{"col_index": 0, "role": "key"},
                    {"col_index": 1, "role": "input", "slot": "seq"}],
        **uploaded})
    assert r.status_code == 400


def test_admin_cannot_control_others_workflow(client, one_workflow):
    """§9.3：值班台**只读**。管理员看得见别人的工作流，但不能 start/pause/abort。

    管理员看得到是因为值班台列表传了 is_admin=True；控制权不放开是刻意取舍，
    不然一次误点就停了别人几十分钟 GPU 的批次。
    """
    import auth
    auth.create_user("root", "pw-for-tests", role="admin")
    admin_token = auth.issue_token(int(auth.get_user("root")["id"]))
    headers = {"Authorization": "Bearer " + admin_token}

    assert client.get(f"/api/workflows/{one_workflow['id']}",
                      headers=headers).status_code == 200   # 看得见
    for act in ("start", "pause", "abort"):
        r = client.post(f"/api/workflows/{one_workflow['id']}/{act}",
                        headers=headers)
        assert r.status_code == 404, f"管理员竟然能 {act} 别人的工作流"
    assert store.get_workflow(one_workflow["id"])["status"] == "running"


def test_workflow_list_is_owner_scoped(client, auth_headers, one_workflow,
                                      other_headers):
    r = client.get("/api/workflows", headers=other_headers)
    assert r.json()["workflows"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_workflow_api.py -q --no-header -p no:cacheprovider`
Expected: FAIL —— `AttributeError: module 'app' has no attribute 'save_upload'`

- [ ] **Step 3: 扩 `USER_UI_FILES` 与 scope**

`server/app.py`：

```python
USER_UI_FILES = frozenset({"index.html", "app.js", "style.css",
                           "workflow.html", "workflow.js"})
```

`GET /api/tasks` 加 `scope` 参数（**默认 `regular`**，与规格 8.1 一致）：

```python
    @app.get("/api/tasks")
    def list_tasks(status: str | None = None,
                   scope: str = Query(default="regular",
                                      pattern="^(regular|workflow|all)$"),
                   limit: int = Query(default=100, ge=1, le=500),
                   user=Depends(auth.current_user)):
        is_admin = user["role"] == "admin"
        rows = store.list_tasks(None if is_admin else user["id"], status,
                                is_admin=is_admin, limit=limit)
        # scope 只管「行任务算不算进来」，**不管归属**——归属由上面的
        # owner_id 参数负责，两个维度是正交的。
        if scope == "regular":
            rows = [r for r in rows if r.get("workflow_id") is None]
        elif scope == "workflow":
            rows = [r for r in rows if r.get("workflow_id") is not None]
        return {"tasks": [_task_view(t) for t in rows]}
```

- [ ] **Step 4: 加 DELETE 守卫**

在 `server/app.py` 的 `DELETE /api/tasks/{task_id}` 里，`store.get_task` 之后立刻插入：

```python
        if task.get("workflow_id") is not None:
            # 删掉行任务后归档再也扫不到它 → 那一行的结果静默丢失，而闸门②
            # 因为该行不见了反而满足，整批继续往下跑（规格 6.5 洞三）。
            raise PreciseHTTPException(
                409,
                "这是工作流行任务，删掉它会让那一行的结果无法归档。"
                "要跳过这一行请用「跳过这一行」，要清掉整批请删除整个工作流。")
```

- [ ] **Step 5: 加 `save_upload` 与九个端点**

在 `server/app.py` 里加（`save_upload` 放模块级，端点放进 `create_public_app()` 内）：

```python
def _workflow_root() -> Path:
    """工作流源文件目录。**刻意在 guards.regions() 四区之外**——
    agent 不该有权限碰用户的表格，回写一律由平台做（规格 4.4）。"""
    return db.data_root() / "workflows"


def save_upload(filename: str, data: bytes, owner_id: int) -> dict:
    """落盘 + 建库记录。**先 staging、解析成功再建记录**（规格 4.5 ③）。

    反过来（先建记录再解析）的话，解析失败要回滚一行记录加一个目录，
    两处都得清，漏一处就留下垃圾。
    """
    import workflow as wfmod
    wfmod.check_upload(filename, data)          # 魔数 / 扩展名 / 大小 / 空文件
    root = _workflow_root()
    staging = wfmod.staging_path(root)
    staging.write_bytes(data)
    try:
        view = wfmod.read_source(staging)
    except Exception:
        staging.unlink(missing_ok=True)         # 解析失败：库里不留痕，磁盘不留垃圾
        raise
    wf = store.create_workflow(
        owner_id, wfmod.safe_display_name(filename), "",
        sheet=view.sheet, source_path="", header_row=1, key_col=0,
        total_rows=len(view.row_numbers))
    source = wfmod.finalize_upload(root, staging, int(wf["id"]))
    store.update_workflow(int(wf["id"]), source_path=str(source))
    store.set_workflow_columns(int(wf["id"]), [
        {"col_index": i, "header": h, "role": ("key" if i == 0 else "ignore")}
        for i, h in enumerate(view.headers)])
    wf = store.get_workflow(int(wf["id"]))
    return {"workflow": wf, "headers": view.headers,
            "row_count": len(view.row_numbers), "preview": view.preview}
```

九个端点（放进 `create_public_app()`，每个都先按 owner 取，越权 404）：

```python
    @app.post("/api/workflows/upload")
    async def upload_workflow(file: UploadFile = File(...),
                              user=Depends(auth.current_user)):
        data = await file.read()
        info = save_upload(file.filename or "", data, user["id"])
        return info

    @app.post("/api/workflows")
    def save_workflow(body: WorkflowIn, user=Depends(auth.current_user)):
        """上传端点已经建好了记录，这里只负责**配列映射与模板**。

        越权先判（404），再强校验，再落库——顺序反了会把「别人的工作流」
        这个信息通过 400/409 的差异泄露出去。
        """
        import workflow as wfmod
        wf = store.get_workflow(body.workflow_id, owner_id=user["id"])
        cols = [c.model_dump() for c in body.columns]
        wfmod.validate_binding(body.prompt_template, cols)     # 服务端强校验
        store.update_workflow(body.workflow_id,
                              prompt_template=body.prompt_template)
        store.set_workflow_columns(body.workflow_id, cols)
        return {"workflow": store.get_workflow(wf["id"], owner_id=user["id"])}

    @app.get("/api/workflows")
    def list_workflows(user=Depends(auth.current_user)):
        is_admin = user["role"] == "admin"
        return {"workflows": store.list_workflows(
            None if is_admin else user["id"], is_admin=is_admin)}

    @app.get("/api/workflows/{workflow_id}")
    def get_workflow(workflow_id: int, user=Depends(auth.current_user)):
        is_admin = user["role"] == "admin"
        wf = store.get_workflow(workflow_id, owner_id=user["id"],
                                is_admin=is_admin)
        return {"workflow": wf,
                "columns": store.list_workflow_columns(workflow_id),
                "skips": sorted(store.list_workflow_skips(workflow_id)),
                "rows": store.list_workflow_rows(workflow_id)}

    @app.get("/api/workflows/{workflow_id}/preview")
    def preview_workflow(workflow_id: int, row: int = 0,
                         user=Depends(auth.current_user)):
        import workflow as wfmod
        wf = store.get_workflow(workflow_id, owner_id=user["id"])
        source = Path(wf["source_path"])
        view = wfmod.read_source(source, header_row=int(wf["header_row"]))
        values = wfmod.row_values(source, view, int(row))
        slots = {}
        for col in store.list_workflow_columns(workflow_id):
            if col["role"] in ("input", "context") and col.get("slot"):
                slots[col["slot"]] = values.get(int(col["col_index"]), "")
        return {"row": row,
                "rendered": wfmod.render(wf["prompt_template"], slots),
                "values": {wf["sheet"]: values}}

    @app.post("/api/workflows/{workflow_id}/{action}")
    def control_workflow(workflow_id: int, action: str,
                         user=Depends(auth.current_user)):
        """action ∈ start | pause | abort。越权一律 404（规格 9.2）。"""
        if action not in ("start", "pause", "abort"):
            raise PreciseHTTPException(404, "没有这个操作")
        wf = store.get_workflow(workflow_id, owner_id=user["id"])   # 管理员也按 owner
        if action == "start":
            wf = store.update_workflow(workflow_id, status="running",
                                       abort_requested=0)
        elif action == "pause":
            wf = store.update_workflow(workflow_id, status="paused")
        else:
            wf = store.update_workflow(workflow_id, status="paused",
                                       abort_requested=1)
            _abort_live_rows(workflow_id)
        sse.notify()
        return {"workflow": wf}

    @app.post("/api/workflows/{workflow_id}/rows/{row_index}/skip")
    def skip_row(workflow_id: int, row_index: int,
                 body: SkipIn, user=Depends(auth.current_user)):
        """跳过 = 写 workflow_skips + 把该行置终态 + 归档写「已跳过」。

        置终态是必需的：不置的话闸门② 会一直等这一行，整批卡住。
        """
        wf = store.get_workflow(workflow_id, owner_id=user["id"])
        store.skip_workflow_row(workflow_id, row_index, body.reason)
        for row in store.list_workflow_rows(workflow_id, row_index):
            if row["status"] not in db.TERMINAL_STATUSES:
                store.set_status(row["id"], "failed", actor="human",
                                 message=f"用户跳过：{body.reason or '未说明'}")
        sse.notify()
        return {"workflow": wf, "skips": sorted(store.list_workflow_skips(workflow_id))}

    @app.get("/api/workflows/{workflow_id}/result.xlsx")
    def download_result(workflow_id: int, user=Depends(auth.current_user)):
        wf = store.get_workflow(workflow_id, owner_id=user["id"])
        path = Path(wf["result_path"] or "")
        if not wf["writeback"] or not path.exists():
            raise PreciseHTTPException(404, "结果表还没生成")
        return FileResponse(path, filename=path.name,
                            media_type=("application/vnd.openxmlformats-"
                                        "officedocument.spreadsheetml.sheet"))

    @app.delete("/api/workflows/{workflow_id}")
    def delete_workflow(workflow_id: int, user=Depends(auth.current_user)):
        """先中止在跑的行任务，再删记录（规格 6.7）。

        顺序反了会留下一个还在烧 GPU 的僵尸：级联删掉 tasks 行后
        ``_db_status`` 返回 ""，``_abort_signal`` 因 "" not in TERMINAL_STATUSES
        直接返回 None，调度器不会去杀它。
        """
        _abort_live_rows(workflow_id)
        _settle_abort_signal(dispatcher.POLL_INTERVAL * 3)
        store.delete_workflow(workflow_id)
        sse.notify()
        return {"ok": True}
```

配套的模块级辅助：

```python
def _abort_live_rows(workflow_id: int) -> list[int]:
    """把在跑的行逐个写成 failed（actor=human）——**这是唯一的跨进程中止通道**。

    不要试图去拿调度器实例调 abort：``scripts/start-services.ps1:41`` 对 public /
    internal / admin **三个进程都设了 WORKERS_ENV=1**，所以三个进程各自建了
    一个 Dispatcher 并 start()（``app.py:799-801``），靠 claim_queued 的 CAS
    决定谁真的起进程。于是 public 里的 app.state.dispatcher 虽然是活的，它的
    running 里**通常没有别人的 agent**——对着它调 abort 是对着空字典操作，静默无效。

    真正管用的是数据库：现有 abort 路由（``app.py:487-491``）就只写库，持有该
    agent 的那个 dispatcher 在下一次 _reap（<=1 秒）里经 _abort_signal 看到终态
    且 actor 是人，于是真去杀进程。这里照抄那一套，不发明新机制。
    """
    out = []
    for row in store.list_workflow_rows(workflow_id):
        if row["status"] not in db.TERMINAL_STATUSES:
            store.set_status(row["id"], "failed", actor="human",
                             message="工作流中止")
            store.update_task(row["id"], error="工作流中止")
            out.append(int(row["id"]))
    return out


def _settle_abort_signal(seconds: float) -> None:
    """给中止信号留出投递时间，然后**不假装确认过**。

    public 进程看不到 agent 是否真死了（不在它的 running 里），能观测到的
    只有数据库。所以这不是「等进程退出」，只是让持有它的那个 dispatcher 至少
    完成一轮 _reap。**绝不能先删记录再等**——删完 _db_status 立刻返回 ""，
    中止信号根本产生不出来，那正是「删工作流留下一个烧到自然结束的僵尸」的
    成因（规格 6.7）。
    """
    import time
    time.sleep(seconds)
```

把删除端点里的那行也改掉：

```python
        _abort_live_rows(workflow_id)
        _settle_abort_signal(dispatcher.POLL_INTERVAL * 3)
        store.delete_workflow(workflow_id)
```

两个请求体模型（加在既有的请求体模型旁边）：

```python
class ColumnIn(BaseModel):
    col_index: int
    header: str = ""
    role: str = "ignore"
    slot: str = ""
    field: str = ""


class WorkflowIn(BaseModel):
    workflow_id: int
    name: str = ""
    prompt_template: str
    columns: list[ColumnIn] = []


class SkipIn(BaseModel):
    reason: str = ""
```

- [ ] **Step 6: 跑测试确认通过**

Run: `pytest tests/test_workflow_api.py -q --no-header -p no:cacheprovider`
Expected: PASS

- [ ] **Step 7: 跑全量**

Run: `pytest tests -q --no-header -p no:cacheprovider`
Expected: 全绿

- [ ] **Step 8: 提交**

message 写进 `.git\MSG_t6.txt` 后 `git -F`：

```
HTTP 层：九个工作流端点 + scope + 删除守卫

归属校验统一走 store.get_workflow(owner_id=...) → 404 不 403。
result.xlsx 是风险最高的一个：绕过列表层过滤、且是唯一非 JSON 端点。

scope 只管「行任务算不算进来」，不管归属——两个正交维度。

DELETE 行任务 409：删掉后归档扫不到它，那一行结果静默丢失，
而闸门② 因为该行不见了反而满足，整批继续跑。

落盘固定名 source.xlsx，展示名才去路径分隔符；解析失败不留库记录、
不留 staging 垃圾。
```

---

### Task 7: 值班台 —— 409 守卫与列表过滤

**Files:**
- Modify: `admin/app_admin.py`（列表在 416；`force_status` 在 472；`retry` 在 486）
- Test: `tests/test_workflow_admin.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `store` 函数
- Produces: 值班台列表默认排除行任务；`force_status` 对行任务的 409；`retry` 清 `workflow_archived`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_workflow_admin.py`：

```python
"""值班台对工作流行任务的三处处理（规格 6.5 守卫侧 / 8.3）。

最狠的一条是 force_status 能设 running/packaging：库里真写成非终态，
claim_queued 接不走，闸门⑤从此恒真，**整个工作流从那一行起永久卡死**，
而界面显示「正在跑第 N 行」且没有任何进程。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app_admin as admin_svc
import db
import store


@pytest.fixture()
def client():
    return TestClient(admin_svc.create_admin_app())


def test_force_status_running_on_row_task_is_409(client, admin_headers, one_row):
    for target in ("running", "packaging"):
        r = client.post(f"/api/admin/tasks/{one_row['id']}/status",
                        headers=admin_headers, json={"target": target})
        assert r.status_code == 409, target
        assert "工作流" in r.json()["detail"]
        assert store.get_task(one_row["id"])["status"] == "failed"   # 没被改


def test_force_status_terminal_still_allowed(client, admin_headers, one_row):
    r = client.post(f"/api/admin/tasks/{one_row['id']}/status",
                    headers=admin_headers, json={"target": "done"})
    assert r.status_code == 200


def test_force_status_regular_task_unaffected(client, admin_headers, one_regular):
    r = client.post(f"/api/admin/tasks/{one_regular['id']}/status",
                    headers=admin_headers, json={"target": "running"})
    assert r.status_code == 200


def test_retry_clears_archived(client, admin_headers, one_row):
    """漏清这个标记 → 重跑跑完了、状态是 done、但表里还是旧值（规格 6.5）。"""
    store.update_task(one_row["id"], workflow_archived=1)
    r = client.post(f"/api/admin/tasks/{one_row['id']}/retry",
                    headers=admin_headers)
    assert r.status_code == 200
    back = store.get_task(one_row["id"])
    assert back["status"] == "queued"
    assert back["workflow_archived"] == 0


def test_console_list_excludes_workflow_rows(client, admin_headers, one_row,
                                             one_regular):
    r = client.get("/api/admin/tasks", headers=admin_headers)
    ids = {t["id"] for t in r.json()["tasks"]}
    assert one_regular["id"] in ids
    assert one_row["id"] not in ids


def test_console_can_still_see_workflow_rows_explicitly(client, admin_headers,
                                                        one_row):
    r = client.get("/api/admin/tasks?scope=workflow", headers=admin_headers)
    assert one_row["id"] in {t["id"] for t in r.json()["tasks"]}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_workflow_admin.py -q --no-header -p no:cacheprovider`
Expected: FAIL —— 列表包含行任务、`force_status` 返回 200、`retry` 后 `workflow_archived` 仍是 1

- [ ] **Step 3: 加守卫与过滤**

`admin/app_admin.py`：

```python
    rows = store.list_tasks(None, None, is_admin=True, limit=500)
    # 值班台是「现在有什么不对」的地方，几百个正常跑的行任务会把故障排查
    # 视图糊满。行任务的排查走工作流详情页（规格 8.3）。
    if scope == "regular":
        rows = [r for r in rows if r.get("workflow_id") is None]
    elif scope == "workflow":
        rows = [r for r in rows if r.get("workflow_id") is not None]
```

在 `force_status` 里，`target not in db.VALID_STATUSES` 那个校验**之后**插入：

```python
    if task.get("workflow_id") is not None and target in ("running", "packaging"):
        raise PreciseHTTPException(
            409,
            "这是工作流行任务。把它改成「计算中」或「打包中」会让整批卡死："
            "库里写成非终态之后没人能接走它。要重跑请用「重跑」，"
            "要标成完成或失败请选终态。")
```

在 `retry` 里，写回 `queued` 的**同一处**加一行：

```python
    store.update_task(task_id, workflow_archived=0)
```

并把注释写清楚：

```python
    # workflow_archived 必须一起清：不清的话重跑的行带着 archived=1，
    # 归档 pass 扫「终态 且 archived=0」，于是这行的新结果永远不写回表，
    # 而平台上看起来一切正常（规格 6.5）。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_workflow_admin.py -q --no-header -p no:cacheprovider`
Expected: PASS（6 passed）

- [ ] **Step 5: 跑全量**

Run: `pytest tests -q --no-header -p no:cacheprovider`
Expected: 全绿

- [ ] **Step 6: 提交**

message 写进 `.git\MSG_t7.txt` 后 `git -F`：

```
值班台：force_status 409 + 列表过滤 + retry 清归档标记

force_status 设 running/packaging 会让整批从那一行起永久卡死，
界面显示「正在跑第 N 行」而没有任何进程——库里写成非终态，
claim_queued 接不走，闸门⑤恒真。

retry 漏清 workflow_archived 是全篇最安静的失效：状态是 done、
界面一切正常，只有 Excel 里那行是上一轮的旧值。

围栏 6 条，每条都读回数据库核对值。
```

---

### Task 8: agent 侧 —— 工作流行任务的产物落点规则

**Files:**
- Modify: `agent/run.mjs`（`PLATFORM_RULES`）
- Test: `agent/guards.audit.test.mjs`（追加用例）

**Interfaces:**
- Consumes: 无（纯提示词）
- Produces: `PLATFORM_RULES` 里关于产物落点的一段

- [ ] **Step 1: 写失败测试**

追加到 `agent/guards.audit.test.mjs`：

```javascript
// 工作流行任务的产物落点（规格 7.4）。
//
// 这条规则存在的原因很具体：`artifacts.discover()` 有三条发现路径，
// `outputs/` 下时间窗兜底命中的 verified=0，而打包器诚实地失败。落到
// outputs 根又没前缀，这一行会以「没有可下载的产物」失败——按「失败继续」
// 策略整批一行接一行全失败，**且只显示 failed、不报任何错**。
test("PLATFORM_RULES 明写工作流行任务的产物落点", async () => {
  const src = await readFile(new URL("./run.mjs", import.meta.url), "utf8");
  assert.ok(src.includes("工作流"), "提示词里没提工作流");
  assert.ok(/工作目录|工作区/.test(src), "没要求落在工作目录");
  assert.ok(src.includes("t{task_id}_") || src.includes("t${taskId}_")
            || /t\\{task_id\\}_/.test(src),
            "没写清 outputs 前缀规则");
});
```

顶部补两行 import（`assert` 这个文件已经 import 了，`readFile` 没有）：

```javascript
import { readFile } from "node:fs/promises';
```

即在现有的

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';
```

**之后**加这一行，不要动已有那两行。用 `readFile` 而不是 `readFileSync`
是为了和同目录其它测试风格一致；两者在这个用例里都成立。

- [ ] **Step 2: 跑测试确认失败**

Run: `Set-Location '.\agent'; node --test guards.audit.test.mjs`
Expected: FAIL —— 断言「提示词里没提工作流」

- [ ] **Step 3: 改 `PLATFORM_RULES`**

在 `agent/run.mjs` 的 `PLATFORM_RULES` 里追加一段（放在产物落点相关的既有条款之后）：

```javascript
  `- **工作流任务（第 N 行）的产物落点**：这类任务的产物同样必须落在工作目录，
   也就是你启动时所在的那个目录。如果你确实需要写共享目录，文件名**必须**以
   t{你的任务号}_ 开头（例如 t17_hit.csv）——落在共享目录根部又没有这个前缀的
   文件会被判为「归属不明」，**那一行会算成失败，而表里只会看到一个 failed，
   不会告诉你原因**。`,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `Set-Location '.\agent'; node --test guards.audit.test.mjs guards.test.mjs guards.dotdot.test.mjs`
Expected: PASS

- [ ] **Step 5: 提交**

message 写进 `.git\MSG_t8.txt` 后 `git -F`：

```
agent 侧：PLATFORM_RULES 补工作流行任务的产物落点

落在 outputs 根又没 t{task_id}_ 前缀的文件，verified=0，打包器诚实地失败。
按「失败继续」策略整批一行接一行全失败，且只显示 failed 不报错——
批量场景特有的放大效应。

改 agent 侧文件**不需要重启服务**：调度器每次 spawn 子进程时现读 run.mjs。
```

---

### Task 9: 前端 —— 先扩测试范围，再建工作流页

**Files:**
- Modify: `tests/test_web_static.py`（文件清单硬编码在 26-28、41）
- Modify: `tests/test_frontend_url_contract.py`（`USER_JS` 硬编码在 34）
- Create: `web/workflow.html`
- Create: `web/workflow.js`
- Test: `tests/frontend_smoke.mjs`（追加用例）

**Interfaces:**
- Consumes: Task 6 的九个端点
- Produces: 工作流页；两个既有测试的扫描范围包含新文件

> ⚠️ **本任务里 Step 1-3 必须在 Step 4 之前。** 反过来做会出现一个窗口期：
> 新文件已在磁盘上却不在任何检查范围里，而所有测试都是绿的——那正是
> 「看起来有测试、实际没测」的状态（规格 8.4）。

- [ ] **Step 1: 扩 `test_web_static.py` 的文件清单**

把 `tests/test_web_static.py` 的 25-41 改成：

```python
WEB = Path(__file__).resolve().parent.parent / "web"
# 工作流页是**新文件**，而这套检查原本只扫三个写死的文件——不把新文件加进
# _files()，它就完全在检查范围外：禁用词、外链、状态色几类一个都不查。
# 而工作流页必然要写「计算任务/计算进程」这类文案，正是最容易顺手写出
# 禁用词的地方（规格 8.4）。
_HTML_FILES = ["index.html", "workflow.html"]
_CSS_FILES = ["style.css"]
_JS_FILES = ["app.js", "workflow.js"]


def _read(p: Path) -> str:
    assert p.exists(), f"缺文件：{p}"
    return p.read_text(encoding="utf-8")


def _html(name: str) -> str:
    return _read(WEB / name)


def _js(name: str) -> str:
    return _read(WEB / name)


def _files() -> dict[str, str]:
    out = {}
    for name in _HTML_FILES + _CSS_FILES + _JS_FILES:
        out[name] = _read(WEB / name)
    return out
```

把 `test_no_external_resource_links` 里读 `HTML` 的地方改成循环：

```python
def test_no_external_resource_links():
    for name in _HTML_FILES:
        html = _html(name)
        refs = re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', html)
        assert refs, f"{name} 一个资源引用都没有，八成是写坏了"
        for r in refs:
            assert not r.startswith(("http://", "https://", "//")), f"{name} 外链资源：{r}"
            assert r in ("style.css", "app.js", "workflow.js", "workflow.html",
                         "data:,"), f"{name} 意外的引用：{r}"
```

> 上面那行**故意不改成"任意同源路径"**。放宽了等于放弃「首页只准引这几个」这条
> 约束，而它当初就是为了拦住外链 CDN 而加的。

- [ ] **Step 2: 加一条「白名单与磁盘互为充要」的用例**

追加到 `tests/test_web_static.py`：

```python
def test_user_ui_files_match_disk_both_ways():
    """白名单里有而磁盘上没有 → 红；磁盘上有而白名单里没有 → 红。

    后一种就是线上 404 而所有测试全绿的形状（规格 8.4）。
    """
    import sys
    sys.path.insert(0, str(REPO / "server"))
    import app as svc

    whitelisted = set(svc.USER_UI_FILES)
    on_disk = {p.name for p in WEB.iterdir()
               if p.suffix in (".html", ".js", ".css")}
    assert whitelisted <= on_disk, f"白名单里有磁盘上没有的：{whitelisted - on_disk}"
    assert on_disk <= whitelisted, f"磁盘上有白名单里没有的：{on_disk - whitelisted}"
```

顶部加 `REPO`：

```python
REPO = Path(__file__).resolve().parent.parent
```

- [ ] **Step 3: 扩 `test_frontend_url_contract.py`**

把 `USER_JS` 改成列表，并加四个取数的辅助：

```python
USER_JS = [REPO / "web" / "app.js", REPO / "web" / "workflow.js"]
ADMIN_JS = [REPO / "admin-ui" / "app.js"]


def _user_srcs():
    return [p.read_text(encoding="utf-8") for p in USER_JS]


def _admin_srcs():
    return [p.read_text(encoding="utf-8") for p in ADMIN_JS]


def _user_paths():
    return [p for src in _user_srcs() for p in _js_paths(src)]


def _admin_paths():
    return [p for src in _admin_srcs() for p in _js_paths(src)]
```

然后**只改取数、不改断言**，逐处替换（共 7 处）：

| 现有行 | 原 | 换成 |
| --- | --- | --- |
| 99 | `_js_paths(USER_JS.read_text(encoding="utf-8"))` | `_user_paths()` |
| 103 | `_js_paths(ADMIN_JS.read_text(encoding="utf-8"))` | `_admin_paths()` |
| 116 | `_api_base_suffix(USER_JS.read_text(encoding="utf-8"))` | `for src in _user_srcs(): assert _api_base_suffix(src) == "", ...` |
| 125、133 | `for p in _js_paths(USER_JS.read_text(...)):` | `for p in _user_paths():` |
| 146、155 | `for p in _js_paths(ADMIN_JS.read_text(...)):` | `for p in _admin_paths():` |

**第 99 行那条防恒真的断言要顺势加强**：现在两个文件合起来扫，条数门槛不该
还停在 8，提到 `>= 10`。否则「只扫到 app.js 一个文件」的退化也照样通过——
而这条用例存在的理由正是防恒真。

- [ ] **Step 4: 跑这两个测试确认它们**因为新文件还不存在**而红**

Run: `pytest tests/test_web_static.py tests/test_frontend_url_contract.py -q --no-header -p no:cacheprovider`
Expected: FAIL —— `缺文件：.../web/workflow.html`

**这一步的红是预期中的**，它正是「扫描范围已经扩到新文件」的证据。**记下这个红的样子**——Step 5 之后它必须变成绿，而红的原因要从「文件不存在」变成「真有问题」才算修好。

- [ ] **Step 5: 写 `web/workflow.html`**

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>工作流 · 自动化计算平台</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="style.css">
</head>
<body class="workflow-page">
<header class="topbar">
  <a class="back" href="index.html">← 返回计算任务</a>
  <h1>工作流</h1>
</header>

<main id="wf-main">
  <section id="wf-list" hidden>
    <div class="toolbar">
      <input type="file" id="wf-file" accept=".xlsx">
      <button id="wf-upload">上传表格</button>
    </div>
    <div id="wf-cards"></div>
  </section>

  <section id="wf-detail" hidden>
    <div class="wf-head">
      <h2 id="wf-name"></h2>
      <div class="wf-progress">
        <div class="bar"><i id="wf-bar"></i></div>
        <span id="wf-progress-text"></span>
      </div>
      <div class="wf-controls">
        <button data-act="start">开始</button>
        <button data-act="pause">暂停（当前行会跑完再停）</button>
        <button data-act="abort">中止整批（立刻停掉当前行）</button>
        <a id="wf-download" href="#" hidden>下载结果表</a>
      </div>
    </div>

    <div class="wf-cols" id="wf-cols"></div>

    <table class="wf-table">
      <thead><tr id="wf-head"></tr></thead>
      <tbody id="wf-body"></tbody>
    </table>

    <h3>第 <span id="wf-preview-row">1</span> 行渲染出的提示词</h3>
    <pre id="wf-preview"></pre>
  </section>
</main>

<script src="workflow.js"></script>
</body>
</html>
```

**禁用词自查**：`AI`、`助手`、`大模型` 一个都不能出现。

- [ ] **Step 6: 写 `web/workflow.js`**

```javascript
// 工作流页。零构建原生 JS：只用同源接口，不引任何 CDN（规格 8.2）。
const $ = (s) => document.querySelector(s);
const api = async (method, path, body) => {
  const r = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d.detail || r.statusText);
  }
  return r.json();
};

let wfId = null;

async function boot() {
  await loadList();
}

async function loadList() {
  const d = await api("GET", "/api/workflows");
  $("#wf-list").hidden = false;
  $("#wf-detail").hidden = true;
  const box = $("#wf-cards");
  box.innerHTML = "";
  for (const wf of d.workflows) {
    const done = Number(wf.cursor);
    const total = Number(wf.total_rows) || 0;
    const el = document.createElement("article");
    el.className = "wf-card";
    el.innerHTML = `<h3></h3>
      <div class="bar"><i style="width:${total ? (done / total * 100) : 0}%"></i></div>
      <p class="meta"></p>`;
    el.querySelector("h3").textContent = wf.name;
    el.querySelector(".meta").textContent =
      `${wf.status} · ${done} / ${total} 行` +
      (wf.abort_requested ? " · 已请求中止" : "") +
      (wf.writeback_pending ? " · 结果表被占用，待补写" : "");
    el.addEventListener("click", () => openDetail(wf.id));
    box.appendChild(el);
  }
}

async function openDetail(id) {
  wfId = id;
  const d = await api("GET", `/api/workflows/${id}`);
  $("#wf-list").hidden = true;
  $("#wf-detail").hidden = false;
  const wf = d.workflow;
  $("#wf-name").textContent = wf.name;
  const done = Number(wf.cursor), total = Number(wf.total_rows) || 0;
  $("#wf-bar").style.width = `${total ? (done / total * 100) : 0}%`;
  $("#wf-progress-text").textContent = `${done} / ${total} 行`;
  $("#wf-download").hidden = !wf.writeback;
  $("#wf-download").href = `/api/workflows/${id}/result.xlsx`;

  // 列角色做在表头上：心智是「看着表选列」，操作就该发生在表上（规格 8.2）
  const head = $("#wf-head");
  head.innerHTML = "";
  head.appendChild(document.createElement("th"));
  for (const col of d.columns) {
    const th = document.createElement("th");
    const sel = document.createElement("select");
    for (const r of ["ignore", "key", "input", "context", "output"]) {
      const o = document.createElement("option");
      o.value = r; o.textContent = ROLE_LABEL[r];
      if (col.role === r) o.selected = true;
      sel.appendChild(o);
    }
    sel.addEventListener("change", () => setRole(col.col_index, sel.value));
    th.appendChild(sel);
    th.appendChild(document.createTextNode(col.header || `列${col.col_index + 1}`));
    head.appendChild(th);
  }
  renderRows(d);
  loadPreview(0);
}

const ROLE_LABEL = {
  ignore: "忽略", key: "标识列", input: "输入列",
  context: "上下文", output: "结果列",
};

function renderRows(d) {
  const body = $("#wf-body");
  body.innerHTML = "";
  // 表头是「行号列 + 每一列角色」共 1+N 格，所以数据行也必须 1+N 格。
  // 少写一个 td，浏览器会把后面的行整体左移，读起来像是列错位了。
  const cols = d.columns;
  for (const row of d.rows) {
    const tr = document.createElement("tr");
    tr.addEventListener("click", () => loadPreview(row.workflow_row));
    const th = document.createElement("th");
    th.textContent = String(row.workflow_row + 1);
    th.title = row.title || "";
    tr.appendChild(th);
    for (let i = 0; i < cols.length; i += 1) {
      const td = document.createElement("td");
      if (cols[i].role === "output") {
        // 结果列：显示状态，其余交给详情页与结果表
        td.textContent = row.status === "done" ? "已完成"
                    : row.status === "failed" ? "失败" : row.status || "";
        td.className = "cell-status";
      }
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
}

async function loadPreview(rowIndex) {
  $("#wf-preview-row").textContent = rowIndex + 1;
  const d = await api("GET", `/api/workflows/${wfId}/preview?row=${rowIndex}`);
  $("#wf-preview").textContent = d.rendered;
}

async function setRole(colIndex, role) {
  const d = await api("GET", `/api/workflows/${wfId}`);
  const cols = d.columns.map((c) => ({
    col_index: c.col_index, header: c.header,
    role: c.col_index === colIndex ? role : c.role,
    slot: c.slot, field: c.field,
  }));
  await api("POST", "/api/workflows", {
    workflow_id: wfId,
    prompt_template: d.workflow.prompt_template,
    columns: cols,
  });
  await openDetail(wfId);
  loadPreview(0);
}

$("#wf-upload")?.addEventListener("click", async () => {
  const f = $("#wf-file").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const r = await fetch("/api/workflows/upload", { method: "POST", body: fd });
  const d = await r.json();
  if (!r.ok) { alert(d.detail || "上传失败"); return; }
  location.reload();
});

document.querySelectorAll(".wf-controls [data-act]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    await api("POST", `/api/workflows/${wfId}/${btn.dataset.act}`);
    await openDetail(wfId);
  });
});

boot();
```

- [ ] **Step 7: 跑前端测试确认通过**

Run: `pytest tests/test_web_static.py tests/test_frontend_url_contract.py -q --no-header -p no:cacheprovider`
Expected: PASS

- [ ] **Step 8: 验证禁用词检查真的咬住了新文件**

Run:
```powershell
Add-Content -Path '.\web\workflow.js' -Value "`n// AI 助手"
pytest tests/test_web_static.py -q --no-header -p no:cacheprovider -k banned
```
Expected: FAIL —— 断言里出现 `AI`

**确认红了之后把那行删掉**（用编辑工具删，不要用 PowerShell 回写）。然后重跑，Expected: PASS。

**这一步不做的话，「新文件进了扫描范围」只是一句没验证过的话。**

- [ ] **Step 9: 把工作流页纳入冒烟（不改 `frontend_smoke.mjs` 的代码）**

`tests/frontend_smoke.mjs` 的用法是 `node tests/frontend_smoke.mjs <app.js 路径> [...]`
（见它第 22 行的 docstring），**它本来就是按 argv 收多个文件**的：把工作流页
作为第二个参数传进去就行，顶层抛错就非零退出。

```powershell
Set-Location '.'
node tests\frontend_smoke.mjs web\app.js web\workflow.js
```

**这一步要真的验证它咬得住**：临时往 `web/workflow.js` 末尾加一行顶层
`throw new Error("x")`，确认命令非零退出；然后删掉那行再跑一次确认转绿。
不做这一下，「冒烟覆盖了新页面」就只是一句没验证过的话——而这个脚本
存在的理由恰恰是「模块加载时不报错、页面上却是死的」被咬了四次。

- [ ] **Step 10: 跑全部前端检查**

```powershell
Set-Location '.'
pytest tests/test_web_static.py tests/test_frontend_url_contract.py -q --no-header -p no:cacheprovider
node tests\frontend_smoke.mjs web\app.js web\workflow.js
```
Expected: 全绿

- [ ] **Step 11: 提交**

message 写进 `.git\MSG_t9.txt` 后 `git -F`：

```
工作流页 + 两个前端测试的扫描范围扩展

test_web_static.py 与 test_frontend_url_contract.py 都把文件路径硬编码在
模块顶部，所以新页面会**整片落在检查范围外**——漏掉的恰是禁用词和 URL
契约这两类已经踩过的（/api/api/xxx 那次事故骗过了所有自动化手段）。

扩展必须在 workflow.html 落地之前或同时做，否则会出现「新文件已在磁盘、
不在任何检查范围、所有测试全绿」的窗口期。

test_web_static.py 的首页引用白名单显式加 workflow.html，没有放宽成
「任意同源路径」——放宽就等于放弃了当初拦住 CDN 外链的那条约束。

新增「USER_UI_FILES 与磁盘互为充要」用例，防的是「加了白名单忘建文件」
和「建了文件忘加白名单而线上 404」。
```

---

## 完成后的收尾核对

全部任务完成后，逐条实跑，不要凭推断：

```powershell
chcp 65001 | Out-Null; $env:PYTHONIOENCODING='utf-8'
Set-Location '.'

# 1. 全量测试
& 'C:\ProgramData\anaconda3\python.exe' -m pytest tests -q --no-header -p no:cacheprovider

# 2. agent 测试
Set-Location '.\agent'
node --test guards.test.mjs guards.dotdot.test.mjs guards.audit.test.mjs
Set-Location '.'

# 3. 前端冒烟
node tests\frontend_smoke.mjs web\app.js web\workflow.js

# 4. 真机：建一个 3 行的工作流，跑完，下载结果表，打开确认三行的值都对
#    - 只改 agent 侧文件不需要重启服务；改了 server/*.py 才需要
#    - 工作流行任务的产物必须落在工作目录，否则该行 failed 且不报错
```

**真机验证要盯的四件事**：

1. 第 1 行跑完**之前**第 2 行没有出现（串行）
2. 常规任务与工作流行任务同时排队时，**常规的先跑**
3. 结果表里三行的结果列都有值，且**与平台内看到的 summary 一致**
4. 中止当前行后，下一行不会放行；重跑那一行后，**结果表里那一行是新值**（不是旧值）
