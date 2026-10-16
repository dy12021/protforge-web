"""FastAPI 主服务。**两个 app 实例，两个端口**，不是一台机器两个 app。

    create_public_app()    -> 0.0.0.0:8473    用户 API（浏览器够得着）
    create_internal_app()  -> 127.0.0.1:8475  内部 API（本机反代不该碰）

为什么不能合成一个 app + 检查来源 IP：用户的反代跑在本机，公网请求经反代
转发之后 `request.client.host` **同样是 127.0.0.1**——有反代时"检查来源 IP"
等于不检查。所以隔离只能靠物理层：内部端口只绑回环地址，且反代不转发它。
路由不共存是第二道闸（public app 上根本没有 /api/internal/*），测试钉死这一条。

**没有 /advance 端点**：状态推进全由系统触发（调度器接走 / agent 收口 /
打包成功 / 中止超时）。用户能推 running → packaging 就绕过了完成判定。

`detail` 恒为中文字符串，含 pydantic 框架层错误——数组透出去前端拿到
"[object Object]"。做法抄 `D:\\coding\\creative-panel\\server\\app.py:74`。

**凡是本文件写完事件的地方，后面都跟一句 `sse.notify()`。** 那是 SSE 内存总线
的叫醒钩子，写漏了这条事件只能等 3 秒轮询才到用户眼前（不会丢，只是迟）。
store.py 属于别的 worker，数据层不该知道有没有人在听，所以叫醒钩子放在 API 层。
"""
from __future__ import annotations

import secrets
import time
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

import auth
import db
import guards
import skills_index
import sse
import store

VERSION = "0.1.0"

PUBLIC_PORT = 8473
INTERNAL_PORT = 8475

# 参数名 -> 中文标签。pydantic 的 loc 里全是英文 key，不翻的话错误提示是
# "title 不合法"这种夹生文案。
_PARAM_LABEL = {
    "username": "用户名", "password": "密码", "title": "标题",
    "brief": "需求描述", "text": "步骤文本", "kind": "事件类型",
    "message": "内容", "summary": "结论", "error": "错误信息",
    "task_id": "任务编号", "status": "状态",
}

# 框架层自己抛的 HTTPException（404 路由不存在、405 方法不对……）默认 detail 是
# 英文短语。**不要**给 401/403 配条目：那些是我们自己带中文 detail 抛的，
# 映射表会把具体原因覆盖成一句通用话。
_HTTP_DETAIL = {
    400: "请求不对", 404: "接口不存在", 405: "请求方法不对",
    409: "请求冲突", 413: "内容太大", 422: "请求参数不合法",
    500: "服务器内部错误", 503: "服务暂时不可用",
}


def validation_message(exc: RequestValidationError) -> str:
    """把 pydantic 的错误数组翻成一句人话。

    下游都假定 detail 是字符串：前端 `throw new Error(d.detail)`、CLI 直接
    打印。数组透出去的话前端拿到 "[object Object]"，而两种形态混在同一个
    字段里，下游没法写匹配的逻辑。

    文案按 loc 定到具体参数；认不出来的退回一句通用话，**不把 pydantic 原文**
    （Input should be ...）甩给人看。
    """
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


def _install_common(app: FastAPI) -> None:
    """两个 app 共用的异常处理器与 /api/health。detail 全局只有一种形态。"""

    @app.exception_handler(store.NotFound)
    async def _not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(store.Invalid)
    async def _invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(auth.Invalid)
    async def _auth_invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    # 框架层（pydantic）错误：少了它，同一个 {"detail": ...} 下会同时存在 str 和数组。
    @app.exception_handler(RequestValidationError)
    async def _framework_error(request, exc):
        return JSONResponse(status_code=422,
                            content={"detail": validation_message(exc)})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request, exc):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": _HTTP_DETAIL.get(
                                exc.status_code, str(exc.detail))})

    @app.get("/api/health")
    def health():
        # 技能数放进健康检查，是"这台机器还能不能算"的第一个信号。
        # 扫目录可能因为环境不全失败，所以失败时报 null 而不是让健康检查 500。
        try:
            skills = len(skills_index.scan())
        except Exception:                                   # noqa: BLE001
            skills = None
        return {"ok": True, "version": VERSION, "skills": skills}


def _task_view(task: dict) -> dict:
    """任务视图：扁平的 tasks 行 + 中文状态标签。"""
    out = dict(task)
    out["status_label"] = db.STATUS_LABEL.get(task["status"], task["status"])
    return out


def _detail(task: dict) -> dict:
    return {"task": _task_view(task),
            "events": store.list_events(task["id"]),
            "artifacts": store.artifacts.list(task["id"])}


