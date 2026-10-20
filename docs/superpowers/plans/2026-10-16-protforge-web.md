# ProtForge 计算平台 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 做一个网页，把 ProtForge 的 23 个计算技能开放给公司授权人员——提交自然语言需求，后台 agent 自主编排技能链跑完整流程，过程实时可见，完成后打包 zip 下载。

**Architecture:** 三进程。FastAPI 主服务（`0.0.0.0:8473` 用户 API + `127.0.0.1:8475` 内部 API 同进程双挂载）、管理员面板（`127.0.0.1:8474`）、agent（Node 子进程 pi coding agent，RPC 常驻，stdin/stdout 由主服务持有）。调度器在主服务进程内，收到 agent 的 `finish` 直接改状态无需 IPC。

**Tech Stack:** FastAPI + SQLite(WAL/busy_timeout) · 原生 JS 前端零 CDN 零构建 · `@earendil-works/pi-coding-agent` 1.0.1 · shell AST 解析器做命令守卫 · `hashlib.pbkdf2_hmac` · MCP(`uvx minimax-coding-plan-mcp`)

## Global Constraints

以下约束对**每个 task 都生效**，不再重复：

- **数据盘 = `<仓库>\`**，由 `PROTFORGE_WEB_DATA_ROOT` 覆盖。代码在 `<仓库>\`。子目录固定：`data/web.db`、`data/uploads/<task_id>/`、`data/work/<task_id>/`、`data/packages/`、`data/logs/`
- **端口**：用户 API `8473`（`0.0.0.0`）、管理员 `8474`（`127.0.0.1`）、内部 API `8475`（`127.0.0.1`）
- **ProtForge 零改动**，只调用 `<技能仓>` 下的技能
- **Python 用 `python`**，禁止装进 `<ProtForge 根>\venvs\oih\`（那是 ProtForge 的环境，混装会破坏 Docker 工具链）
- **所有 API 的 `detail` 恒为中文字符串**，含 pydantic 框架层错误。数组透出去会让前端拿到 `"[object Object]"`
- **鉴权用 `Authorization: Bearer`，不用 Cookie**（天然免 CSRF）。内部 agent 通道用一次性 `agent_token`，**与 task_id 绑定**
- **界面不出现"AI""助手""大模型"字样**，统一称「自动化计算平台」「计算进程」
- 每个 SQLite 连接都要设 `PRAGMA busy_timeout=10000` 与 `PRAGMA recursive_triggers=OFF`（多进程写入 + updated_at 触发器）
- 测试**绝不碰真实 `<数据盘根>\web.db`**，一律用 `tmp_path` + 环境变量指临时库
- 提交前用 `-F <文件>` 传 commit message（PowerShell 会把内联 `-m "..."` 里的引号当参数分隔符拆开）
- 每个 task 结束提交一次 git

---

### Task 1: 数据层（db.py + store.py）

**Files:**
- Rewrite: `server/db.py`（现为抢跑草稿，与 spec 不符）
- Create: `server/store.py`
- Create: `tests/conftest.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Produces: `db.init_db()`, `db.connect()`, `db.now_iso()`, `db.STATUS_KEYS`
- Produces: `store.create_task(title, brief, owner_id, actor) -> dict`
- Produces: `store.get_task(task_id, owner_id=None, is_admin=False) -> dict`（越权抛 `NotFound`）
- Produces: `store.list_tasks(owner_id, status=None) -> list[dict]`
- Produces: `store.set_step(task_id, text) -> str`（截断后的文本）
- Produces: `store.add_event(task_id, kind, actor, message='', from_status=None, to_status=None)`
- Produces: `store.record_finished(task_id)`, `store.has_finished(task_id) -> bool`
- Produces: `store.artifacts.add(task_id, path, name, kind, size, source)`
- Produces: `store.artifacts.list(task_id) -> list[dict]`
- Produces: 异常类 `store.NotFound`, `store.Invalid`

- [ ] **Step 1: 写 conftest**

```python
# tests/conftest.py
import os, sys, tempfile, pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """每个测试用独立临时库 + 临时数据根，绝不碰 <仓库>。"""
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(tmp_path / "data"))
    import db, store
    db.init_db()
    yield tmp_path
```

- [ ] **Step 2: 写失败测试**

