"""数据库连接与 schema。业务查询全在 store.py，这个文件只放"容易踩坑的规则"。

三个进程（主服务 / 管理员面板 / agent 编排）会同时写同一个库，所以每条连接
必须设满四条 PRAGMA，缺一条就是难复现的 "database is locked" 或触发器递归：

    foreign_keys=ON       不开则 ON DELETE CASCADE 静默失效
    recursive_triggers=OFF 不开则 trg_tasks_touch 自己触发自己
    journal_mode=WAL      文件属性，建库时设一次即可
    busy_timeout=10000    多进程写等待锁，其余三条都救不了这条

命名说明：主语是 **task（任务）**。任务有一个明确的终态产物（zip 包）和一次
真实计算过程，时间线语义与 creative-panel 的"创意卡片"不同。
"""
import os
import sqlite3
import time
from pathlib import Path

# 状态机是固定、线性的，不能加列改名。tasks.status 的 CHECK 与这里必须一致，
# 改一边不改另一边会让 libsqlite 静默拒绝插入。
#
#   queued    已排队，等计算进程接走
#   running   计算进程已接走，ProtForge 正在跑
#   packaging 产物已落地，正在收拢成 zip
#   done      完成，可下载
#
# failed 不在主链上：它是**终点**而不是阶段，所以不进流转轨的格数
# （与 creative-panel 把"卡住"做成布尔位而不是第六个状态是同一个取舍）。
STATUSES = [
    ("queued", "已排队"),
    ("running", "计算中"),
    ("packaging", "打包中"),
    ("done", "已完成"),
]
STATUS_KEYS = [k for k, _ in STATUSES]          # 主链 4 态（流转轨画这几格）
STATUS_LABEL = dict(STATUSES)
STATUS_LABEL["failed"] = "失败"
STATUS_INDEX = {k: i for i, k in enumerate(STATUS_KEYS)}

# 合法状态全集 = 主链 + 终态失败。**校验入参要用这个**，用 STATUS_KEYS 会把
# failed 判成非法值，导致失败的任务再也存不回去。
VALID_STATUSES = STATUS_KEYS + ["failed"]
TERMINAL_STATUSES = ("done", "failed")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL, salt TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user', disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, CHECK (role IN ('user','admin'))
);
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
CREATE TABLE IF NOT EXISTS uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  filename TEXT NOT NULL, path TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'file',
  size INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'prefix',
  verified INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, from_status TEXT, to_status TEXT,
  actor TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  CHECK (kind IN ('created','status','log','note','upload','package','error','edited','step','finished')),
  CHECK (actor IN ('human','agent','admin','system'))
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, kind TEXT NOT NULL, command TEXT NOT NULL DEFAULT '',
  exit_code INTEGER, elapsed_ms INTEGER, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_log(task_id, id);
CREATE TRIGGER IF NOT EXISTS trg_tasks_touch AFTER UPDATE ON tasks
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE tasks SET updated_at = strftime('%Y-%m-%dT%H:%M:%f','now','localtime') WHERE id = NEW.id;
END;
"""


def data_root() -> Path:
    """数据盘根。代码在 .，数据一律落在 data。

    测试用 PROTFORGE_WEB_DATA_ROOT 指到 tmp_path，所以**每次调用都现读环境变量**，
    不要 import 时算一次存成模块常量。
    """
    return Path(os.environ.get("PROTFORGE_WEB_DATA_ROOT") or r"data")


def db_path() -> Path:
    """读 PROTFORGE_WEB_DB，缺省 <数据根>/web.db。"""
    env = os.environ.get("PROTFORGE_WEB_DB")
    return Path(env) if env else (data_root() / "web.db")


def _enable_wal(c: sqlite3.Connection) -> None:
    """把 journal_mode 切成 WAL，切不动就重试。

    `PRAGMA journal_mode=WAL` 要短暂拿独占锁，而 busy_timeout 对它**不生效**
    ——它不排队等锁，直接回 SQLITE_BUSY。三个进程同时对着一个还没切过 WAL 的
    库起服务（public / admin / internal 都在启动时调 init_db），后到的那个就会
    吃到 `database is locked`，服务起不来。这不是工作流迁移引入的，但迁移让启动
    期的写窗口变长，撞上的概率更高，所以在这里补上。

    已经是 WAL 就直接返回：journal_mode 是文件属性，转换过一次之后这条语句
    只是读，不再需要锁，也就没有竞争。
    """
    if str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        return
    for attempt in range(50):                      # 50 × 0.1s = 最多等 5s
        try:
            c.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise                              # 只等锁，别的一律上抛
            if attempt == 49:
                raise                              # 等不到就明着失败，别假装建好了
            time.sleep(0.1)


def connect() -> sqlite3.Connection:
    """每次调用返回新连接，四条 PRAGMA 已就位。"""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")         # 排第一：等锁要早于任何写
    c.execute("PRAGMA foreign_keys=ON")            # 每连接：不开则级联删除失效
    c.execute("PRAGMA recursive_triggers=OFF")     # 每连接：不开则 touch 触发器递归
    _enable_wal(c)                                 # 文件属性：建库时设一次即可
    return c


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


def init_db() -> None:
    """建表。建库期允许手工改过 schema 的库存在，所以不做版本化迁移。"""
    c = connect()
    try:
        c.executescript(SCHEMA)
        _add_missing_columns(c)          # 旧库补列；新库这里三个都在，什么也不做
        c.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        c.commit()
    finally:
        c.close()


def now_iso() -> str:
    """毫秒精度、无时区，格式与 trg_tasks_touch 写入的完全一致。

    用 :memory: 而不是走 connect()，免得为了取时间戳去 mkdir + 建库，
    顺手污染真实数据盘上的 web.db。
    """
    c = sqlite3.connect(":memory:")
    try:
        return c.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%f','now','localtime')").fetchone()[0]
    finally:
        c.close()