def _delete_task_row(task_id: int) -> None:
    """删 tasks 一行，uploads / artifacts / events 靠 schema 的 ON DELETE CASCADE 清。

    业务 SQL 本该在 store.py，这里直接写是因为本 task 的文件归属只有
    auth.py / app.py / 两个测试文件，store.py 属于另一个 worker——不去抢。
    依赖 db.connect() 的 `foreign_keys=ON`（每连接生效，不是建库属性），
    级联真的生效由 test_delete_task_cascades_to_events_and_artifacts 钉死。

    **只删数据库**：work/<id>/、uploads/<id>/、packages/ 一律不动，磁盘清理由
    janitor.py 负责（7/14/30 天保留）。数据库里查不到的东西仍然可以回收，
    反过来则不然。
    """
    c = db.connect()
    try:
        with c:
            c.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
    finally:
        c.close()


# ---------------------------------------------------------------- 请求体

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=store.TITLE_MAX)
    brief: str = Field(default="", max_length=store.BRIEF_MAX)


class TaskPatch(BaseModel):
    """只允许改标题与需求正文。

    **没有 status**：用户侧没有状态推进权（也没有 /advance），多传的 status
    由 pydantic 默认忽略——既不是 400（前端不该为一个非法字段整次失败），
    更不是静默生效。

    `None`（不传 / 显式 null）一律当"这个字段不动"，所以模型里全是可空：
    要清空需求正文传 `""`，不传就是保持原样。
    """

    title: str | None = Field(default=None, max_length=store.TITLE_MAX)
    brief: str | None = Field(default=None, max_length=store.BRIEF_MAX)


class StepIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class EventIn(BaseModel):
    # kind 收窄到 agent 能写的那几种：created/status/finished 由系统写，
    # agent 冒充它们就能自己宣布任务完成。
    kind: Literal["log", "note", "error"] = "log"
    message: str = Field(default="", max_length=8000)


class FinishIn(BaseModel):
    summary: str = Field(default="", max_length=4000)
    error: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------- 内部令牌

AGENT_TOKEN_TTL = 24 * 3600

# {token: (task_id, 过期时间戳)}。**进程内**：agent_token 是"这次计算的进程
# 凭证"，不该比任务活得久，进程重启即失效反而是对的。代价是签发方必须与
# 内部 app 同一个进程——所以签发走 /api/internal/admin/tokens/{task_id}。
_AGENT_TOKENS: dict[str, tuple[int, float]] = {}


def issue_agent_token(task_id: int, ttl: float = AGENT_TOKEN_TTL) -> str:
    """给某个任务签一个 agent_token。**与 task_id 绑定**——这是它唯一的作用域保证。"""
    token = secrets.token_urlsafe(32)
    _AGENT_TOKENS[token] = (int(task_id), time.time() + float(ttl))
    return token


def revoke_agent_token(task_id: int | None = None, token: str | None = None) -> None:
    if token is not None:
        _AGENT_TOKENS.pop(token, None)
    if task_id is not None:
        for t in [k for k, v in _AGENT_TOKENS.items() if v[0] == int(task_id)]:
            _AGENT_TOKENS.pop(t, None)


def agent_token_task(token: str) -> int | None:
    """令牌 -> 绑定的 task_id。过期或查不到返回 None。"""
    entry = _AGENT_TOKENS.get((token or "").strip() or "")
    if entry is None or entry[1] < time.time():
        return None
    return entry[0]


def require_agent(task_id: int, request: Request) -> int:
    """FastAPI 依赖：校验 X-Agent-Token 与路径里的 task_id 是否同一个任务。

    对不上返 403 而不是 401：令牌本身是真的，只是用错了地方。
    """
    raw = (request.headers.get("X-Agent-Token") or "").strip()
    if not raw:
        raise HTTPException(401, "缺少 agent_token 请求头")
    bound = agent_token_task(raw)
    if bound is None:
        raise HTTPException(401, "agent_token 无效或已过期")
    if bound != int(task_id):
        raise HTTPException(403, f"这个 agent_token 只能用于任务 #{bound}")
    return int(task_id)


# ---------------------------------------------------------------- 用户 API