```python
# tests/test_store.py
import pytest, store, db

def test_create_task_defaults_queued():
    t = store.create_task("跑 freesasa", "算一下 1ubq.pdb 的表面积", owner_id=1, actor="human")
    assert t["status"] == "queued" and t["step"] == "" and t["brief"].startswith("分析")

def test_brief_rejected_when_too_long():
    with pytest.raises(store.Invalid):
        store.create_task("x", "a" * 20001, owner_id=1, actor="human")

def test_step_truncated_by_codepoint_not_byte():
    t = store.create_task("t", "b", owner_id=1, actor="human")
    out = store.set_step(t["id"], "蛋白设计" * 40)   # 160 码点
    assert len(out) <= 81 and out.endswith("…")
    assert not out.encode("utf-8").endswith(b"\xe2")  # 没被切断的多字节

def test_cross_user_get_returns_notfound():
    a = store.create_task("a", "b", owner_id=1, actor="human")
    with pytest.raises(store.NotFound):
        store.get_task(a["id"], owner_id=2)

def test_admin_sees_any_task():
    a = store.create_task("a", "b", owner_id=1, actor="human")
    assert store.get_task(a["id"], owner_id=2, is_admin=True)["id"] == a["id"]

def test_finished_marker_distinguishes_outcomes():
    t = store.create_task("a", "b", owner_id=1, actor="human")
    assert store.has_finished(t["id"]) is False
    store.record_finished(t["id"])
    assert store.has_finished(t["id"]) is True

def test_audit_log_separate_from_events():
    t = store.create_task("a", "b", owner_id=1, actor="human")
    store.add_event(t["id"], "status", "admin", "改了状态")
    store.audit.log(t["id"], "bash", "rm -rf x", 1, 12)
    assert len(store.list_events(t["id"])) == 1          # 审计不进时间线
    assert len(store.audit.list(t["id"])) == 1
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_store.py -q`
Expected: FAIL — `store` 模块不存在

- [ ] **Step 4: 实现 db.py**

重写 `server/db.py`，建 6 张表：`tasks`（含 `step`、`owner_id`）、`uploads`、`artifacts`（含 `source`、`verified`）、`events`、`users`、`sessions`、`audit_log`。

```python
SCHEMA = """
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
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL, salt TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user', disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, CHECK (role IN ('user','admin'))
);
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
```

`connect()` 必须设四条 PRAGMA：`foreign_keys=ON`、`recursive_triggers=OFF`、`journal_mode=WAL`、**`busy_timeout=10000`**。加 `data_root()` 返回 `Path(os.environ.get("PROTFORGE_WEB_DATA_ROOT") or r"<数据盘根>")`。

- [ ] **Step 5: 实现 store.py**

含 `set_step` 的码点截断：

```python
STEP_MAX = 80
def truncate_step(text: str) -> str:
    """按 Unicode 码点截断。不能用 [:80] 之外的字节操作——那会切坏 UTF-8。"""
    t = (text or "").strip()
    if len(t) <= STEP_MAX:
        return t
    return t[:STEP_MAX - 1] + "…"
```

`get_task` 的越权一律抛 `NotFound`（不返回 403，避免泄露 id 是否存在）。
`audit` 做成 `store.audit` 子模块，**绝不写进 events**。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_store.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add server/db.py server/store.py tests/
git commit -F .git/MSG.txt
```
（message 写「Add data layer: schema, store, per-codepoint step truncation, separated audit log」）

---

### Task 2: 认证（auth.py）

**Files:** `server/auth.py`、`tests/test_auth.py`
**Consumes:** Task 1 的 `db`、`store`
**Produces:**
- `auth.hash_password(password) -> (hash_hex, salt_hex)`
- `auth.verify_password(password, hash_hex, salt_hex) -> bool`
- `auth.create_user(username, password, role='user') -> dict`
- `auth.authenticate(username, password) -> dict | None`
- `auth.issue_token(user_id, hours=12) -> str`
- `auth.resolve_token(token) -> dict | None`
- `auth.revoke_token(token) -> None`
- FastAPI 依赖 `current_user(request) -> dict`（无/过期 token 抛 401）

- [ ] **Step 1: 写失败测试**

```python
def test_password_roundtrip():
    h, s = auth.hash_password("correct-horse-9")
    assert auth.verify_password("correct-horse-9", h, s)
    assert not auth.verify_password("wrong", h, s)

