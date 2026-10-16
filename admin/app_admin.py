"""管理员面板后端（Task 12）。**独立进程，只绑 127.0.0.1:8474。**

这是值班台的后端，不是用户 API 的第二份拷贝。四条设计：

1. **只绑回环地址。** 公网够不着，所以它才可以直接操作别人的计算任务、
   看审计日志、管用户。用户端口（8473）上**一条管理路由都没有**，
   路由不共存是端口隔离之外的第二道闸。
2. **中间只多一跳。** agent 的 stdin/stdout 句柄在主服务进程里（spec §8.1），
   面板够不着，所以干预一律转发到内部端口的
   `/api/internal/admin/rpc/{task_id}` 与 `/api/internal/admin/stream/{task_id}`，
   携带管理员自己的 Bearer token。转发端点由主服务提供；它不在的时候
   干预会失败并说人话，而不是静默无效。
3. **`detail` 恒为中文字符串**，与用户侧同一约定（spec §13）。校验消息的**生成
   规则**抄 `server/app.py:validation_message`，本地多补几条参数名
   （`role`/`ok`/`command`…）；而错误文案的映射表不再抄——那是 server/app.py 里的
   单一来源（`svc.HTTP_DETAIL` + `svc.user_detail`），本面板只负责用自己的
   `PanelError` 保住具体原因。
4. **响应头 `Content-Security-Policy: default-src 'self'`。** 令牌存在
   localStorage 里，一次 XSS 就能把它拿走；面板又是本机页面，正好是内网里
   最容易被顺手打开的一个，所以头必须发。

界面口径：只说「自动化计算平台」「计算任务」「计算进程」，不出现 AI/助手/大模型。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import iterate_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

# 面板在 admin\ 下，业务模块在 server\。与 tests/conftest.py 同一套做法：
# 直接跑 `python admin/app_admin.py` 与被 pytest 收集，两种情况下 import 都能成。
SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import artifacts as prod          # noqa: E402  产物发现/打包/确认（server/artifacts.py）
import auth                       # noqa: E402
import db                         # noqa: E402
import store                      # noqa: E402

VERSION = "0.1.0"

# **只绑回环地址，写死在代码里不给开关。** 面板能中止别人的任务、能导出审计
# 日志、能建用户——任何一个把它绑到 0.0.0.0 的改动都是安全事故，所以连
# "环境变量覆盖端口"都不提供（端口可覆盖，host 不可）。
ADMIN_HOST = "127.0.0.1"
ADMIN_PORT = 8474

# 内部通道（主服务的第二个端口）。agent 的 RPC 与事件流都在那边。
DEFAULT_INTERNAL_BASE = "http://127.0.0.1:8475"
_INTERNAL_ENV = "PROTFORGE_WEB_INTERNAL"

UI_DIR = Path(__file__).resolve().parent.parent / "admin-ui"

# 转发超时的取舍：内部端口不可达时要在几秒内说人话，而不是让面板的按钮
# 一直转圈。30 秒与 dispatcher.RPC_TIMEOUT 同值，是「等满一次 agent 响应」。
FORWARD_TIMEOUT = 30.0
STREAM_TIMEOUT = 60.0

# ---------------------------------------------------------------- 错误消息

# 与 server/app.py:validation_message 同一套规则。本面板多出的参数名在这里补，
# 不去改那个文件（它属于主服务的 worker）。
_PARAM_LABEL = {
    "username": "用户名", "password": "密码", "title": "标题",
    "brief": "需求说明", "text": "步骤文本", "kind": "事件类型",
    "message": "内容", "summary": "结论", "task_id": "任务编号",
    "status": "状态", "role": "角色", "ok": "确认结论",
    "command": "干预命令", "model": "模型名", "thinking_level": "思考档位",
    "limit": "条数", "after_id": "游标", "artifact_id": "产物编号",
    "enabled": "停用状态",
}

# 错误文案的映射表与 `user_detail()` **只有一份**，在 server/app.py（主服务）里，
# 这里 import 过来用。不要再抄一份：抄过一次就已经漂了——app 侧有 422/503、
# 这边没有，两边的注释还互相矛盾，于是同一个框架层错误在两个面板上显示成两句话。
import app as svc               # noqa: E402  server/app.py：错误文案的单一来源


class PanelError(HTTPException):
    """本面板自己抛的错，detail 恒为中文。

    单开一个子类而不是直接抛 HTTPException：Starlette 的异常处理器按异常的
    **MRO** 找最近的处理器，直接抛 HTTPException 会落进 StarletteHTTPException
    那条兜底，被 svc.HTTP_DETAIL 覆盖掉具体原因。`svc.user_detail(exc, PanelError)`
    里那个 own 参数就是干这个的——server/app.py 侧的对应类是 PreciseHTTPException，
    两边各保各的具体原因。

    **凡是有具体中文原因的错都用这个类抛**，不管状态码在不在映射表里。
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


