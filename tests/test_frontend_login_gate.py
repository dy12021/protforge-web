"""「登录与否由服务端说了算」这条契约的围栏。

缺陷是怎么来的
--------------
两个前端的 ``boot()`` 早先都写着::

    if (!token()) return showLogin();

这是**替服务端猜**：localStorage 里有没有令牌，只说明"上次点过登录"，
不代表"现在还登录着"。于是只要服务端不要求 Bearer 令牌（临时登录旁路
``auth.DEV_LOGIN_ENV`` 开着就是这种情况），``/api/me`` 明明返回 200，
面板却永远停在登录框——**接口全通、看板全空**，页面骨架是静态 HTML，
看着像"打开了但没数据"。

同一个 bug 的第二种表现：兜底轮询也拿 ``if (!token()) return;`` 当闸，
于是旁路下 SSE 断了也不会兜底刷新。

所以这里钉两件事，缺一不可：

1. **服务端那边**（真行为）：没令牌时 ``/api/me`` 到底答什么。旁路关 →
   401；旁路开 → 200 并指明是谁。前端敢不敢问，取决于这个答案靠不靠谱。
2. **前端那边**（源码扫描）：``boot()`` 必须去问身份接口，且不得再拿
   ``token()`` 预先短路；兜底轮询的闸必须是 ``state.me``。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
for d in (SERVER_DIR, ADMIN_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

import app as svc                      # noqa: E402
import app_admin as admin_svc          # noqa: E402
import auth                            # noqa: E402

REPO = Path(__file__).resolve().parent.parent
USER_JS = REPO / "web" / "app.js"
ADMIN_JS = REPO / "admin-ui" / "app.js"

GOOD_PW = "password-123"


def _func_body(src: str, name: str) -> str:
    """取 ``async function name() { … }`` 的函数体（按大括号配平）。

    正则扫源码本来就是这套契约测试的基本手法（见 test_frontend_url_contract），
    所以这里沿用同一套思路，只是要按括号配平而不是扫到行尾。
    """
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"找不到 function {name}()，前端改版了？"
    i = src.index("{", m.end())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    raise AssertionError(f"{name}() 的花括号不配平")


# ---------------------------------------------------------------- 服务端
# 前端敢"先问后进"的前提是这个问题有确定答案。两侧都钉住：旁路关时它答
# 401（前端弹登录框），开着时它答 200（前端直接进看板）。


@pytest.fixture
def user_named():
    return auth.create_user("gateuser", GOOD_PW, role="user")


@pytest.fixture
def admin_named():
    return auth.create_user("gateadmin", GOOD_PW, role="admin")


def test_me_without_token_is_401_when_the_bypass_is_off(user_named):
    """旁路关着时，无令牌问身份必须 401 —— 前端据此弹登录框。"""
    assert auth.dev_login_user() is None, "本机环境变量里还留着旁路，测试不成立"
    with TestClient(svc.create_public_app()) as c:
        assert c.get("/api/me").status_code == 401


def test_admin_me_without_token_is_401_when_the_bypass_is_off(admin_named):
    with TestClient(admin_svc.create_admin_app()) as c:
        assert c.get("/api/admin/me").status_code == 401


def test_me_without_token_is_200_when_the_bypass_is_on(user_named, monkeypatch):
    """旁路开着时，无令牌问身份必须 200 并指出是谁。

    这条是前端那个修复能成立的全部依据：服务端在"没有令牌"时仍然认得你，
    客户端就不该拿"有没有令牌"当登录与否的判据。
    """
    monkeypatch.setenv(auth.DEV_LOGIN_ENV, "admin:" + user_named["username"])
    with TestClient(svc.create_public_app()) as c:
        r = c.get("/api/me")
    assert r.status_code == 200, r.text
    assert r.json()["username"] == user_named["username"], r.text


def test_admin_me_without_token_is_200_when_the_bypass_is_on(admin_named, monkeypatch):
    monkeypatch.setenv(auth.DEV_LOGIN_ENV, "admin:" + admin_named["username"])
    with TestClient(admin_svc.create_admin_app()) as c:
        r = c.get("/api/admin/me")
    assert r.status_code == 200, r.text
    assert r.json()["username"] == admin_named["username"], r.text


def test_the_bypass_still_refuses_a_non_admin_on_the_admin_panel(user_named, monkeypatch):
    """旁路不是万能钥匙：角色是 user 时管理员面板仍然 403。

    没有这条，``PROTFORGE_WEB_DEV_LOGIN=user:xxx`` 会把 8474 一并放开。
    """
    monkeypatch.setenv(auth.DEV_LOGIN_ENV, "user:" + user_named["username"])
    with TestClient(admin_svc.create_admin_app()) as c:
        assert c.get("/api/admin/me").status_code == 403


# ---------------------------------------------------------------- 前端源码

PANELS = [("用户面板", USER_JS, "/api/me"), ("管理员面板", ADMIN_JS, "/me")]


@pytest.mark.parametrize("label,path,me_path", PANELS,
                         ids=[p[0] for p in PANELS])
def test_boot_asks_the_server_who_you_are(label, path, me_path):
    body = _func_body(path.read_text(encoding="utf-8"), "boot")
    assert f'"{me_path}"' in body, (
        f"{label}的 boot() 没有去问 {me_path} —— 它必须让服务端回答登录与否，"
        "而不是自己在客户端猜")


@pytest.mark.parametrize("label,path,me_path", PANELS,
                         ids=[p[0] for p in PANELS])
def test_boot_does_not_short_circuit_on_the_local_token(label, path, me_path):
    """在**问身份之前**不得拿本地令牌短路。

    ``if (!token()) return showLogin();`` 是本缺陷本体。断言只看身份请求
    **之前**的那一段——之后 catch 分支里读一次 ``token()`` 只是为了区分
    「登录已过期」和「请登录」两种提示语，那时服务端已经答过 401 了。
    """
    body = _func_body(path.read_text(encoding="utf-8"), "boot")
    ask_at = body.index(f'"{me_path}"')
    before = body[:ask_at]
    offenders = [ln.strip() for ln in before.splitlines() if "token()" in ln]
    assert not offenders, (
        f"{label}的 boot() 在问服务端之前就拿 localStorage 里的令牌当登录判据："
        f"{offenders}；令牌只说明『上次点过登录』，旁路下压根没有令牌")


@pytest.mark.parametrize("label,path,me_path", PANELS,
                         ids=[p[0] for p in PANELS])
def test_signed_out_clears_the_identity_so_polling_stops(label, path, me_path):
    """退出后必须把身份清掉。

    兜底轮询改成拿 ``state.me`` 当闸之后，这就是它唯一的停机信号；
    不清的话退出登录后页面还会继续每 10 秒打一次接口。
    """
    body = _func_body(path.read_text(encoding="utf-8"), "signedOut")
    assert "state.me" in body, f"{label}的 signedOut() 没清 state.me，轮询不会停"


@pytest.mark.parametrize("label,path,me_path", PANELS,
                         ids=[p[0] for p in PANELS])
def test_the_fallback_poll_is_gated_on_identity_not_on_the_token(label, path, me_path):
    src = path.read_text(encoding="utf-8")
    m = re.search(r"setInterval\(\(\)\s*=>\s*\{(.*?)\n\},", src, re.S)
    assert m, f"{label}找不到兜底轮询的 setInterval"
    cb = m.group(1)
    assert "state.me" in cb, f"{label}的兜底轮询没拿 state.me 当闸"
    assert "token()" not in cb, (
        f"{label}的兜底轮询还在 if (!token()) return; —— 旁路下没有令牌，"
        "SSE 一断看板就再也不刷新了")


def test_the_extractor_finds_boot_and_signed_out():
    """防恒真：扫描器坏了要立刻红，不能"扫到 0 行所以通过"。"""
    src = USER_JS.read_text(encoding="utf-8")
    assert "await boot()" in src
    assert _func_body(src, "boot").strip()
    assert _func_body(src, "signedOut").strip()