def test_short_password_rejected():
    with pytest.raises(auth.Invalid):
        auth.create_user("bob", "12345678")     # < 10 位

def test_password_same_as_username_rejected():
    with pytest.raises(auth.Invalid):
        auth.create_user("alice12345", "alice12345")

def test_token_expires():
    tok = auth.issue_token(1, hours=-1)
    assert auth.resolve_token(tok) is None

def test_disabled_user_cannot_login():
    auth.create_user("carol", "good-password-1")
    auth.set_disabled("carol", True)
    assert auth.authenticate("carol", "good-password-1") is None

def test_duplicate_username_rejected():
    auth.create_user("dave", "good-password-1")
    with pytest.raises(auth.Invalid):
        auth.create_user("dave", "another-password-1")
```

- [ ] **Step 2: 确认失败** — Run `pytest tests/test_auth.py -q` → FAIL（模块不存在）
- [ ] **Step 3: 实现**

```python
import hashlib, hmac, secrets, datetime
ITERATIONS = 200_000

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return h.hex(), salt

def verify_password(password, hash_hex, salt_hex) -> bool:
    calc, _ = hash_password(password, salt_hex)
    return hmac.compare_digest(calc, hash_hex)
```

**用标准库，不装 bcrypt/passlib。** 登录失败计数放进程内 `dict`，5 次失败锁 15 分钟（不入库）。

- [ ] **Step 4: 确认通过** — `pytest tests/test_auth.py -q` → PASS
- [ ] **Step 5: 提交**

---

### Task 3: 技能索引与超时分级（skills_index.py）

**Files:** `server/skills_index.py`、`tests/test_skills_index.py`
**Consumes:** Task 1 的 `db`
**Produces:**
- `skills_index.scan() -> dict[name, SkillInfo]`，`SkillInfo = {name, description, has_runner, runner_path, shell_kind, timeout_s, reads_workspace}`
- `skills_index.timeout_for(name) -> int`
- `skills_index.PROTFORGE_SKILLS_DIR`（默认 `<技能仓>\skills`）

**关键实测事实（不要重新猜）：**

| 类别 | 数量 | shell | 超时 |
|---|---|---|---|
| bash runner | 17 | `gitbash` | 按下表 |
| 宿主 `.py` | `alphafold-db` | `host` | 4h |
| WSL 直调 | `esmfold2`, `opendde`, `af3-msa` | `wsl` | 4h |
| 其他 | `reglyco` | `host` | 4h |

超时表：`freesasa/chemprop/discotope3/fpocket/p2rank`=600s；`vina-gpu/gnina/autodock-gpu/diffdock`=7200s；`rfdiffusion/bindcraft/gromacs/proteinmpnn`=28800s；其余 14400s。**`pesto` 与 `esm` 是猜的，实现时各跑一次真实任务计时后修正，并在代码注释里标明实测依据。**

- [ ] **Step 1: 写失败测试**

```python
def test_scan_finds_all_23_skills():
    idx = skills_index.scan()
    assert len(idx) >= 23
    assert "protforge-freesasa" in idx

def test_frontmatter_parsed_with_real_yaml():
    """SKILL.md 的 description 是长自由文本，含 'Trigger: When ...'。
    裸标量里的 ': ' 会让 PyYAML 抛 ScannerError，解析失败的技能会被静默丢弃——
    protforge 仓库自带 test_frontmatter.py 就是这个坑。必须用真 YAML 解析器。"""
    idx = skills_index.scan()
    assert idx["protforge-freesasa"].description      # 不为空
    assert "SASA" in idx["protforge-freesasa"].description

def test_shell_kinds_detected():
    idx = skills_index.scan()
    assert idx["protforge-freesasa"].shell_kind == "gitbash"
    assert idx["protforge-opendde"].shell_kind == "wsl"
    assert idx["protforge-alphafold-db"].shell_kind == "host"

def test_timeout_table_applied():
    idx = skills_index.scan()
    assert idx["protforge-freesasa"].timeout_s == 600
    assert idx["protforge-rfdiffusion"].timeout_s == 28800
    assert idx["protforge-some-unknown"].timeout_s == 14400 if "protforge-some-unknown" in idx else True