def validation_message(exc: RequestValidationError) -> str:
    """把 pydantic 的错误数组翻成一句人话。detail 全局只有字符串一种形态。"""
    msgs: list[str] = []
    for err in exc.errors():
        loc = tuple(err.get("loc") or ())
        kind = err.get("type") or ""
        name = _PARAM_LABEL.get(loc[1], loc[1]) if len(loc) > 1 else ""
        if loc[:1] == ("body",):
            if len(loc) == 1:
                msg = "缺少请求体"
            elif kind == "missing":
                msg = f"缺少{name}"
            elif kind in ("json_invalid", "model_attributes_type"):
                msg = "请求体不是合法的 JSON"
            else:
                msg = f"「{name}」不合法"
        elif loc[:1] == ("path",):
            msg = f"{name or '路径参数'}必须是整数"
        elif loc[:1] == ("query",):
            msg = f"{name or '查询参数'}不合法"
        else:
            msg = "请求参数不合法"
        if msg not in msgs:
            msgs.append(msg)
    return "；".join(msgs) or "请求参数不合法"


# ---------------------------------------------------------------- 视图

def _task_view(task: dict) -> dict:
    out = dict(task)
    out["status_label"] = db.STATUS_LABEL.get(task["status"], task["status"])
    return out


def _pending_artifacts(task_id: int) -> list[dict]:
    """这个任务上还没确认的时间窗产物（spec §8.2 的确认区数据源）。

    只取 ``source='timewindow'`` 且 ``verified=0`` 的。前缀产物登记即已确认，
    不该出现在这里——否则确认区里全是点不动的噪音，管理员会懒得看。
    """
    rows = store.artifacts.list(int(task_id))
    return [r for r in rows
            if r.get("source") == "timewindow" and int(r.get("verified") or 0) == 0]


# ---------------------------------------------------------------- 内部转发

def internal_base() -> str:
    """内部端口的基址。每次现读环境变量（同 db.data_root 的理由）。"""
    return str(os.environ.get(_INTERNAL_ENV) or DEFAULT_INTERNAL_BASE).rstrip("/")


def _forward_rpc(task_id: int, payload: dict, token: str) -> dict:
    """把一条干预命令转发给内部端口的 agent 通道（spec §8.1）。

    错误一律翻成中文：内部端口没起是**运维问题**，却会由面板上的一个按钮触发，
    所以必须说清楚是「内部通道连不上」而不是笼统的「失败」。
    """
    url = f"{internal_base()}/api/internal/admin/rpc/{int(task_id)}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        detail = _detail_of(raw) or f"内部通道返回 {e.code}"
        raise PanelError(_public_status(e.code), detail) from None
    except urllib.error.URLError:
        raise PanelError(502, f"连不上内部通道 {internal_base()}，"
                                 f"干预命令没有发出去") from None
    except OSError as e:
        raise PanelError(502, f"转发干预命令失败：{e}") from None
    try:
        got = json.loads(raw) if raw.strip() else {}
    except ValueError:
        got = {"ok": True}
    return got if isinstance(got, dict) else {"ok": True, "result": got}


