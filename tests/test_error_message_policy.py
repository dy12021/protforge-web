"""错误文案策略：同一个错，两个面板必须说同一句中文（修中低危②）。

背景
----
``_HTTP_DETAIL`` 曾经有两份拷贝：``server/app.py`` 侧有 422/503，
``admin/app_admin.py`` 侧没有，键集合已经漂了；两边的注释还互相矛盾
（一边写"不要给 401/403 配条目"，另一边写"401/403/422 不配条目"）。后果是同一个
框架层错误在两个面板上显示成不同的东西，一处中文一处英文短语。

所以这里钉三件事，每一条都能真的抓到回归：

1. **只有一份定义**（源码级计数 + 运行期两个 app 拿到的是同一个对象）
2. **同一组状态码，两个 app 产出同一条 detail，且没有一条是英文短语**
3. **我们自己抛的中文 detail 没被覆盖**（app 侧 PreciseHTTPException、
   面板侧 PanelError；auth.py 的 401/403 也得保住）

策略本身写在 ``server/app.py`` 的 HTTP_DETALL 注释里：我们自己的异常类一律
原样透出，映射表只留给框架层兜底的（404 路由不存在、405 方法不对、422 校验失败）。
401/403 刻意不配条目——auth.py 的 Unauthorized/Forbidden 也走这条兜底，而它们
没法用我们的子类（auth 被 app import，反过来会成环）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route

import app as svc

REPO = Path(__file__).resolve().parent.parent
SOURCES = (REPO / "server" / "app.py", REPO / "admin" / "app_admin.py")

# 面板在 admin\ 下（server\ 由 conftest 加上），这里补另一半。做法与
# admin/app_admin.py 自己往 sys.path 里塞 SERVER_DIR 是同一套。
ADMIN_DIR = REPO / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

import app_admin as panel  # noqa: E402

# 自己抛的三种（app 侧两种 + 面板一种）。源码里只该有这三个 + 框架的 HTTPException。
OWN_RAISES = {"PreciseHTTPException", "PanelError", "HTTPException"}


def is_chinese(detail) -> bool:
    return isinstance(detail, str) and any("一" <= ch <= "鿿" for ch in detail)


# ---------------------------------------------------------------- 探针

def _detail_for(app, status: int, detail=None, exc_class=StarletteHTTPException):
    """在 app 上临时挂一条"抛 <status>"的探针路由，返回 (状态码, detail)。

    插到路由表**最前面**：管理员面板的 `/{path:path}` 是静态页兜底，挂最后，
    插后面这条探针会被它吃掉（换个 404，什么都测不到）。
    """
    async def _boom(request):
        raise exc_class(status_code=status, detail=detail)

    app.router.routes.insert(0, Route("/__probe", _boom, methods=["GET"]))
    with TestClient(app) as client:
        r = client.get("/__probe")
    return r.status_code, r.json()["detail"]


@pytest.fixture
def public():
    return svc.create_public_app()


@pytest.fixture
def admin():
    return panel.create_admin_app()


# ---------------------------------------------------------------- 只有一份定义

def test_http_detail_defined_exactly_once():
    """两个文件加起来只能有一处映射表字面量。抄第二份就是缺陷②本身。"""
    pat = re.compile(r"^\s*_?HTTP_DETAIL\s*=\s*\{", re.M)
    hits = {p: len(pat.findall(p.read_text(encoding="utf-8"))) for p in SOURCES}
    assert sum(hits.values()) == 1, hits


def test_admin_uses_the_same_object():
    """不是"抄得一样"，是**同一个对象**：抄的下一轮就会再漂。"""
    assert panel.svc.HTTP_DETAIL is svc.HTTP_DETAIL
    assert panel.svc.user_detail is svc.user_detail


def test_both_handlers_go_through_user_detail():
    """两个 app 的兜底处理器都必须走那一个函数（静态钉，防止有人绕过它直接查表）。"""
    for path in SOURCES:
        src = path.read_text(encoding="utf-8")
        assert "user_detail(" in src, path
        assert "_HTTP_DETAIL.get(" not in src, path


# ---------------------------------------------------------------- 同一组状态码

@pytest.mark.parametrize("status", sorted(svc.HTTP_DETAIL))
def test_same_status_same_chinese_detail(public, admin, status):
    """框架层兜底的每个码，两个 app 必须给出同一条中文。"""
    p_code, p_detail = _detail_for(public, status)
    a_code, a_detail = _detail_for(admin, status)

    assert p_code == a_code == status
    assert p_detail == a_detail, (status, p_detail, a_detail)
    assert is_chinese(p_detail), (status, p_detail)
    # 框架默认的英文短语（"Not Found" 之类）必须已经被换掉
    assert p_detail != StarletteHTTPException(status_code=status).detail


@pytest.mark.parametrize("status", [401, 403])
def test_auth_style_401_403_keep_their_chinese_detail(public, admin, status):
    """401/403 不在表里，靠"原样透出"保住 auth.py 写的中文。

    auth.py 不能用我们的异常子类（auth 被 app import，反过来会成环），所以这两个
    码一旦被加进表，就会把「登录已失效，请重新登录」覆盖成一句通用话。
    """
    msg = "登录已失效，请重新登录" if status == 401 else "这个面板只对管理员开放"
    p_code, p_detail = _detail_for(public, status, msg)
    a_code, a_detail = _detail_for(admin, status, msg)

    assert p_code == a_code == status
    assert p_detail == a_detail == msg


@pytest.mark.parametrize("status,msg", [
    (409, "结果包还在生成中，任务完成后才能下载"),
    (404, "页面不存在"),
    (422, "任务已经结束了，不能再中止"),
    (503, "调度器没有运行，看不到实时流"),
])
def test_public_keeps_its_own_chinese_detail(public, status, msg):
    """app 侧靠 PreciseHTTPException 保住具体原因，一个字都不许被通用话覆盖。

    曾经各错一半：PreciseHTTPException 本身是好的，可 abort / 下载 / 收口那些
    **用普通 HTTPException 抛的** 409/422 照样被表覆盖成「请求冲突」「请求参数
    不合法」——用户点了中止，告诉他"参数不合法"。
    """
    code, detail = _detail_for(public, status, msg, svc.PreciseHTTPException)
    assert code == status
    assert detail == msg, (status, detail)


@pytest.mark.parametrize("status,msg", [
    (404, "面板文件缺失"),
    (422, "至少要保留一个可用的管理员账号"),
    (401, "用户名或密码不对"),
    (403, "这个面板只对管理员开放"),
])
def test_admin_keeps_its_own_chinese_detail(admin, status, msg):
    """面板侧靠 PanelError 保住具体原因。

    原来没有这条：`PanelError(404, "面板文件缺失")` 会被表覆盖成「接口不存在」，
    面板文件真缺了也看不出来。**每个 app 只保自己的类**——另一个 app 的异常类
    在这个 app 里就是框架兜底，不在 `own` 里。
    """
    code, detail = _detail_for(admin, status, msg, panel.PanelError)
    assert code == status
    assert detail == msg, (status, detail)


# ---------------------------------------------------------------- 真实端点

def _logged_in_public(public):
    import auth
    svc.db.init_db()
    auth.create_user("user1", "password-123", role="user")
    with TestClient(public) as client:
        r = client.post("/api/auth/login",
                        json={"username": "user1", "password": "password-123"})
        assert r.status_code == 200, r.text
        headers = {"Authorization": "Bearer " + r.json()["token"]}
        r = client.post("/api/tasks", json={"title": "分析 1ubq", "brief": ""},
                        headers=headers)
        assert r.status_code == 201, r.text
        task = r.json()["task"]

        import store
        store.set_status(task["id"], "done", actor="system")
        abort = client.post(f"/api/tasks/{task['id']}/abort", headers=headers)
        # 下载端点：任务还没打包时的那条 409
        store.update_task(task["id"], status="running")
        pkg = client.get(f"/api/tasks/{task['id']}/package", headers=headers)
    return abort, pkg


def test_real_endpoints_keep_their_specific_reason(public):
    """端到端：用户中止 / 下载未就绪这两条，必须是具体那句话而不是通用话。"""
    abort, pkg = _logged_in_public(public)
    assert abort.status_code == 422
    # 不比全句：中止那条是 f"任务已经{状态标签}了，不能再中止"，标签由 db 决定。
    # 只钉两件事——具体原因在，且没被换成表里的通用话。
    assert "不能再中止" in abort.json()["detail"], abort.text
    assert abort.json()["detail"] != svc.HTTP_DETAIL[422]

    assert pkg.status_code == 409
    assert pkg.json()["detail"] == "结果包还在生成中，任务完成后才能下载", pkg.text
    assert pkg.json()["detail"] != svc.HTTP_DETAIL[409]


# ---------------------------------------------------------------- 真实路由

def test_real_routes_agree(public, admin):
    """不打探针，走真实路由：同一个错在两个面板上同一句话。"""
    p = TestClient(public)
    a = TestClient(admin)

    # 404 路由不存在
    assert p.get("/api/nope").json()["detail"] == a.get("/api/admin/nope").json()["detail"]
    # 405 方法不对
    assert (p.get("/api/auth/login").json()["detail"]
            == a.post("/api/admin/me").json()["detail"])
    # 401 没带令牌：auth.py 的 Unauthorized 在两个 app 上同一句
    assert p.get("/api/me").json()["detail"] == a.get("/api/admin/me").json()["detail"]


def test_no_english_leaks_from_our_own_raises():
    """静态钉死"表外的码必须自带中文 detail"。

    表外的码走 ``str(exc.detail)`` 原样透出，所以裸抛一处英文，就是一条英文短语
    直接漏给用户。规则（源码级，两个文件一起查）：

    1. detail 是字符串字面量 → 必须含中文；
    2. detail 是算出来的（变量 / 调用，静态判不了）→ 那条 raise 必须用我们自己的
       异常类（PreciseHTTPException / PanelError），否则它不受表保护，
       值里有没有中文就完全没人管了；
    3. 干脆不带 detail → 那个码必须在 HTTP_DETAIL 里，否则透出的就是框架那句
       英文短语（"Not Found"）。
    """
    table = svc.HTTP_DETAIL
    problems = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            name = getattr(node.exc.func, "id", "")
            if name not in OWN_RAISES:
                continue
            args = node.exc.args
            code = (args[0].value if args and isinstance(args[0], ast.Constant)
                    else None)
            detail_node = args[1] if len(args) > 1 else None
            where = f"{path.name}:{node.lineno} {name}({code})"
            if detail_node is None:
                if code not in table:
                    problems.append(f"{where} 裸抛且不在映射表里 → 会漏英文短语")
            elif isinstance(detail_node, (ast.Constant, ast.JoinedStr)):
                if not is_chinese(ast.unparse(detail_node)):
                    problems.append(
                        f"{where} detail 没有中文：{ast.unparse(detail_node)}")
            elif name == "HTTPException":
                problems.append(f"{where} detail 是算出来的，却没用我们自己的"
                                f"异常类（{ast.unparse(detail_node)}）")
    assert not problems, "\n".join(problems)