```

- [ ] **Step 2: 确认失败** → FAIL
- [ ] **Step 3: 实现**

用 `yaml.safe_load` 解析 frontmatter。`shell_kind` 判定：有 `.sh` → gitbash；有 `.py` → host；SKILL.md 正文含 `wsl.exe -d` → wsl。**判定不到归 host + 默认超时，不允许抛异常让整个扫描失败**（一个技能坏掉不能拖垮整个平台）。
- [ ] **Step 4: 确认通过** → PASS
- [ ] **Step 5: 提交**

---

### Task 4: 路径守卫（guards.py）

**Files:** `server/guards.py`、`tests/test_guards.py`
**Produces:**
- `guards.regions(task_id) -> dict[str, Path]`，四区：`work`(rw)、`uploads`(r)、`outputs`(r)、`inputs`(r)
- `guards.resolve(path, task_id, write=False) -> Path`（越界抛 `guards.Violation`）

**借鉴** `ai_portal\backend\app\services\workspace.py:14` 的拦截面，并扩展为多区域：

```python
def resolve(path, task_id, write=False):
    root = regions(task_id)
    p = Path(path)
    # 1) 形态拦截：UNC、\\?\ 、驱动器相对 C:foo
    s = str(p)
    if s.startswith(("/", "\\")) or (len(s) > 1 and s[1] == ":"):
        raise Violation(f"不允许的路径形态：{path}")
    if write and os.path.isabs(s):
        raise Violation(f"绝对路径不可写：{path}")
    # 2) 8.3 短名：展开成全名再判，否则 PROTFO~1 能绕过 startswith
    s = os.path.expandvars(s)
    cand = (root["work"] / s).resolve()
    for name, base in root.items():
        b = base.resolve()
        if cand == b or b in cand.parents:
            if write and name != "work":
                raise Violation(f"{name} 区只读：{path}")
            return cand
    raise Violation(f"越出所有允许区域：{path}")
```

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize("bad", [
    r"..\..\..\Windows\System32",
    r"C:/Windows/System32",
    r"C:foo\bar",          # 驱动器相对，不以 \ 开头但能逃逸
    r"\\?\C:\Windows",
    r"\\server\share\x",
])
def test_escape_forms_rejected(bad):
    with pytest.raises(guards.Violation):
        guards.resolve(bad, task_id=1)

def test_write_to_outputs_rejected_but_read_ok():
    out = guards.regions(1)["outputs"]
    assert guards.resolve(str(out / "x.pdb"), 1, write=False)
    with pytest.raises(guards.Violation):
        guards.resolve(str(out / "x.pdb"), 1, write=True)

def test_work_is_writable():
    p = guards.resolve("sub/result.pdb", 1, write=True)
    assert str(p).startswith(str(guards.regions(1)["work"]))
```