def _detail_of(raw: str) -> str:
    """从内部端口的响应体里取中文 detail。取不到就是空串。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    detail = data.get("detail") if isinstance(data, dict) else None
    return detail if isinstance(detail, str) and detail else ""


def _public_status(code: int) -> int:
    """内部端口的状态码别原样透出去：它的 404/500 说的是它那边的事，
    面板上应当表现为「这条命令没发出去」。"""
    return code if code in (401, 403, 422) else 502


def _iter_body(resp):
    try:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                return
            yield chunk
    finally:
        resp.close()


# ---------------------------------------------------------------- 账号操作

def _list_users() -> list[dict]:
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT id, username, role, disabled, created_at FROM users ORDER BY id ASC"
        ).fetchall()
    finally:
        c.close()
    return [{"id": int(r["id"]), "username": r["username"], "role": r["role"],
             "disabled": bool(r["disabled"]), "created_at": r["created_at"]}
            for r in rows]


def _enabled_admin_count(exclude_id: int | None = None) -> int:
    return sum(1 for u in _list_users()
               if u["role"] == "admin" and not u["disabled"] and u["id"] != exclude_id)


def _set_role(user_id: int, role: str) -> None:
    if role not in auth.ROLES:
        raise store.Invalid(f"未知角色：{role}")
    c = db.connect()
    try:
        with c:
            cur = c.execute("UPDATE users SET role = ? WHERE id = ?",
                            (role, int(user_id)))
            if cur.rowcount == 0:
                raise store.NotFound("用户不存在")
    finally:
        c.close()


def _reset_password(user_id: int, password: str, username: str) -> None:
    """重置口令。校验规则与 auth.create_user 同一套（长度、不得与用户名相同），
    哈希仍走 auth.hash_password——口令算法只有一处定义。"""
    if len(password or "") < auth.MIN_PASSWORD:
        raise store.Invalid(f"密码至少 {auth.MIN_PASSWORD} 位")
    if len(password or "") > auth.MAX_PASSWORD:
        raise store.Invalid(f"密码最长 {auth.MAX_PASSWORD} 位")
    if (password or "").lower() == (username or "").lower():
        raise store.Invalid("密码不能与用户名相同")
    h, s = auth.hash_password(password)
    c = db.connect()
    try:
        with c:
            cur = c.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                            (h, s, int(user_id)))
            if cur.rowcount == 0:
                raise store.NotFound("用户不存在")
    finally:
        c.close()
    # 改完口令把这个账号的旧会话全清掉：否则已经拿到旧口令的人还能用旧令牌。
    c = db.connect()
    try:
        with c:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (int(user_id),))
    finally:
        c.close()


# ---------------------------------------------------------------- 请求体

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class StatusIn(BaseModel):
    status: str = Field(min_length=1, max_length=32)
    note: str = Field(default="", max_length=200)


class VerifyIn(BaseModel):
    task_id: int = Field(ge=1)
    ok: bool = True


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)
    role: Literal["user", "admin"] = "user"


class UserPatchIn(BaseModel):
    """改角色 / 停用 / 重置密码。**None 一律当「这个字段不动」**，
    所以模型里全可空：要重置口令传 password，要停用传 disabled。"""
    role: Literal["user", "admin"] | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, max_length=200)


class RpcIn(BaseModel):
    command: str = Field(min_length=1, max_length=32)
    message: str = Field(default="", max_length=4000)
    model: str = Field(default="", max_length=200)
    thinking_level: str = Field(default="", max_length=64)


# 面板只认这四条命令。它们是 `admin/rpc/{task_id}` 的白名单，不是给 agent 的：
# 面板是值班台，不该成为往计算进程里塞任意命令的口子。
RPC_COMMANDS = ("steer", "follow_up", "abort", "set_model", "set_thinking_level")


# ---------------------------------------------------------------- 鉴权

def require_admin_token(request: Request) -> tuple[dict, str]:
    """必须是管理员，并**把原始令牌一起交出去**。

    转发到内部端口时要用的就是它：内部端点校验的是管理员 Bearer，
    不是 agent_token（agent_token 在主服务进程内，这里看不到也用不上）。
    """
    token = auth.bearer_token(request)
    user = auth.resolve_token(token)
    if user is None:
        raise auth.Unauthorized("登录已失效，请重新登录")
    if user.get("role") != "admin":
        raise auth.Forbidden("这个面板只对管理员开放")
    return user, token


def require_admin(user: tuple[dict, str] = Depends(require_admin_token)) -> dict:
    return user[0]


# ---------------------------------------------------------------- 应用

def create_admin_app() -> FastAPI:
    db.init_db()
    app = FastAPI(title="蛋白计算平台（管理员面板）", version=VERSION)

    @app.exception_handler(store.NotFound)
    async def _not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(store.Invalid)
    async def _invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(auth.Invalid)
    async def _auth_invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def _framework_error(request, exc):
        return JSONResponse(status_code=422,
                            content={"detail": validation_message(exc)})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request, exc):
        # PanelError 自己带的中文 detail 原样透出；框架层兜底的才查 svc.HTTP_DETAIL。
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": svc.user_detail(exc, PanelError)})

    # --- 静态三件套（零构建零外链，所以自己发就行，不必引 StaticFiles） -------

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION, "internal_base": internal_base()}

    # --- 登录 ------------------------------------------------------------------

    @app.post("/api/admin/login")
    def login(body: LoginIn):
        """面板自己的登录入口。

        令牌是同一个 `sessions` 表里的那种——**和用户侧同一个令牌类型**，
        但面板比用户面板多一道：非管理员一律 403，本面板一条用户路由都不该
        让普通账号打开。
        """
        user = auth.authenticate(body.username, body.password)
        if user is None:
            left = auth.locked_out(body.username)
            if left:
                raise PanelError(401, f"登录失败次数过多，请 {int(left // 60) + 1} 分钟后再试")
            # 用户不存在与口令不对同一条话：分开说等于可以拿它枚举用户名
            raise PanelError(401, "用户名或密码不对")
        if user.get("role") != "admin":
            raise PanelError(403, "这个面板只对管理员开放")
        token = auth.issue_token(user["id"])
        return {"token": token, "expires_at": auth.token_expires_at(token),
                "user": user}

    @app.get("/api/admin/me")
    def me(user: dict = Depends(require_admin)):
        return user

    # --- 计算任务 --------------------------------------------------------------

    @app.get("/api/admin/tasks")
    def list_tasks(user: dict = Depends(require_admin)):
        """全部任务 + 队列 + 待确认产物。

        `pending_artifacts` 随任务一起给：确认区要按任务分组列时间窗产物，
        单独再开一个接口就得在前端做一遍 join，而这里 join 本来就在服务端。
        """
        rows = store.list_tasks(None, None, is_admin=True, limit=500)
        tasks = []
        for t in rows:
            view = _task_view(t)
            view["pending_artifacts"] = _pending_artifacts(t["id"])
            tasks.append(view)
        counts = {k: 0 for k in db.VALID_STATUSES}
        for t in rows:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return {
            "tasks": tasks,
            "queue": {"queued": counts.get("queued", 0),
                      "running": counts.get("running", 0),
                      "packaging": counts.get("packaging", 0)},
            "counts": counts,
            "pending_total": sum(len(t["pending_artifacts"]) for t in tasks),
        }

    @app.get("/api/admin/tasks/{task_id}")
    def get_task(task_id: int, user: dict = Depends(require_admin)):
        t = _task_view(store.get_task(task_id, is_admin=True))
        t["pending_artifacts"] = _pending_artifacts(task_id)
        return {"task": t, "events": store.list_events(task_id),
                "artifacts": store.artifacts.list(task_id)}

    @app.post("/api/admin/tasks/{task_id}/abort")
    def abort(task_id: int, who: tuple[dict, str] = Depends(require_admin_token)):
        """中止。**先杀进程树，再落状态**——反过来会被调度器的收尾覆盖回去：
        调度器看到子进程退出会写 packaging/failed，那是它的判断，不是管理员的。

        这一条用 require_admin_token 而不是 require_admin：转发 abort 命令到
        内部端口要的是**原始 Bearer**，用户视图里没有它。
        """
        user, token = who
        task = store.get_task(task_id, is_admin=True)
        if task["status"] in db.TERMINAL_STATUSES:
            raise PanelError(
                422, f"任务已经{db.STATUS_LABEL.get(task['status'])}了，没有在跑的进程可停")

        stopped, warning = False, ""
        if task["status"] in ("running", "packaging"):
            try:
                _forward_rpc(task_id, {"command": "abort",
                                       "reason": "管理员中止"}, token)
                stopped = True
            except HTTPException as e:
                # 内部通道不通不等于中止失败：状态照样落成失败，进程树留给运维。
                warning = str(e.detail)
        else:                                    # queued：还没起进程，取消排队即可
            stopped = True

        updated = store.set_status(task_id, "failed", actor="admin",
                                   message="管理员中止了任务")
        return {"task": updated, "process_stopped": stopped, "warning": warning}

    @app.post("/api/admin/tasks/{task_id}/status")
    def force_status(task_id: int, body: StatusIn, user: dict = Depends(require_admin)):
        """强制改状态。绕过所有自动推进，所以**这是最危险的一个动作**。"""
        target = (body.status or "").strip()
        if target not in db.VALID_STATUSES:
            raise store.Invalid(f"未知状态：{target}")
        store.get_task(task_id, is_admin=True)
        if target not in db.TERMINAL_STATUSES:
            # 离开终态要清 done_at，否则「已完成于」的时间会一直挂在非终态任务上。
            store.update_task(task_id, done_at=None)
        updated = store.set_status(task_id, target, actor="admin",
                                   message=body.note or "管理员改写了状态")
        return _task_view(updated)

    @app.post("/api/admin/tasks/{task_id}/retry")
    def retry(task_id: int, user: dict = Depends(require_admin)):
        """把结束掉的任务放回队列。

        产物**不删**：产物表按 (task_id, path) 去重，重跑时的 discover 会把
        同一个目录更新回来。留着的好处是这一轮的产物还在页面上可查，
        代价是两次运行的产物混在一起——所以重跑要两步确认，且确认表里写明。"""
        task = store.get_task(task_id, is_admin=True)
        if task["status"] not in db.TERMINAL_STATUSES:
            raise PanelError(422, "任务还在进行中，不需要重跑")
        store.update_task(task_id, done_at=None, step="", error="",
                          package_path="", summary="")
        # 收口标记也必须清：它的语义是「**本轮**已收口」。不清的话第二轮 agent 调
        # finish 会被 422 拒掉，而调度器 _settle 读到的是上一轮的标记，会把第二轮
        # 任何退出（包括直接崩溃）判成"正常收口"，最后打出一份与本次运行无关的旧包。
        store.clear_finished(task_id)
        updated = store.set_status(task_id, "queued", actor="admin",
                                   message="管理员把任务放回队列")
        return _task_view(updated)

    @app.get("/api/admin/tasks/{task_id}/audit")
    def audit_of_task(task_id: int,
                      after_id: int = Query(default=0, ge=0),
                      limit: int = Query(default=200, ge=1, le=1000),
                      user: dict = Depends(require_admin)):
        store.get_task(task_id, is_admin=True)
        return {"audit": store.audit.list(task_id, after_id=after_id, limit=limit)}

    @app.get("/api/admin/audit")
    def audit(after_id: int = Query(default=0, ge=0),
              limit: int = Query(default=200, ge=1, le=1000),
              user: dict = Depends(require_admin)):
        """不限任务的审计流。确认区与审计表都要「按任务过滤」，
        过滤到空就是全局视图，所以这条是 /tasks/{id}/audit 的超集。"""
        return {"audit": store.audit.list(None, after_id=after_id, limit=limit)}

    # --- 时间窗产物确认（spec §8.2） ------------------------------------------

    @app.post("/api/admin/artifacts/{artifact_id}/verify")
    def verify_artifact(artifact_id: int, body: VerifyIn,
                        user: dict = Depends(require_admin)):
        """确认归属 / 排除一个时间窗产物。

        这一步不做，产物兜底机制等于失效：时间窗命中的产物 `verified=0`，
        打包只收 `verified=1`，于是用户看到的是一批永远点不下来的产物，
        而计算进程那部分的算力已经烧掉了。
        """
        task = store.get_task(body.task_id, is_admin=True)
        row = prod.verify(body.task_id, artifact_id, ok=bool(body.ok))
        store.add_event(
            body.task_id, "note", "admin",
            f"管理员{'确认了' if body.ok else '排除了'}时间窗产物「{row['name']}」")
        return {"artifact": row, "task": task, "pending": _pending_artifacts(body.task_id)}

    # --- 用户 ------------------------------------------------------------------

    @app.get("/api/admin/users")
    def users(user: dict = Depends(require_admin)):
        return {"users": _list_users()}

    @app.post("/api/admin/users", status_code=201)
    def create_user(body: UserIn, user: dict = Depends(require_admin)):
        return auth.create_user(body.username, body.password, body.role)

    @app.patch("/api/admin/users/{user_id}")
    def patch_user(user_id: int, body: UserPatchIn, user: dict = Depends(require_admin)):
        target = auth.get_user(int(user_id))
        if target is None:
            raise store.NotFound("用户不存在")
        # 不能改自己：管理员把自己降级或停用之后，这个面板可能再没人能打开，
        # 而「找回」这条路按设计根本不存在（用户由管理员创建，无自助注册）。
        if int(target["id"]) == int(user["id"]) and (body.role is not None
                                                     or body.disabled is not None):
            raise PanelError(403, "不能改自己的角色或停用状态")

        if body.role is not None and body.role != target["role"]:
            if target["role"] == "admin" and _enabled_admin_count(int(user_id)) == 0:
                raise PanelError(422, "至少要保留一个可用的管理员账号")
            _set_role(user_id, body.role)
        if body.disabled is not None and bool(body.disabled) != _is_disabled(user_id):
            if bool(body.disabled) and _enabled_admin_count(int(user_id)) == 0:
                raise PanelError(422, "至少要保留一个可用的管理员账号")
            auth.set_disabled(int(user_id), bool(body.disabled))
        if body.password:
            _reset_password(user_id, body.password, target["username"])

        fresh = next((u for u in _list_users() if u["id"] == int(user_id)), None)
        return fresh or target

    # --- 干预通道（spec §8.1） -------------------------------------------------

    @app.post("/api/admin/rpc/{task_id}")
    def rpc(task_id: int, body: RpcIn, who: tuple[dict, str] = Depends(require_admin_token)):
        command = (body.command or "").strip()
        if command not in RPC_COMMANDS:
            raise store.Invalid(f"不支持的干预命令：{command}")
        store.get_task(task_id, is_admin=True)
        payload: dict = {"command": command}
        if command in ("steer", "follow_up"):
            text = (body.message or "").strip()
            if not text:
                raise store.Invalid("干预内容不能为空")
            payload["message"] = text
        elif command == "set_model":
            if not (body.model or "").strip():
                raise store.Invalid("模型名不能为空")
            payload["model"] = body.model.strip()
        elif command == "set_thinking_level":
            if not (body.thinking_level or "").strip():
                raise store.Invalid("思考档位不能为空")
            payload["thinking_level"] = body.thinking_level.strip()
        got = _forward_rpc(task_id, payload, who[1])
        store.audit.log(task_id, "admin_rpc",
                        command=f"{command} {body.message or body.model or body.thinking_level or ''}".strip(),
                        actor="admin")
        return {"ok": True, "command": command, "result": got}

    @app.get("/api/admin/stream/{task_id}")
    async def stream(task_id: int, request: Request,
                     who: tuple[dict, str] = Depends(require_admin_token)):
        """代理 agent 事件流。Bearer 走请求头，所以 EventSource 用不了，
        前端必须 fetch + ReadableStream 自己读。"""
        store.get_task(task_id, is_admin=True)
        url = f"{internal_base()}/api/internal/admin/stream/{int(task_id)}"
        raw_last = request.headers.get("Last-Event-ID") or \
            request.query_params.get("after") or "0"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {who[1]}")
        req.add_header("Last-Event-ID", str(raw_last))
        req.add_header("Accept", "text/event-stream")

        def _open():
            try:
                return urllib.request.urlopen(req, timeout=STREAM_TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise PanelError(e.code, "内部通道拒绝了这次订阅") from None
                raise PanelError(502, "内部通道没有响应") from None
            except (urllib.error.URLError, OSError):
                raise PanelError(502, f"连不上内部通道 {internal_base()}") from None

        resp = await iterate_in_threadpool(_open)
        return StreamingResponse(
            iterate_in_threadpool(_iter_body(resp)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},       # 反代默认缓冲，不关掉就不实时
        )

    # 静态页**最后**挂。Starlette 按注册顺序匹配，`/{path:path}` 要是挂在前面，
    # GET /api/admin/tasks 会被这条兜底吃掉（它自己抛 404，表现为「接口不存在」，
    # 而真正的接口是好的）——这类错不报错，只会让人以为后端没实现。
    _mount_ui(app)
    return app


def _is_disabled(user_id: int) -> bool:
    return any(u["id"] == int(user_id) and u["disabled"] for u in _list_users())


# ---------------------------------------------------------------- 静态页

# 键是**不带前导斜杠**的路径：`/{path:path}` 的 path 参数取的是斜杠之后的部分，
# 所以 "/" 传进来是空串、"/app.js" 传进来是 "app.js"。按带斜杠的键去查会永远
# 命中不了——表现是整页 404，而文件明明在磁盘上。
_STATIC_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
}

# `default-src 'self'` 是这个面板唯一重要的一条头：令牌在 localStorage 里，
# 一次外部脚本注入就等于把管理员令牌交出去。base-uri/form-action 顺手钉死。
CSP = "default-src 'self'; base-uri 'none'; form-action 'self'"


def _mount_ui(app: FastAPI) -> None:
    """把 admin-ui 三件套挂到根路径。

    手工读文件而不是挂 StaticFiles：面板只有三个文件，且**必须**在同一份
    代码里显式声明响应头（CSP、nosniff），挂静态中间件会把头交出去。
    """

    @app.get("/{path:path}", include_in_schema=False)
    def asset(path: str):
        if path not in _STATIC_FILES:
            # 未知路径不给回 200 的 index.html：那会把前端路由错误伪装成正常页面。
            raise HTTPException(404)
        name, media = _STATIC_FILES[path]
        real = (UI_DIR / name).resolve()
        try:
            body = real.read_bytes()
        except OSError:
            raise PanelError(404, "面板文件缺失") from None
        headers = {"Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer"}
        if media.startswith("text/html"):
            return HTMLResponse(body.decode("utf-8"), media_type="text/html",
                                headers={**headers, "Content-Security-Policy": CSP})
        return Response(body, media_type=media,
                        headers={**headers, "Content-Security-Policy": CSP})


def main(argv: list[str] | None = None) -> int:
    """`python admin/app_admin.py`。host 写死回环，不给开关。"""
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(description="蛋白计算平台管理员面板")
    p.add_argument("--port", type=int, default=ADMIN_PORT)
    args = p.parse_args(argv)
    uvicorn.run(create_admin_app(), host=ADMIN_HOST, port=args.port, log_level="info")
    return 0


__all__ = ["ADMIN_HOST", "ADMIN_PORT", "RPC_COMMANDS", "DEFAULT_INTERNAL_BASE",
           "create_admin_app", "internal_base", "validation_message", "main",
           "FORWARD_TIMEOUT", "CSP"]


if __name__ == "__main__":
    raise SystemExit(main())
