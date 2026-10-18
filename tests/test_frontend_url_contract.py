"""前端 JS 的 URL 与服务端路由表的**契约**测试。

为什么必须有
------------
这个洞是**真机点一次登录**才暴露的，前后一共骗过了所有自动化手段：

- 466 条后端测试：它们打的是服务端自己的路由，不知道前端拼了什么；
- 静态资源测试：只检查三个文件发得出来、字段约束在，**不看 URL**；
- 早先那次「61 秒端到端」：驱动它的是 Python 脚本，直接打 API，没经过界面。

于是用户面板的 ``apiBase()`` 在返回值后面多拼了一次 ``/api``，而所有调用处
写的已经是 ``/api/xxx``，拼出来是 ``/api/api/xxx``——**面板每一个接口全部
404**，可页面骨架是静态 HTML，渲染得完全正常，看着像「能打开」。
管理员面板同形态，只是只有 4 个干预动作中招（中止 / 改状态 / 重跑 / 产物确认），
其余因为调用处写的是相对路径而侥幸没事。

所以这里扫两个前端的源码，把它们会请求的 URL 逐条拿出来，
① 核对前缀没重复，② 核对服务端真的有这条路由。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "admin"))

import app as svc                      # noqa: E402
import app_admin as admin_svc          # noqa: E402

REPO = Path(__file__).resolve().parent.parent
#: 用户前端的**全部**源文件。工作流页是新文件（规格 8.4）：不把它列进来，
#: 它请求的每一条 URL 都不会进契约检查，而扫描器漏扫**不报错**，
#: 只会安静地放过——那正是这块围栏当初要拦的形状。
USER_JS = [REPO / "web" / "app.js", REPO / "web" / "workflow.js"]
ADMIN_JS = REPO / "admin-ui" / "app.js"


def _user_srcs() -> list[str]:
    return [p.read_text(encoding="utf-8") for p in USER_JS]


PARAM_RE = re.compile(r"\{[^{}]*\}")
#: JS 表达式里的一枚 token：字符串字面量、**反引号模板串**，或变量（id / t.id / row.id…）
#: 反引号这一支是后补的：模板串写出来的 URL 曾经一条都扫不到，于是
#: 「前端请求了服务端没有的地址」这类错在新页面上完全隐形——扫不到 0 条
#: 不报错，只会安静地放过。
_TOK = r'(?:"[^"]*"|`[^`]*`|[A-Za-z_$][\w$.]*)'
#: 路径表达式 = 若干 token 用 + 串起来，例如 "/api/tasks/" + id + "/stream"
_PATH_EXPR = rf'({_TOK}(?:\s*\+\s*{_TOK})*)'
_TOKEN_RE = re.compile(_TOK)


def _normalize(path: str) -> str:
    # 查询串要剥掉：FastAPI 的路由表里只有路径，没有 ?row= 这种东西。
    # 不剥的话，任何带查询串的调用都会算成「服务端没有这条路由」。
    return PARAM_RE.sub("{x}", path.split("?", 1)[0])


def _routes_of(app) -> set[str]:
    out = set()
    for r in app.routes:
        p = getattr(r, "path", None)
        if p:
            out.add(_normalize(p))
    return out


def _js_paths(src: str) -> list[str]:
    """扫出 `api("METHOD", <路径表达式>)` 与 `fetch(apiBase() + <路径表达式>)` 里的路径。

    路径表达式是字符串字面量与变量用 ``+`` 串起来的形式，例如
    ``"/api/tasks/" + id + "/stream"``。字面量原样保留（**剥掉引号**），
    变量换成 ``{x}``，于是拼出来的形状能和 FastAPI 的路由表逐段比对。
    """
    found: list[str] = []
    # fetch(apiBase( 这一支必须排在裸 fetch 前面。
    #
    # 正则备选是从左往右试、先成先 commit 的。`fetch(apiBase() + "/api/tasks/" + id)`
    # 里，若裸 fetch 排在前面，它会在更靠左的 fetch( 处就匹配上，于是 apiBase()
    # 被当成一个变量 token -> "{x}"，整条路径不以 / 开头被丢弃；而且 finditer
    # 从这个匹配点**之后**继续扫，那条真实路径再也不会被看到。
    #
    # 实测（不加 fetch 分支 / 第一版的顺序 / 下面的顺序）：
    #   web/app.js        13 条 / 10 条（丢 /api/tasks/{x}/stream、/api/tasks/{x}/package）/ 13 条
    #   admin-ui/app.js   14 条 / 13 条（丢 /stream/{x}）/ 14 条
    head = (r'(?:api\s*\(\s*"(?:GET|POST|PATCH|DELETE)"'
            r'|apiBase\(\)\s*\+'
            r'|fetch\(\s*apiBase\(\)\s*\+'
            r'|fetch\(\s*)'
            r'\s*,?\s*')
    for m in re.finditer(head + _PATH_EXPR, src):
        parts = []
        for tok in _TOKEN_RE.finditer(m.group(1)):
            t = tok.group(0)
            if t.startswith('"'):
                parts.append(t[1:-1])
            elif t.startswith('`'):
                parts.append(re.sub(r"\$\{[^{}]*\}", "{x}", t[1:-1]))
            else:
                parts.append("{x}")
        path = "".join(parts)
        # 必须以 / 开头。api() 辅助函数内部那句 `apiBase() + path` 会被同一个
        # 规则扫到，但那里的 path 是个变量（扫出来是光秃秃的 {x}），
        # 它是转发不是端点，混进来只会把路由表的比对变成噪声。
        if path.startswith("/"):
            found.append(path)
    return found


def _api_base_suffix(src: str) -> str:
    """apiBase() 在同源（没有 ?base=）时追加的固定后缀。

    同源时它应当只返回源前缀、后缀是空串；返回非空就说明和调用处重复了。
    """
    m = re.search(r"function\s+apiBase\s*\(\s*\)\s*\{(.*?)\n\}", src, re.S)
    if not m:
        # 没定义 apiBase() 是**正常**的：不是每个前端文件都自带基址。
        # 只有 user_panel 那个文件有。以前这里直接 assert，于是「把扫描范围
        # 扩到第二个文件」这件事本身就会把测试跑挂。
        return None
    body = m.group(1)
    # 取 return 语句里与空串相加的那个字面量（若存在）
    r = re.search(r'return\s+[^;]*?\+\s*"([^"]*)"\s*;', body)
    return r.group(1) if r else ""


# ---------------------------------------------------------------- 守卫自身


def test_scanner_finds_the_known_endpoints():
    """防恒真：正则坏了要立刻红，不能"扫到 0 条所以通过"。"""
    # 登录/任务看板这套流程只在 app.js 里，逐文件的门槛见下面那条
    user = _js_paths(USER_JS[0].read_text(encoding="utf-8"))
    assert len(user) >= 8, user
    assert "/api/auth/login" in user, user
    assert "/api/tasks" in user, user
    admin = _js_paths(ADMIN_JS.read_text(encoding="utf-8"))
    assert len(admin) >= 8, admin
    assert "/login" in admin, admin


# ---------------------------------------------------------------- 用户面板


# 参数化用**路径**而不是源码文本：_user_srcs() 会在模块 import 时读文件，
# 而 workflow.js 是在这个文件之后才落地的——import 期就炸的话，整个模块
# 收集失败，报出来的是 FileNotFoundError，连「哪个文件缺」都指向别的模块。
@pytest.mark.parametrize("path", USER_JS, ids=lambda p: p.name)
def test_user_panel_api_base_does_not_double_prefix(path):
    """apiBase() 在同源时必须只返回源前缀，不能再拼 /api。

    调用处写的已经是 ``/api/xxx``，这里再拼一次就成了 ``/api/api/xxx``。
    每个文件各判一次；没定义 apiBase() 的那个文件返回 None，同样合法。
    """
    suffix = _api_base_suffix(path.read_text(encoding="utf-8"))
    assert suffix in ("", None), (
        f"apiBase() 又拼了一次 {suffix!r}，而所有调用处已经带 /api —— "
        "拼出来是 /api/api/xxx，面板每个接口都会 404")


def test_every_user_panel_url_exists_in_the_public_app():
    routes = _routes_of(svc.create_public_app())
    missing = []
    for src in _user_srcs():
        for p in _js_paths(src):
            n = _normalize(p)
            if n not in routes:
                missing.append(n)
    assert not missing, f"前端会请求这些地址，服务端一条都没有：{sorted(missing)}"


def test_no_user_panel_url_has_a_doubled_api_segment():
    for src in _user_srcs():
        for p in _js_paths(src):
            assert "/api/api/" not in p, f"路径里有重复的 /api：{p}"


# ---------------------------------------------------------------- 管理员面板


def test_admin_panel_urls_all_carry_its_own_prefix():
    """管理员面板的 apiBase() 自带 /api/admin，调用处只写相对路径。

    写全路径的那几处（中止/改状态/重跑/产物确认）曾经被拼成
    /api/admin/api/admin/... —— 也就是**所有干预按钮**都点不动。
    """
    for p in _js_paths(ADMIN_JS.read_text(encoding="utf-8")):
        assert not p.startswith("/api/"), (
            f"管理员面板的调用处写了全路径 {p!r}，会与 apiBase() 的 /api/admin 重复；"
            "这里应该只写相对路径（/tasks、/rpc/{x}…）")


def test_every_admin_panel_url_exists_in_the_admin_app():
    routes = _routes_of(admin_svc.create_admin_app())
    missing = []
    for p in _js_paths(ADMIN_JS.read_text(encoding="utf-8")):
        # 管理员面板的调用处一律写相对路径（上面那条守卫已钉死），
        # 补上 apiBase() 自带的 /api/admin 才是它真正会打的地址。
        full = _normalize("/api/admin" + p)
        if full not in routes:
            missing.append(full)
    assert not missing, f"前端会请求这些地址，管理面板没有：{sorted(missing)}"


# ------------------------------------------------- 扫描器自身的围栏（防恒真）


@pytest.mark.parametrize("src,expected", [
    ('api("GET", "/api/workflows/" + id)', "/api/workflows/{x}"),
    ('api("GET", `/api/workflows/${id}/preview`)', "/api/workflows/{x}/preview"),
    ('api("GET", `/api/workflows/${id}/preview?row=${r}`)',
     "/api/workflows/{x}/preview?row={x}"),
    ('fetch("/api/workflows/upload", { method: "POST" })', "/api/workflows/upload"),
])
def test_scanner_sees_every_url_writing_style(src, expected):
    """防恒真：扫不到必须立刻红，不能「扫到 0 条所以通过」。

    这四种写法就是本项目实际用过的全部形态。少认一种，对应的 URL 就
    整片脱离契约检查——而**没有任何一条测试会因此变红**。
    """
    assert expected in _js_paths(src), f"漏扫：{src!r} -> {_js_paths(src)}"


@pytest.mark.parametrize("name,path,expected", [
    ("app.js", REPO / "web" / "app.js",
     ["/api/tasks/{x}/stream", "/api/tasks/{x}/package"]),
    ("admin-ui/app.js", REPO / "admin-ui" / "app.js", ["/stream/{x}"]),
])
def test_widening_the_scanner_does_not_lose_urls(name, path, expected):
    """扩扫描器的动作**不许**弄丢已经扫到的路径。

    加 `fetch(` 备选时踩过一次：`fetch(apiBase() + …)` 在更靠左的 `fetch(`
    处就匹配上，apiBase() 被当成变量 -> "{x}"，整条被丢弃，而 finditer 从
    那里继续扫，真实路径再也不被看到。条数从 13 掉到 10，**一条测试都没红**。

    这条防的不是「扫得不够多」，是「越改越少」——那种退化最隐蔽。
    """
    found = _js_paths(path.read_text(encoding="utf-8"))
    for p in expected:
        assert p in found, f"{name} 弄丢了 {p}；现在扫到 {found}"


def test_every_user_frontend_file_contributes_paths():
    """每个用户前端文件都必须贡献至少一条 URL。

    只扫到 app.js 一个文件的退化，用「总条数 >= N」抓不到（app.js 自己就有
    13 条），所以必须**逐文件**断言。
    """
    for p in USER_JS:
        n = len(_js_paths(p.read_text(encoding="utf-8")))
        assert n >= 1, f"{p.name} 一条 URL 都没扫到（文件没被扫到，或写法没被认）"