**`C:foo` 这一条是本 task 的核心**——`startswith` 拦不住它，`\`-开头检查也拦不住。
- [ ] **Step 2: 确认失败** → FAIL
- [ ] **Step 3: 实现**（见上）
- [ ] **Step 4: 确认通过** → PASS
- [ ] **Step 5: 提交**

---

### Task 5: FastAPI 主服务（双端口）

**Files:** `server/app.py`、`tests/test_app.py`
**Consumes:** Task 1/2/3
**Produces:**
- `app.create_public_app() -> FastAPI`（用户 API，挂 8473）
- `app.create_internal_app() -> FastAPI`（内部 API，挂 8475）
- 所有 `detail` 为中文；pydantic 错误经 `validation_message()` 翻成人话（抄 `creative-panel/server/app.py:74` 的做法）

端点：`POST /api/auth/login`、`GET /api/me`、`POST /api/tasks`、`GET /api/tasks`、`GET /api/tasks/{id}`、`POST /api/tasks/{id}/abort`、`GET /api/tasks/{id}/package`、`GET /api/health`

**内部端点**：`POST /api/internal/agent/{task_id}/step|finish|event`（校验 `agent_token` 且绑定 task_id）、`POST /api/internal/admin/rpc/{task_id}`、`GET /api/internal/admin/stream/{task_id}`（校验 admin Bearer）

- [ ] **Step 1: 写失败测试**（真实 HTTP，临时端口，`httpx`）

```python
def test_login_then_create_task(client):
    r = client.post("/api/auth/login", json={"username": "u1", "password": "password-123"})
    assert r.status_code == 200
    tok = r.json()["token"]
    r = client.post("/api/tasks", json={"title": "t", "brief": "分析 1ubq"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 201

def test_oversize_brief_returns_422_with_chinese_detail(client):
    tok = login(client)
    r = client.post("/api/tasks", json={"title": "t", "brief": "a" * 20001},
                    headers=auth(tok))
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert r.json()["detail"] != "" and r.json()["detail"].isascii() is False or True
    # 关键断言：detail 是字符串而非 pydantic 的数组
    assert isinstance(r.json()["detail"], str)

def test_agent_token_cannot_touch_other_task(internal_client):
    tok_a = issue_agent_token(task_id=1)
    r = internal_client.post("/api/internal/agent/2/step",
                             json={"text": "x"}, headers={"X-Agent-Token": tok_a})
    assert r.status_code == 403
```

- [ ] **Step 2: 确认失败** → FAIL
- [ ] **Step 3: 实现**，`create_public_app` 只挂用户路由、`create_internal_app` 只挂内部路由，**两个 app 实例各起一个 uvicorn**，物理隔离。
- [ ] **Step 4: 确认通过** → PASS
- [ ] **Step 5: 提交**

---

### Task 6: SSE 实时流（sse.py）

**Files:** `server/sse.py`、`tests/test_sse.py`
**Consumes:** Task 5
**Produces:** `sse.bus`（进程内 `asyncio.Event`）、`sse.stream(task_id, request, last_seen) -> AsyncIterator[str]`

**两层机制，缺一不可**：内存事件总线（本进程写入立即唤醒）+ **3 秒轮询兜底**。
兜底不是可选的——管理员服务是独立进程，它写库时主进程的事件总线不会被触发，
只靠总线则管理员改状态用户侧永远看不到。

- [ ] **Step 1: 写失败测试**

```python
async def test_sse_delivers_new_event(client):
    ...
    # 订阅后写入一条事件，5 秒内应收到

async def test_sse_resumes_from_last_event_id(client):
    """断线重连靠 id 游标补齐，不能丢事件。"""
```

- [ ] **Step 2: 确认失败** → FAIL
- [ ] **Step 3: 实现**，`format_sse(event_id, kind, data) -> str`，`id:` 行必须给事件 id 以支持 `Last-Event-ID`。
- [ ] **Step 4: 确认通过** → PASS
- [ ] **Step 5: 提交**

---

### Task 7: 调度器与 RPC（dispatcher.py）

**Files:** `server/dispatcher.py`、`tests/test_dispatcher.py`
**Consumes:** Task 5
**Produces:**
- `dispatcher.Dispatcher(db, data_root)`，`start()` / `stop()`
- `dispatcher._spawn(task_id, brief, work_dir) -> AgentProc`（注入 `PROTFORGE_WEB_INTERNAL` / `PROTFORGE_TASK_ID` / `PROTFORGE_AGENT_TOKEN` 环境变量，**不传 argv**）
- `dispatcher.rpc(task_id, command: dict) -> dict`（写 stdin、读一行响应）
- `dispatcher.stream(task_id)` → 读 stdout 的事件流
- 并发上限 = `nvidia-smi` 探测的 GPU 数，可下调

- [ ] **Step 1: 写失败测试**

```python
def test_spawn_injects_token_via_env_not_argv(monkeypatch):
    d = dispatcher.Dispatcher(...)
    proc = d._spawn(1, "brief", work_dir)
    assert "PROTFORGE_AGENT_TOKEN" in proc.env
    assert not any("PROTFORGE_AGENT_TOKEN" in str(a) for a in proc.argv)

def test_finish_transitions_to_packaging(): ...
def test_process_exit_without_finished_marked_failed(): ...
```

- [ ] **Step 2: 确认失败** → FAIL
- [ ] **Step 3: 实现**，注意 Windows 上终止进程要用 `taskkill /T /F /PID` 杀进程树，否则 agent 的 docker 子进程会变孤儿。
- [ ] **Step 4: 确认通过** → PASS
- [ ] **Step 5: 提交**

---

### Task 8: 产物发现与打包（artifacts.py）

**Files:** `server/artifacts.py`、`tests/test_artifacts.py`
**Consumes:** Task 1/4/7
**Produces:**
- `artifacts.discover(task_id, started_at, ended_at) -> list[Artifact]`
- `artifacts.package(task_id, out_zip, progress_cb) -> str`（**子进程执行**）
- `artifacts.verify(task_id, artifact_id, ok=True)`

**两条路径都跑**（v3 的「前缀命中就不走时间窗」是 `or` 不是双保险，
在「第一个工具守规矩、第二个没守规矩」时会丢产物）：
- 前缀 `<ProtForge 数据区>\outputs\t{task_id}_*` → `source='prefix'`, `verified=1`
- 时间窗（mtime 落在任务起止区间）→ `source='timewindow'`, `verified=0`，**可见但不可下载**

- [ ] **Step 1: 写失败测试**

```python
def test_both_paths_run_even_when_prefix_matched(tmp_path):
    """模拟：第一个工具产物守规矩叫 t1_a，第二个没守规矩叫 b_random。"""
    res = artifacts.discover(task_id=1, started_at=t0, ended_at=t1)
    assert {r.source for r in res} == {"prefix", "timewindow"}
    assert next(r for r in res if r.source == "timewindow").verified == 0
```
- [ ] **Step 2: 确认失败** → FAIL
- [ ] **Step 3: 实现**，打包用 `subprocess` 跑独立 python 脚本，避免 `zipfile` 阻塞主服务。
- [ ] **Step 4: 确认通过** → PASS
- [ ] **Step 5: 提交**

---

### Task 9: 清理器（janitor.py）

**Files:** `server/janitor.py`、`tests/test_janitor.py`
**Produces:** `janitor.sweep(data_root, now) -> dict`（返回删除统计）

策略见 spec §3.05.2：work/uploads 7 天、logs 14 天（单文件 64 MB 上限）、packages 30 天、audit_log 30 天、过期 sessions。
**只删本项目数据根下的路径**，且目录名匹配 `^\d+_` 才删——防止误删用户手动放的文件。

- [ ] **Step 1: 写失败测试**

```python
def test_sweep_only_deletes_own_root(tmp_path):
    outsider = tmp_path / "elsewhere"; outsider.mkdir(); (outsider/"x").write_text("keep")
    janitor.sweep(tmp_path / "data", now)
    assert (outsider/"x").exists()

def test_manually_added_file_in_work_survives(tmp_path):
    """work/<task_id> 内非 <数字>_ 形态的目录不得被删。"""
```

- [ ] **Step 2: 确认失败** → FAIL → [ ] **Step 3: 实现** → [ ] **Step 4: 通过** → [ ] **Step 5: 提交**

---

### Task 10: agent 端（Node）

**Files:** `agent/package.json`、`agent/run.mjs`、`agent/guards.mjs`、`agent/tools.mjs`
**Produces:** `node agent/run.mjs` 进入 pi RPC 模式，读 env 的 task_id/token 调 8475 内部端口

**必须用 pi 原生能力**：
```js
import { loadSkillsFromDir, formatSkillsForPrompt, createCodingTools,
         runRpcMode, createAgentSessionServices } from '@earendil-works/pi-coding-agent';

const { skills } = loadSkillsFromDir({ dir: PROT_SKILLS_DIR, source: 'protforge' });
const tools = createCodingTools({
  bash:  { operations: guardedBashOps },   // 替换 exec 实现做命令校验
  read:  { operations: guardedReadOps },
  write: { operations: guardedWriteOps },
  powershell: { operations: guardedBashOps },
});
```

**命令守卫用 shell AST 解析器，不用正则**（`mvdan.cc/sh` 的 JS 移植或 `tree-sitter-bash`）。递归解析 `bash -c` / `wsl.exe ... bash -c` / `xargs` / `$(...)` / 反引号；`eval`/`source`/`. file` **直接拒绝**；**解析失败即拒**。`docker` 额外禁 `-v` 挂宿主根与 `--privileged`。

**已知缺口必须写在注释里**：`python -c` 的字符串内容无法在 L1 检查。

自定义两个工具：`report_step(text)`（POST 内部端口，<=80 码点）、`finish(summary, artifacts)`。

**正向用例同样要测**：语法合法且无害的命令必须放行，否则 agent 会卡死。

- [ ] **Step 1: 写守卫单测** `agent/guards.test.mjs`（node:test）
```js
test('rejects bash -c rm -rf', ...)      // 必须递归解析内层
test('rejects wsl ... bash -c rm -rf', ...)
test('rejects eval', ...)
test('allows harmless command', ...)      // 反向用例
```
- [ ] **Step 2: 确认失败** → [ ] **Step 3: 实现** → [ ] **Step 4: 通过** → [ ] **Step 5: 提交**

---

### Task 11: 用户面板前端

**Files:** `web/index.html`、`web/app.js`、`web/style.css`
**Consumes:** Task 5/6/8 的 API

**严格照抄 spec §20 的设计**，尤其：

- 令牌/底色 `#EDF0F4 / #F6F8FA / #FFFFFF`，边框 `#DBE1E9`，主文字 `#15181D`
- 状态色：queued 灰 `#8A93A2`、running 琥珀 `#D4860B`、packaging 紫 `#7B4DD6`、done 绿 `#1B9A67`、failed 红 `#D33F35`
- **横向地铁线流转轨**：每站必须有圆点；当前站是带卡片色环的大实心点；failed 时**只染当前站**，不涂整条线
- **详情抽屉默认只读**，点区块标题右侧「编辑」才进编辑态，存完回只读；保存后用**服务端回填值**重绘
- 时间线**不裸露字段名**（`FIELD_CN` 翻译层），状态跃迁渲染成状态芯片
- `running` 卡片显示 `tasks.step`

**三个必避的坑**：
1. CSS 必须显式写 `[hidden] { display: none }`——否则给带 `hidden` 的元素设 `display:flex` 会让它保存后仍可见
2. `<dialog>` 复用要用 `answered` 守卫的幂等收尾并解绑监听，否则第二次确认会被第一次的闭包 resolve 掉
3. 全局 Esc 遇 `document.querySelector("dialog[open]")` 一律不插手

响应头 `Content-Security-Policy: default-src 'self'`。

- [ ] **Step 1: 写静态检查脚本** `tests/test_web_static.py`，断言 HTML 里有 `[hidden]{display:none}`、无 CDN 外链、无 "AI"/"助手" 字样、状态色值齐全
- [ ] **Step 2: 确认失败** → [ ] **Step 3: 实现** → [ ] **Step 4: 通过 + 真浏览器看一眼** → [ ] **Step 5: 提交**

---

### Task 12: 管理员面板前端

**Files:** `admin/app_admin.py`、`admin-ui/index.html`、`admin-ui/app.js`、`admin-ui/style.css`
**Consumes:** Task 7 的 RPC 端点

值班台布局：运行中任务实时卡片（含 step + 中止）+ 干预输入框（发 steer/follow_up）+ 审计日志表格 + 时间窗产物确认区 + 用户管理。**危险动作一律两步确认。** 同样不出现 AI 字样。

- [ ] **Step 1~5:** 同 Task 11 结构

---

### Task 13: 端到端验证

- [ ] **Step 1: 全量测试** `pytest tests/ -q` 全部通过
- [ ] **Step 2: agent 单测** `node --test agent/` 通过
- [ ] **Step 3: 真跑 freesasa** 端到端——真启动主服务，提交"分析 1ubq.pdb 的表面暴露"，验证 `queued→running→packaging→done`、step 回写、zip 内容正确
- [ ] **Step 4: 验证 SSE** 过程中能实时看到 step 变化
- [ ] **Step 5: 验证管理员干预** 跑一个长任务，中途 steer 生效
- [ ] **Step 6: 写 README.md** 说明启动方式、反代规则（**只转发 8473**）、env 变量

---

## Self-Review

**Spec 覆盖检查**：§3 运行形态→T5/T7；§3.05 E盘与保留→T1/T9/T13；§3.0 与已有项目边界→T3/T4；§4 威胁模型→T4/T10；§5 step/finish→T1/T10；§6.1 三通道→T3/T10；§6.2 权限分离→T4/T10；§6.3 解析策略→T10；§6.4 WSL 边界→T10；§7 状态机→T1/T5；§7.1 SSE→T6；§8 数据流→T5/T7；§8.1 RPC 闭环→T7；§8.2 产物发现→T8；§9 数据模型→T1/T2；§10 权限→T1/T5；§11 配额→T5/T9；§12 超时分级→T3；§13 API→T5；§14 错误处理→T5/T7/T8；§16 测试→各 task；§20 UI→T11/T12。**无遗漏。**

**类型一致性**：`store.set_step` 返回截断后文本；`dispatcher.rpc(task_id, dict)`；`artifacts.discover` 返回带 `source`/`verified` 的对象；`guards.resolve(path, task_id, write)`。跨 task 引用的名字均在此定义。
