"""FastAPI 主服务。**两个 app 实例，两个端口**，不是一台机器两个 app。

    create_public_app()    -> 0.0.0.0:8473    用户 API + 用户面板前端
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

import contextlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

import artifacts
import auth
import db
import dispatcher
import guards
import janitor
import skills_index
import sse
import store

VERSION = "0.1.0"

log = logging.getLogger("protforge.web")

# 用户面板前端。挂在仓库里而不是数据盘：它是程序的一部分，不该跟着数据一起被清。
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
USER_UI_FILES = frozenset({"index.html", "app.js", "style.css"})

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
        detail = (str(exc.detail) if isinstance(exc, PreciseHTTPException)
                  else _HTTP_DETAIL.get(exc.status_code, str(exc.detail)))
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

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


class PreciseHTTPException(HTTPException):
    """detail 原样返回，不被 _HTTP_DETAIL 的通用话覆盖。

    只给**我们自己写的**具体原因用。框架层抛的（路由不存在、方法不对）仍旧走
    _HTTP_DETAIL 映射。绝不要给这个类塞 401/403/400/503 的默认条目来"省事"——
    那正是它存在的理由要避免的事。
    """


class RpcCmdIn(BaseModel):
    """管理员面板 -> 内部通道。与 admin/app_admin.py 的 RpcIn 同形，两边独立定义。"""

    command: str = Field(min_length=1, max_length=32)
    message: str = Field(default="", max_length=4000)
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=64)
    thinking_level: str = Field(default="", max_length=64)


class FinishIn(BaseModel):
    summary: str = Field(default="", max_length=4000)
    error: str = Field(default="", max_length=4000)
    # agent 自报的产物清单。**注意它和产物发现不是一回事**：这里说的是工作目录下的
    # 相对路径，而 artifacts.discover 扫的是 ProtForge 的 outputs 根，两棵不同的树。
    # 所以这份清单只记进时间线供排查，不参与打包——真按它去 assemble 会打空包。
    # 但字段必须收下：pydantic 会把未声明的键**静默丢掉**，agent 以为自己报了、
    # 后端却当没收到，这种不一致最难查。
    artifacts: list[str] = Field(default_factory=list, max_length=50)


# ---------------------------------------------------------------- 内部令牌

# **令牌表只有一张，在 dispatcher 里。** 签发方是调度器（它起进程时把令牌交给
# agent），校验方是下面这个文件里的内部端点，两边必须读同一张表：本文件原先自己
# 抄了一份，两个进程各留各的，症状是 agent 每一步回调都 401 且极难看出原因。
#
# 顺带一个约束：令牌是进程内存，所以调度器必须与内部 app(8475) 同进程，
# 签发走 /api/internal/admin/tokens/{task_id}，别写进数据库——进程重启即失效
# 反而是对的，这把钥匙不该比它守的门活得久。
AGENT_TOKEN_TTL = dispatcher.AGENT_TOKEN_TTL

issue_agent_token = dispatcher.issue_agent_token
revoke_agent_token = dispatcher.revoke_agent_token
agent_token_task = dispatcher.agent_token_task


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
        # error 字段也补上：详情页读它展示失败原因，而调度器看到库里已经是 failed
        # 之后收尾不会再覆盖（_mark_failed 遇到 failed 直接返回），不写就空了。
        updated = store.update_task(task_id, error="用户中止了任务")
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
            # **不能是 guards.resolve()**：生产打包写在 data\packages\task_<id>.zip，
            # 而 resolve() 认的是 agent 的四个区域（work/uploads/outputs/inputs），
            # packages 不在里面——于是每个真实任务点下载都恒 409，包生成了、用户
            # 永远拿不到，测试全绿是因为夹具写的是 work 区里一个生产中不存在的布局。
            # resolve_package() 只认本任务自己的那一个文件，顺带把 inputs/outputs/
            # work 里的任意文件也堵了（原来 write=False 是放行它们的）。
            real = guards.resolve_package(task_id, raw)
        except guards.Violation:
            # 越界（package_path 被写坏或指向别处）一律当"没有包"，
            # 不把文件系统布局透出去。
            raise HTTPException(409, "结果包不在允许的目录里，无法下载")
        if not real.is_file():
            raise HTTPException(409, "结果包文件不见了")
        return FileResponse(real, filename=real.name, media_type="application/zip")

    # 用户面板前端挂在**最后**，且只放行这三个文件。
    #
    # **不要**改成 app.mount("/", StaticFiles(...))：那会把整棵树接管，POST /api/...
    # 从 404 变成 405、detail 也不再是中文，而"用户侧没有 /advance"这条设计就被
    # 悄悄改掉了——症状是三条不相干的测试同时变红，没人会想到是挂载顺序。
    #
    # 前端用相对路径引用（href="style.css"），所以单段路径就够；/api/* 至少两段，
    # 撞不上。未知单段路径落到下面 raise 的 404，仍是统一的中文提示。
    if not (WEB_DIR / "index.html").is_file():
        log.warning("用户面板前端不在 %s，8473 首页会是 404", WEB_DIR)

    @app.get("/")
    def user_ui():
        if not (WEB_DIR / "index.html").is_file():
            raise HTTPException(404, "用户面板前端文件缺失")
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.get("/favicon.ico")
    def user_favicon():
        # 前端用的是 data: 空图标，但浏览器仍会来要一次。给它 204 而不是 404，
        # 省得在日志里堆无意义的报错。
        from fastapi import Response
        return Response(status_code=204)

    @app.get("/{asset}")
    def user_asset(asset: str):
        if asset not in USER_UI_FILES:
            raise HTTPException(404, "页面不存在")
        f = WEB_DIR / asset
        if not f.is_file():
            raise HTTPException(404, "页面不存在")
        # 静态资源不该被中途改掉：前端文件没有版本号，缓存一旧就白屏
        return FileResponse(f, headers={"Cache-Control": "no-cache"})

    return app


# ---------------------------------------------------------------- 后台工种
#
# 这三样都是"模块写好了但没人调"的那一类，症状一律是静默的：任务停在 packaging
# 没人打包、E 盘只增不减、没人能登录。所以它们在这里被真正接上，而不是留在文档里。

PACK_SCAN_INTERVAL = 2.0        # 秒。packaging 是给人看的状态，不该让人等太久
JANITOR_INTERVAL = 30 * 60      # 半小时。清理是体力活，不需要勤

# 正在打包的 task_id。打包在子进程里跑、要占磁盘，同一个任务不能被扫两遍。
_pack_lock = threading.Lock()
_packing: set[int] = set()


def _task_visible(task_id: int) -> dict | None:
    """读任务并叫醒 SSE；读不到返回 None。"""
    try:
        task = store.get_task(task_id, is_admin=True)
    except Exception:                                  # noqa: BLE001
        log.exception("读任务 %s 失败", task_id)
        return None
    sse.notify()
    return task


def _fail_task(task_id: int, reason: str) -> None:
    """把任务标成失败并说明原因。已经是终态的**不覆盖**。

    不覆盖的原因：打包失败和调度器收尾可能撞在一起（比如管理员同时点了中止），
    谁后到谁覆盖的话，用户点的是中止，最后看到的却是"打包失败"——
    和 dispatcher._mark_failed 里那道闸门是同一个道理。
    """
    try:
        task = store.get_task(task_id, is_admin=True)
    except Exception:                                  # noqa: BLE001
        log.exception("读任务 %s 失败，跳过标记", task_id)
        return
    if task.get("status") in db.TERMINAL_STATUSES:
        return
    try:
        store.update_task(task_id, error=reason)
        store.set_status(task_id, "failed", actor="system", message=reason)
        store.add_event(task_id, "error", "system", reason)
    except Exception:                                  # noqa: BLE001
        log.exception("把任务 %s 标记为失败时出错", task_id)
    sse.notify()


def _package_one(task_id: int) -> None:
    """打一个包：先登记产物，再压 zip，最后写 package_path 并收口。

    产物靠时间窗兜底发现时 verified=0（"先显示、再确认"），这种情况**不自动放行**：
    自动确认等于把"先显示、再确认"这道闸门拆了。它诚实地失败，由管理员确认后
    走 /api/internal/admin/repack 重新打包。
    """
    try:
        found = artifacts.discover(task_id)
    except Exception as e:                             # noqa: BLE001
        log.exception("登记产物失败：%s", task_id)
        _fail_task(task_id, f"登记产物时出错：{e}")
        return

    if not artifacts.downloadable(task_id):
        if found:
            _fail_task(task_id, "产物已找到但尚未确认打包："
                              "需要管理员在产物列表里确认后重新打包")
        else:
            _fail_task(task_id, "计算结束了，但没有找到任何产物")
        return

    out = artifacts.default_package_path(task_id)
    try:
        artifacts.package(task_id, out_zip=out)
    except artifacts.PackageError as e:                # QuotaExceeded 是它的子类
        log.warning("打包 %s 失败：%s", task_id, e)
        _fail_task(task_id, str(e))
        return
    except Exception:                                   # noqa: BLE001
        log.exception("打包 %s 时出错", task_id)
        _fail_task(task_id, "打包过程出错，详见服务端日志")
        return

    # **写 done 之前必须重读一次状态。** 20 GB 的 zip 可能压好几分钟，用户在这期间
    # 点中止（跑在 public app 那个进程里）是完全正常的。不重读就会把 failed 覆盖回
    # done——用户刚点完中止，任务却变成"已完成"，中止被静默撤销。
    # 失败路径已经有一道同款闸（_fail_task），成功路径漏了，两边不对称。
    try:
        current = store.get_task(task_id, is_admin=True)
    except Exception:                                   # noqa: BLE001
        log.exception("读任务 %s 失败，跳过收口", task_id)
        return
    if current.get("status") in db.TERMINAL_STATUSES:
        # 已经被中止/失败盖住了：结果包留着（管理员确认后可以走 repack 重打），
        # 但绝不改成 done。
        log.info("任务 %s 在打包期间已进入终态（%s），不再改写为 done",
                 task_id, current.get("status"))
        sse.notify()
        return

    try:
        store.update_task(task_id, package_path=str(out))
        store.set_status(task_id, "done", actor="system",
                         message="结果包已就绪，可以下载")
    except Exception:                                   # noqa: BLE001
        log.exception("收口任务 %s 失败", task_id)
        return
    sse.notify()


def _pack_scan_once() -> list[int]:
    """把停在 packaging 的任务打包掉。返回处理过的 task_id。"""
    handled: list[int] = []
    try:
        rows = store.list_tasks(status="packaging", is_admin=True, limit=50)
    except Exception:                                   # noqa: BLE001
        log.exception("读取待打包任务失败")
        return handled
    for row in rows:
        task_id = int(row["id"])
        if (row.get("package_path") or "").strip():
            continue                                   # 已经打过包了，别再来一遍
        with _pack_lock:
            if task_id in _packing:
                continue
            _packing.add(task_id)
        try:
            _package_one(task_id)
            handled.append(task_id)
        finally:
            with _pack_lock:
                _packing.discard(task_id)
    return handled


def _pack_loop(stop: threading.Event) -> None:
    while not stop.wait(PACK_SCAN_INTERVAL):
        try:
            _pack_scan_once()
        except Exception:                               # noqa: BLE001 - 线程不能死
            log.exception("打包扫描出错")


def _janitor_loop(stop: threading.Event) -> None:
    while not stop.wait(JANITOR_INTERVAL):
        try:
            janitor.sweep()
        except Exception:                               # noqa: BLE001
            log.exception("磁盘清理出错")


def _start_housekeeping(app: FastAPI) -> list[threading.Thread]:
    """起打包与清理两个后台线程，返回它们（停机时 join）。"""
    threads = []
    for name, target in (("packager", _pack_loop), ("janitor", _janitor_loop)):
        stop = threading.Event()
        app.state.housekeeping_stop = stop

        def run(stop=stop, target=target, name=name):
            target(stop)

        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        threads.append(t)
    return threads


ADMIN_USER_ENV = "PROTFORGE_ADMIN_USER"
ADMIN_PASSWORD_ENV = "PROTFORGE_ADMIN_PASSWORD"


def bootstrap_admin() -> dict | None:
    """按环境变量建初始管理员，返回建出来的用户。没配返回 None。

    **只在环境变量给了用户名+口令时才动手**，不自动生成随机口令往日志里打——
    那种做法迟早会有人把日志贴到工单里。没配也**不是错误**：可能管理员是别人
    建好的，所以由 :func:`_warn_if_no_admin` 去说这件事。
    """
    user = (os.environ.get(ADMIN_USER_ENV) or "").strip()
    pwd = os.environ.get(ADMIN_PASSWORD_ENV) or ""
    if not user or not pwd:
        return None
    if auth.get_user(user) is not None:
        return None                                    # 已经有了，**绝不覆盖**
    try:
        made = auth.create_user(user, pwd, role="admin")
    except Exception:                                  # noqa: BLE001
        log.exception("创建初始管理员 %s 失败", user)
        return None
    log.info("已按环境变量创建初始管理员：%s", user)
    return made


def _warn_if_no_admin() -> None:
    """一个管理员都没有时把话说清楚——否则表现是"登录页谁都登不进去"。"""
    if not _has_any_admin():
        log.warning(
            "库里还没有管理员，用户端登录与管理面板都会用不了。"
            "在 .env 里设 %s / %s 再重启即可创建。", ADMIN_USER_ENV, ADMIN_PASSWORD_ENV)


def _has_any_admin() -> bool:
    try:
        c = db.connect()
        try:
            row = c.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
            return row is not None
        finally:
            c.close()
    except Exception:                                  # noqa: BLE001
        log.exception("检查管理员是否存在时出错")
        return True                                    # 查不了就别拦着，交给登录时报错


# ---------------------------------------------------------------- 内部 API

# 后台工种总开关：调度器 + 打包器 + 清理器，三样一起开或一起关。
#
# **生产必须设为 1**，否则任务提交后永远停在"排队中"，不会有 agent 被拉起来。
# 默认关闭只有一个理由：测试用 TestClient 起内部 app 时，这三样都会对着临时库
# 真去干活（起 node 进程、压 zip），测试既慢又不确定。
WORKERS_ENV = "PROTFORGE_WEB_WORKERS"
_TRUTHY = ("1", "on", "true", "yes")


@contextlib.asynccontextmanager
async def _workers_lifespan(app: FastAPI):
    """内部 app 顺带把三个后台工种带起来——**只有内部 app 带**。

    - **调度器**：agent 的令牌在这里校验、管理员的 RPC 也转发到这里，它跟这两样
      同进程才共享得到那张内存令牌表。public app 里再起一个等于起两份，
      两份会互相抢任务。
    - **打包器 / 清理器**：跟着调度器一起开关。三样都是"没人调用就静默不干活"的
      东西，分成两个旋钮的话，运维只开一个会得到一个能登录、能提交、但永远
      排队的服务——比全关掉更难查。

    全部由 :data:`WORKERS_ENV` 显式打开。
    """
    bootstrap_admin()
    _warn_if_no_admin()

    if os.environ.get(WORKERS_ENV, "").strip().lower() not in _TRUTHY:
        yield
        return

    disp = dispatcher.Dispatcher()
    app.state.dispatcher = disp
    disp.start()
    threads = _start_housekeeping(app)
    try:
        yield
    finally:
        disp.stop()
        for t in threads:
            t.join(timeout=2.0)


def create_internal_app() -> FastAPI:
    db.init_db()
    app = FastAPI(title="蛋白计算平台（内部通道）", version=VERSION,
                  lifespan=_workers_lifespan)
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
        # 闸门看 finished 标记，不只看状态：状态已经不由 finish 决定了，
        # 只拿终态去判会把第二次 finish 放进来。
        if store.has_finished(task_id) or task["status"] in db.TERMINAL_STATUSES:
            raise HTTPException(422, "任务已经结束，不能重复收口")
        # 给了 error 就判失败：agent 自己报的失败要能落进时间线，
        # 但只有 system/调度器才允许把任务推上 done。
        if body.error:
            updated = store.set_status(task_id, "failed", actor="agent",
                                       message=body.error)
            updated = store.update_task(task_id, error=body.error)
        else:
            # **成功时不改状态。** 算完不等于交付完：还要 _settle 推 packaging、
            # 打包器打 zip，才到 done。原来这里直接置 done，调度器随后又把状态改回
            # packaging，界面上流转轨会**倒退一格**再前进——明明算完了却看着像返工。
            updated = task
        if body.summary:
            updated = store.update_task(task_id, summary=body.summary)
        # finished 事件放最后：调度器拿 has_finished() 区分"重连"与"真跑完"，
        # 它出现即代表本次计算已彻底收口。
        store.record_finished(task_id, actor="agent")
        if body.artifacts:
            names = "、".join(p.strip() for p in body.artifacts[:8] if p.strip())
            more = f"（共 {len(body.artifacts)} 项）" if len(body.artifacts) > 8 else ""
            store.add_event(task_id, "note", "agent",
                            f"计算进程自报产物：{names}{more}")
        sse.notify()
        return _detail(updated)

    # 干预命令白名单。面板是值班台，不该成为往计算进程里塞任意命令的口子——
    # 白名单与 admin/app_admin.py 的 RPC_COMMANDS 保持一致，但**独立**：面板那边
    # 是它自己的边界，这里是内网通道的边界，两道闸不合并。
    _RPC_COMMANDS = ("steer", "follow_up", "abort", "set_model", "set_thinking_level")

    @app.get("/api/internal/admin/stream/{task_id}")
    async def admin_stream(task_id: int, request: Request,
                           user=Depends(auth.require_admin)):
        """把计算进程 stdout 里的事件流转成 SSE，给值班台看。

        游标用的是 agent 那条 stdout 自己的 ``seq``，**不是** ``events.id``：
        那是两套不同的流，游标语义不能互换——换过去要么重连丢事件，要么重复。
        """
        store.get_task(task_id, is_admin=True)
        disp = getattr(app.state, "dispatcher", None)
        if disp is None:
            raise PreciseHTTPException(503, "调度器没有运行，看不到实时流")
        if int(task_id) not in disp.running:
            # 任务没在跑就别开长连接：面板会拿着 409 当成"流断了"然后无限重连。
            raise PreciseHTTPException(409, "这个任务的计算进程不在运行，没有实时流")
        raw_after = (request.headers.get("Last-Event-ID")
                     or request.query_params.get("after") or 0)
        try:
            after = int(raw_after)
        except (TypeError, ValueError):
            after = 0

        def _gen():
            try:
                for obj in disp.stream(int(task_id), after=after):
                    seq = obj.get("seq", 0)
                    data = json.dumps(obj, ensure_ascii=False)
                    yield f"id: {seq}\nevent: agent\ndata: {data}\n\n"
            except Exception:                       # noqa: BLE001
                log.exception("任务 %s 的实时流出错", task_id)
                yield 'event: error\ndata: {"detail": "实时流中断"}\n\n'
            # 明确收尾：面板靠它停止重连，而不是等超时
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/internal/admin/rpc/{task_id}")
    def admin_rpc(task_id: int, body: RpcCmdIn, user=Depends(auth.require_admin)):
        """把一条干预命令送进计算进程（spec §8.1 的 RPC 闭环）。

        这一层做的是**形状翻译**：面板发的是 ``{"command": "steer", "message": ...}``，
        agent 认的是 ``{"type": "steer", "message": ...}``，中间就差这一段。
        """
        command = (body.command or "").strip()
        if command not in _RPC_COMMANDS:
            raise PreciseHTTPException(400, f"不支持的干预命令：{command}")
        store.get_task(task_id, is_admin=True)

        disp = getattr(app.state, "dispatcher", None)
        if disp is None:
            raise PreciseHTTPException(503, "调度器没有运行，无法干预计算进程")

        # abort 直接杀进程树，不透传给 agent。面板上那个按钮的意思是"停掉这个计算"，
        # 不是"让当前这轮回答先说完"——留着进程继续烧 GPU 比多等几秒优雅重要。
        if command == "abort":
            stopped = disp.abort(task_id, reason="管理员中止")
            sse.notify()
            return {"ok": True, "result": {"aborted": bool(stopped)}}

        if command in ("steer", "follow_up"):
            text = (body.message or "").strip()
            if not text:
                raise PreciseHTTPException(400, "干预内容不能为空")
            wire = {"type": command, "message": text}
        elif command == "set_model":
            model = (body.model or "").strip()
            if not model:
                raise PreciseHTTPException(400, "模型名不能为空")
            wire = {"type": "set_model",
                    "provider": (body.provider or "").strip() or dispatcher.LLM_PROVIDER_ID,
                    "modelId": model}
        else:                                                   # set_thinking_level
            level = (body.thinking_level or "").strip()
            if not level:
                raise PreciseHTTPException(400, "思考档位不能为空")
            wire = {"type": "set_thinking_level", "level": level}

        try:
            result = disp.rpc(task_id, wire)
        except dispatcher.AgentGone:
            raise PreciseHTTPException(
                409, "计算进程已经不在了，干预命令没有发出去") from None
        except dispatcher.RpcTimeout:
            raise PreciseHTTPException(504, "计算进程没有在期限内回应") from None
        sse.notify()
        return {"ok": True, "result": result}

    @app.post("/api/internal/admin/repack/{task_id}")
    def repack(task_id: int, user=Depends(auth.require_admin)):
        """把任务推回 packaging，让打包器再打一次。

        存在的理由：产物靠时间窗兜底发现时 verified=0（"先显示、再确认"），
        管理员在产物列表里点完确认之后需要一个重新打包的入口。
        只认 failed 与 packaging——已经是 done 的任务重打会把已有 zip 覆盖掉，
        那种事该由人显式决定，不该由一个通用端点顺手做掉。
        """
        task = store.get_task(task_id, is_admin=True)
        if task["status"] not in ("failed", "packaging"):
            raise HTTPException(
                409, f"任务现在是{db.STATUS_LABEL.get(task['status'], task['status'])}，"
                     "不需要重新打包")
        store.update_task(task_id, error="")
        updated = store.set_status(task_id, "packaging", actor="admin",
                                   message="已重新排队打包")
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
