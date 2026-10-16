"""数据库连接、schema 与存储层约定。

与 creative-panel 同构：FastAPI + SQLite，本机单人用，无鉴权。
这个文件只放"容易踩坑的规则"，业务查询在 store.py。

命名说明：这里的主语是 **task（任务）** 而不是 idea（创意）。任务有一个明确的
终态产物（zip 包）和一次计算过程，而创意只是一张会流动的卡片，两者的时间线
语义不同——任务线上"打包中"是真实的工作步骤，不是"正在制作"那种进度隐喻。
"""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 状态机是**固定、线性**的，不能加列改名。status 的 CHECK 约束与这里必须一致，
# 改一边不改另一边会让 libsqlite 静默拒绝插入（只在前端加状态不改 schema 是
# 最容易犯且最难查的一种）。
#
#   queued    已排队，等计算进程接走
#   running   计算进程已接走，ProtForge 正在跑
#   packaging 产物已落地，正在收拢成 zip
#   done      完成，可下载
#   failed    失败（模型报错 / 工具链断了 / 预算耗尽）
#
# failed 不在主链上：它是**终点**而不是阶段，所以不进流转轨的格数，
# 与 creative-panel 把「卡住」做成布尔位而不是第六个状态是同一个取舍。
STATUSES = [
    ("queued", "已排队"),
    ("running", "计算中"),
    ("packaging", "打包中"),
    ("done", "已完成"),
]
STATUS_KEYS = [k for k, _ in STATUSES]
STATUS_LABEL = dict(STATUSES)
STATUS_INDEX = {k: i for i, k in enumerate(STATUS_KEYS)}

# failed 与主链互斥：到 failed 即终止，不会再回主链。
TERMINAL_STATUSES = ("done", "failed")

SCHEMA_VERSION = 1

SCHEMA = """
-- 一个计算任务。brief 是用户用自然语言写的需求全文，agent 据此决定
-- 调哪些 ProtForge 技能、按什么顺序调、填什么参数——所以它必须是**原样保存**
-- 的完整文本，不能像标题那样截断，agent 看不到全文就会自己编。
CREATE TABLE IF NOT EXISTS tasks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  brief        TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'queued',
  blocked      INTEGER NOT NULL DEFAULT 0,
  blocked_note TEXT NOT NULL DEFAULT '',
  priority     INTEGER NOT NULL DEFAULT 0,
  uploads_dir  TEXT NOT NULL DEFAULT '',   -- 本任务上传文件所在目录
  output_dir   TEXT NOT NULL DEFAULT '',   -- ProtForge 产物目录（agent 回填）
  package_path TEXT NOT NULL DEFAULT '',   -- zip 绝对路径
  summary      TEXT NOT NULL DEFAULT '',   -- agent 的收口说明（中文，直接给人看）
  error        TEXT NOT NULL DEFAULT '',
  session_id   TEXT NOT NULL DEFAULT '',   -- 计算进程标识，便于定位是哪个 agent 在跑
  position     REAL NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  done_at      TEXT,
  CHECK (status IN ('queued','running','packaging','done','failed'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- 用户上传的输入文件。只存路径，文件本体落在 uploads_dir 下。
CREATE TABLE IF NOT EXISTS uploads (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  filename   TEXT NOT NULL,
  path       TEXT NOT NULL,
  size       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_task ON uploads(task_id, id);

-- 产物清单。agent 跑完后由 runner 扫 output_dir 收进来，网页按它渲染下载列表。
CREATE TABLE IF NOT EXISTS artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  path       TEXT NOT NULL,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'file',
  size       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, id);

-- 时间线。日志走这里（kind='log'），actor 区分是人、是 agent、还是系统。
-- 计算过程的 stdout 只在 log 事件里摘要留存，完整日志在 logs/ 下按任务存文件。
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  from_status TEXT,
  to_status   TEXT,
  actor       TEXT NOT NULL,
  message     TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  CHECK (kind  IN ('created','status','log','note','upload','package','error','edited')),
  CHECK (actor IN ('human','agent','system'))
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);

-- 自动维护 updated_at。WHEN 子句在承重：去掉它连跨秒场景也会递归。
-- recursive_triggers=OFF 是硬性前提（connect() 里设）。
CREATE TRIGGER IF NOT EXISTS trg_tasks_touch AFTER UPDATE ON tasks
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE tasks SET updated_at = strftime('%Y-%m-%dT%H:%M:%f','now','localtime')
  WHERE id = NEW.id;
END;
"""


def db_path():
    """读 PROTFORGE_WEB_DB，缺省 <项目根>/data/web.db。"""
    env = os.environ.get("PROTFORGE_WEB_DB")
    return Path(env) if env else (ROOT / "data" / "web.db")


def connect():
    """每次调用返回新连接，三条 PRAGMA 已就位。"""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")          # 每连接：不开则级联删除失效
    c.execute("PRAGMA recursive_triggers=OFF")  # 每连接：不开则 touch 触发器递归
    c.execute("PRAGMA journal_mode=WAL")        # 文件属性：建库时设一次即可
    return c


def init_db():
    """建表。建库期允许手工改过 schema 的库存在，所以不做版本化迁移。"""
    c = connect()
    try:
        c.executescript(SCHEMA)
        c.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        c.commit()
    finally:
        c.close()


def now_iso():
    """毫秒精度、无时区，与 creative-panel 同一形态。

    用 :memory: 而不是走 connect()，免得为了取时间戳去 mkdir + 建库，
    顺手污染真实 data/web.db。
    """
    c = sqlite3.connect(":memory:")
    try:
        return c.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%f','now','localtime')").fetchone()[0]
    finally:
        c.close()