def create_public_app() -> FastAPI:
    db.init_db()
    app = FastAPI(title="蛋白计算平台", version=VERSION)
    _install_common(app)

    @app.post("/api/auth/login")
    def login(body: LoginIn):
        user = auth.authenticate(body.username, body.password)
        if user is None:
            left = auth.locked_out(body.username)
            if left:
                raise HTTPException(401, f"登录失败次数过多，请 {int(left // 60) + 1} 分钟后再试")
            # 用户不存在与口令不对同一条话：分开说等于可以拿它枚举用户名
            raise HTTPException(401, "用户名或密码不对")
        token = auth.issue_token(user["id"])
        return {"token": token, "expires_at": auth.token_expires_at(token),
                "user": user}

    @app.get("/api/me")
    def me(user=Depends(auth.current_user)):
        return user

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskIn, user=Depends(auth.current_user)):
        task = store.create_task(body.title, body.brief, owner_id=user["id"],
                                 actor="human")
        # store.create_task 刻意不写 created 事件（事件流由 API 层负责），
        # 所以这一条得在这儿补，否则时间线是从"已排队"凭空开始的。
        store.add_event(task["id"], "created", "human", f"提交了任务：{task['title']}")
        sse.notify()
        return _detail(task)

    @app.get("/api/tasks")
    def list_tasks(status: str | None = Query(default=None),
                   limit: int = Query(default=100, ge=1, le=500),
                   user=Depends(auth.current_user)):
        is_admin = user["role"] == "admin"
        rows = store.list_tasks(None if is_admin else user["id"], status,
                                is_admin=is_admin, limit=limit)
        return {"tasks": [_task_view(t) for t in rows]}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int, user=Depends(auth.current_user)):
        # 越权走 store.get_task → NotFound → 404，不返 403（不承认 id 存在）
        return _detail(store.get_task(task_id, owner_id=user["id"],
                                       is_admin=user["role"] == "admin"))

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: int, body: TaskPatch, user=Depends(auth.current_user)):
        task = store.get_task(task_id, owner_id=user["id"],
                              is_admin=user["role"] == "admin")
        fields, changed = {}, []
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise store.Invalid("标题不能为空")
            fields["title"] = title
        if body.brief is not None:
            if len(body.brief) > store.BRIEF_MAX:
                raise store.Invalid(f"需求描述过长，最多 {store.BRIEF_MAX} 个字符")
            fields["brief"] = body.brief
        if not fields:
            raise store.Invalid("没有要修改的字段")
        for k, v in fields.items():
            if task.get(k) != v:
                changed.append(k)
        if not changed:
            # 内容没变就不写事件：时间线是给人看的，不该被无意义的保存灌满
            raise store.Invalid("内容没有变化，不用保存")
        updated = store.update_task(task_id, **fields)
        # message 里放**原样的英文字段名**：这是 API 契约的一部分，翻译留给
        # 前端显示层（FIELD_CN 翻成「改了标题 / 需求」），服务端不写死中文结构名。
        store.add_event(task_id, "edited", "human", "修改了：" + "、".join(changed))
        sse.notify()
        return _detail(updated)

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: int, user=Depends(auth.current_user)):
        store.get_task(task_id, owner_id=user["id"],
                       is_admin=user["role"] == "admin")     # 越权 → 404
        _delete_task_row(task_id)
        # 任务行已经没了，这条"删除"事件无处可写，也不假装记了日志
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/abort")
    def abort(task_id: int, user=Depends(auth.current_user)):
        task = store.get_task(task_id, owner_id=user["id"],
                              is_admin=user["role"] == "admin")
        if task["status"] in db.TERMINAL_STATUSES:
            raise HTTPException(
                422, f"任务已经{db.STATUS_LABEL.get(task['status'])}了，不能再中止")
        updated = store.set_status(task_id, "failed", actor="human",
                                   message="用户中止了任务")
        sse.notify()
        return _detail(updated)

    @app.get("/api/tasks/{task_id}/stream")
    async def stream_task(task_id: int, request: Request,
                          user=Depends(auth.current_user)):
        """任务时间线的 SSE 实时流。

        **鉴权先做、流后开**：越权走 store.get_task → NotFound → 404（不返 403，
        403 等于承认这个 id 存在）。前端 openDrawer 会在详情页里拉一次，这里
        同样拦住，省得靠"连上了没数据"来暴露别人的任务。

        游标优先取 `Last-Event-ID` 头（前端重连时带），退化到 `last_event_id`
        查询参数（curl 手测方便）。两者都缺/是脏数据就是 0 = 从头补。

        处理器是 `async def`：响应体是一条长连接，必须挂在事件循环上；
        鉴权依赖 current_user 是同步函数，由 FastAPI 自己丢线程池，不影响。
        """
        store.get_task(task_id, owner_id=user["id"],
                       is_admin=user["role"] == "admin")
        raw = request.headers.get("Last-Event-ID")
        if raw is None:
            raw = request.query_params.get("last_event_id")
        try:
            last_seen = max(0, int(raw)) if raw not in (None, "") else 0
        except (TypeError, ValueError):
            # 游标是脏数据就从头补：宁可重复（前端按事件 id 去重）也不丢
            last_seen = 0
        return StreamingResponse(
            sse.stream(task_id, request, last_seen),
            media_type="text/event-stream",
            # X-Accel-Buffering: no —— 反代（nginx）默认会缓冲响应体，
            # 不显式关掉，实时流会被攒成一块再吐给浏览器，退化成轮询。
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    @app.get("/api/tasks/{task_id}/package")
    def download(task_id: int, user=Depends(auth.current_user)):
        task = store.get_task(task_id, owner_id=user["id"],
                              is_admin=user["role"] == "admin")
        if task["status"] not in db.TERMINAL_STATUSES:
            raise HTTPException(409, "结果包还在生成中，任务完成后才能下载")
        raw = (task.get("package_path") or "").strip()
        if not raw:
            raise HTTPException(409, "这个任务还没有可下载的结果包")
        try:
            # 越界（package_path 被写坏或指向别处）一律当"没有包"，
            # 不把文件系统布局透出去。
            real = guards.resolve(raw, task_id, write=False)
        except guards.Violation:
            raise HTTPException(409, "结果包不在允许的目录里，无法下载")
        if not real.is_file():
            raise HTTPException(409, "结果包文件不见了")
        return FileResponse(real, filename=real.name, media_type="application/zip")

    return app


# ---------------------------------------------------------------- 内部 API

def create_internal_app() -> FastAPI:
    db.init_db()
    app = FastAPI(title="蛋白计算平台（内部通道）", version=VERSION)
    _install_common(app)

    @app.post("/api/internal/agent/{task_id}/step")
    def agent_step(task_id: int, body: StepIn,
                   _tid: int = Depends(require_agent)):
        store.get_task(task_id)
        step = store.set_step(task_id, body.text)      # 返回值已是截断后的
        store.add_event(task_id, "step", "agent", body.text)
        sse.notify()
        return {"step": step}

    @app.post("/api/internal/agent/{task_id}/event")
    def agent_event(task_id: int, body: EventIn,
                    _tid: int = Depends(require_agent)):
        store.get_task(task_id)
        eid = store.add_event(task_id, body.kind, "agent", body.message)
        sse.notify()
        return {"event_id": eid}

    @app.post("/api/internal/agent/{task_id}/finish")
    def agent_finish(task_id: int, body: FinishIn,
                     _tid: int = Depends(require_agent)):
        task = store.get_task(task_id)
        if task["status"] in db.TERMINAL_STATUSES:
            raise HTTPException(422, "任务已经结束，不能重复收口")
        # 给了 error 就判失败：agent 自己报的失败要能落进时间线，
        # 但只有 system/调度器才允许把任务推上 done。
        fields = {"status": "failed", "error": body.error} if body.error \
            else {"status": "done"}
        updated = store.set_status(task_id, fields.pop("status"), actor="agent",
                                   message=body.summary or body.error)
        if body.summary:
            fields["summary"] = body.summary
        if fields:
            updated = store.update_task(task_id, **fields)
        # finished 事件放最后：调度器拿 has_finished() 区分"重连"与"真跑完"，
        # 它出现即代表本次计算已彻底收口。
        store.record_finished(task_id, actor="agent")
        sse.notify()
        return _detail(updated)

    # 内部管理通道的第二套令牌：管理员 Bearer。它只负责发 agent_token，
    # 自己不碰任务状态——状态推进全由系统触发。
    @app.post("/api/internal/admin/tokens/{task_id}")
    def admin_issue_token(task_id: int, user=Depends(auth.require_admin)):
        store.get_task(task_id)
        return {"task_id": task_id, "token": issue_agent_token(task_id)}

    return app


__all__ = [
    "VERSION", "PUBLIC_PORT", "INTERNAL_PORT", "create_public_app",
    "create_internal_app", "validation_message", "require_agent",
    "issue_agent_token", "revoke_agent_token", "agent_token_task", "main",
]


def main(argv: list[str] | None = None) -> int:
    """`python server/app.py public|internal` —— **两个进程，各起一个 uvicorn**。

    故意不做"一个进程两个 app"的写法：内部 app 必须只绑回环地址，
    绑在同一进程里靠参数切换，早晚有人用错参数把内部通道开到 0.0.0.0。
    两条命令分开跑，端口写死在代码里，反代配置照着抄即可：

        python server/app.py public      # 0.0.0.0:8473  反代指向它
        python server/app.py internal    # 127.0.0.1:8475 反代**不要**转发它
    """
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(description="蛋白计算平台主服务")
    p.add_argument("which", choices=("public", "internal"))
    p.add_argument("--port", type=int, default=None)
    args = p.parse_args(argv)

    if args.which == "public":
        app, host, port = create_public_app(), "0.0.0.0", args.port or PUBLIC_PORT
    else:
        app, host, port = create_internal_app(), "127.0.0.1", args.port or INTERNAL_PORT
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
