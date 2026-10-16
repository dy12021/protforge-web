"""值班台 -> 内部通道的**契约**测试。

为什么必须是契约而不是逐个手写
------------------------------
面板把请求转发到内部端口的某个 URL。如果那个 URL 不存在，**两边各自的测试都绿**：
面板测的是"转发逻辑写对了"，内部测的是"我有的路由都对"——没人测"它转发的那条
URL 在对面真的存在"。

这个洞已经真实漏过一次：``GET /api/admin/stream/{task_id}`` 转发到
``/api/internal/admin/stream/{task_id}``，而后者从没被实现，面板上打开实时视图恒 502，
前端还按退避逻辑无限重连。同形态的缺陷这已经是第三次（令牌表、打包器、这次的流）。

所以这里的做法是**扫源码里的转发 URL，逐条核对内部 app 的路由表**。以后面板加了新的
内部调用而忘了在对面实现，这条会立刻红。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

import app as svc

REPO = Path(__file__).resolve().parent.parent
ADMIN_SRC = (REPO / "admin" / "app_admin.py").read_text(encoding="utf-8")

# 面板拼内部 URL 的所有写法：f"{internal_base()}/api/..."、"/api/internal/..."
# 字符类要含 () ：f-string 插值里是 {int(task_id)}，不含括号就会在 {int 处截断。
FORWARD_RE = re.compile(r"/api/internal/[A-Za-z0-9_\-{}/{}:().]*")
# 路径里的 {task_id} / {int(task_id)} / {task_id:int} 都要归一成同一个占位符：
# 面板源码写的是 f-string，插值表达式可能是任意 Python（{int(task_id)}），
# 而 FastAPI 路由表里是 {task_id}。不归一就永远比不上。
PARAM_RE = re.compile(r"\{[^{}]*\}")


def _normalize(path: str) -> str:
    return PARAM_RE.sub("{x}", path)


def _internal_routes() -> set[str]:
    app = svc.create_internal_app()
    out = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if path and path.startswith("/api/internal/"):
            out.add(_normalize(path))
    return out


def _forwarded_paths() -> set[str]:
    """从面板源码里扫出所有它会请求的内部路径（不含查询串）。"""
    found = set()
    for m in FORWARD_RE.finditer(ADMIN_SRC):
        p = m.group(0).rstrip("/")
        if p.count("{") and p.endswith("}"):
            pass                       # 已是带参形式
        found.add(_normalize(p))
    return found


def test_panel_has_at_least_one_forwarded_path():
    """守卫自身：正则要是哪天写坏了，这条先红，别让它悄悄放行 0 条。"""
    paths = _forwarded_paths()
    assert paths, "没从面板源码里扫到任何内部路径——正则失效了？"
    assert "/api/internal/admin/stream/{x}" in paths, paths
    assert "/api/internal/admin/rpc/{x}" in paths, paths


def test_every_forwarded_path_exists_on_the_internal_app():
    """核心契约：面板转发的每一条，在内部 app 上都必须真的存在。"""
    missing = sorted(_forwarded_paths() - _internal_routes())
    assert not missing, (
        "管理员面板会转发到这些内部路径，但 internal app 上没有对应路由：\n  "
        + "\n  ".join(missing)
        + "\n面板上按这些按钮只会拿到 502，而且前端会按退避逻辑无限重连。"
    )


def test_internal_app_has_no_route_that_public_app_lacks_as_internal():
    """反向核对：内部路由集合本身不该为空（防止上面两条一起恒真）。"""
    routes = _internal_routes()
    assert len(routes) >= 5, routes
    for must in ("/api/internal/agent/{x}/step",
                 "/api/internal/agent/{x}/finish",
                 "/api/internal/admin/rpc/{x}",
                 "/api/internal/admin/stream/{x}",
                 "/api/internal/admin/repack/{x}"):
        assert must in routes, f"内部 app 缺少 {must}"


def test_public_app_never_exposes_internal_routes():
    """反代会把它一并转发出去——这是双端口隔离的第二道闸。"""
    app = svc.create_public_app()
    leaked = [r.path for r in app.routes
              if getattr(r, "path", "").startswith("/api/internal/")]
    assert not leaked, leaked


# ---------------------------------------------------------------- stream 端点本身

@pytest.fixture
def env():
    app = svc.create_internal_app()
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        c.app = app
        yield c, app


def test_stream_requires_admin(env):
    import auth
    from fastapi.testclient import TestClient
    c, app = env
    u = auth.create_user("plain", "password-123", role="user")
    tid = svc.store.create_task(title="t", brief="b", owner_id=u["id"])["id"]
    r = c.get(f"/api/internal/admin/stream/{tid}",
              headers={"Authorization": f"Bearer {auth.issue_token(u['id'])}"})
    assert r.status_code == 403, r.text


def test_stream_503_when_dispatcher_not_running(env):
    import auth
    c, app = env
    admin = auth.create_user("boss", "password-123", role="admin")
    tid = svc.store.create_task(title="t", brief="b", owner_id=admin["id"])["id"]
    app.state.dispatcher = None
    r = c.get(f"/api/internal/admin/stream/{tid}",
              headers={"Authorization": f"Bearer {auth.issue_token(admin['id'])}"})
    assert r.status_code == 503, r.text
    assert "调度器" in r.json()["detail"]


def test_stream_409_when_task_not_running(env):
    """任务没在跑就别开长连接：面板会拿 409 当成流断了，然后无限重连。"""
    import auth
    c, app = env
    admin = auth.create_user("boss", "password-123", role="admin")

    class NoTasks:
        running: dict = {}

    tid = svc.store.create_task(title="t", brief="b", owner_id=admin["id"])["id"]
    app.state.dispatcher = NoTasks()
    r = c.get(f"/api/internal/admin/stream/{tid}",
              headers={"Authorization": f"Bearer {auth.issue_token(admin['id'])}"})
    assert r.status_code == 409, r.text
