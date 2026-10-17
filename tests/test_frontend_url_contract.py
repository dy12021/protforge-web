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
USER_JS = REPO / "web" / "app.js"
ADMIN_JS = REPO / "admin-ui" / "app.js"

PARAM_RE = re.compile(r"\{[^{}]*\}")
#: JS 表达式里的一枚 token：字符串字面量，或变量（id / t.id / row.id…）
_TOK = r'(?:"[^"]*"|[A-Za-z_$][\w$.]*)'
#: 路径表达式 = 若干 token 用 + 串起来，例如 "/api/tasks/" + id + "/stream"
_PATH_EXPR = rf'({_TOK}(?:\s*\+\s*{_TOK})*)'
_TOKEN_RE = re.compile(_TOK)


def _normalize(path: str) -> str:
    return PARAM_RE.sub("{x}", path)


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
    head = r'(?:api\s*\(\s*"(?:GET|POST|PATCH|DELETE)"|apiBase\(\)\s*\+)\s*,?\s*'
    for m in re.finditer(head + _PATH_EXPR, src):
        parts = []
        for tok in _TOKEN_RE.finditer(m.group(1)):
            t = tok.group(0)
            parts.append(t[1:-1] if t.startswith('"') else "{x}")
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
    assert m, "找不到 apiBase()，前端改版了？"
    body = m.group(1)
    # 取 return 语句里与空串相加的那个字面量（若存在）
    r = re.search(r'return\s+[^;]*?\+\s*"([^"]*)"\s*;', body)
    return r.group(1) if r else ""


# ---------------------------------------------------------------- 守卫自身


def test_scanner_finds_the_known_endpoints():
    """防恒真：正则坏了要立刻红，不能"扫到 0 条所以通过"。"""
    user = _js_paths(USER_JS.read_text(encoding="utf-8"))
    assert len(user) >= 8, user
    assert "/api/auth/login" in user, user
    assert "/api/tasks" in user, user
    admin = _js_paths(ADMIN_JS.read_text(encoding="utf-8"))
    assert len(admin) >= 8, admin
    assert "/login" in admin, admin


# ---------------------------------------------------------------- 用户面板


def test_user_panel_api_base_does_not_double_prefix():
    """apiBase() 在同源时必须只返回源前缀，不能再拼 /api。

    调用处写的已经是 ``/api/xxx``，这里再拼一次就成了 ``/api/api/xxx``。
    """
    suffix = _api_base_suffix(USER_JS.read_text(encoding="utf-8"))
    assert suffix == "", (
        f"apiBase() 又拼了一次 {suffix!r}，而所有调用处已经带 /api —— "
        "拼出来是 /api/api/xxx，面板每个接口都会 404")


def test_every_user_panel_url_exists_in_the_public_app():
    routes = _routes_of(svc.create_public_app())
    missing = []
    for p in _js_paths(USER_JS.read_text(encoding="utf-8")):
        n = _normalize(p)
        if n not in routes:
            missing.append(n)
    assert not missing, f"前端会请求这些地址，服务端一条都没有：{sorted(missing)}"


def test_no_user_panel_url_has_a_doubled_api_segment():
    for p in _js_paths(USER_JS.read_text(encoding="utf-8")):
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
