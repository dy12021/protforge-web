"""auth.py 的测试（Task 2）。

前六条是计划里逐字列出的；后面几条补的是"只靠计划那六条测不出来"的东西：
令牌撤销、登录锁定、FastAPI 依赖的 401/403 判定——这三条恰好是双端口方案里
鉴权最容易被绕过的地方。
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

import auth

GOOD = "password-123"


def _request(headers: dict | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/api/me",
                    "query_string": b"", "headers": raw})


# ---------------------------------------------------------------- 口令

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


def test_duplicate_username_rejected():
    auth.create_user("dave", "good-password-1")
    with pytest.raises(auth.Invalid):
        auth.create_user("dave", "another-password-1")


def test_bad_username_rejected():
    # 用户名会进日志、进路径、当前端输入框：只收字母数字与 _ . -
    for bad in ("ab", "a b", "用户一", "x" * 40, ""):
        with pytest.raises(auth.Invalid):
            auth.create_user(bad, "good-password-1")


def test_invalid_message_is_chinese():
    # detail 恒为中文字符串是全站承诺，auth 抛的异常直接被 handler 当 detail 用
    with pytest.raises(auth.Invalid) as e:
        auth.create_user("bob", "12345678")
    assert any("\u4e00" <= ch <= "\u9fff" for ch in str(e.value))


def test_role_must_be_known():
    with pytest.raises(auth.Invalid):
        auth.create_user("erin", "good-password-1", role="root")


# ---------------------------------------------------------------- 登录

def test_authenticate_ok():
    auth.create_user("carol2", GOOD)
    user = auth.authenticate("carol2", GOOD)
    assert user and user["username"] == "carol2"
    assert "password_hash" not in user and "salt" not in user


def test_authenticate_wrong_password_is_none():
    auth.create_user("carol3", GOOD)
    assert auth.authenticate("carol3", "nope-nope-nope") is None
    assert auth.authenticate("nobody", GOOD) is None


def test_disabled_user_cannot_login():
    auth.create_user("carol", "good-password-1")
    auth.set_disabled("carol", True)
    assert auth.authenticate("carol", "good-password-1") is None


def test_locked_after_five_failures():
    """连错 5 次锁 15 分钟：此时**输对口令也不放行**，否则限流形同虚设。"""
    auth.reset_lockouts()
    auth.create_user("frank", GOOD)
    for _ in range(5):
        assert auth.authenticate("frank", "wrong-wrong-1") is None
    assert auth.authenticate("frank", GOOD) is None
    auth.reset_lockouts()
    assert auth.authenticate("frank", GOOD) is not None


# ---------------------------------------------------------------- 令牌

def test_token_roundtrip():
    u = auth.create_user("gina", GOOD)
    tok = auth.issue_token(u["id"])
    got = auth.resolve_token(tok)
    assert got and got["username"] == "gina"


def test_token_expires():
    # 计划里的草稿写的是 issue_token(1, hours=-1)——那个 id 不存在，
    # sessions.user_id 有外键，会直接 IntegrityError。造一个真用户再签。
    u = auth.create_user("leo", GOOD)
    tok = auth.issue_token(u["id"], hours=-1)
    assert auth.resolve_token(tok) is None


def test_unknown_token_is_none():
    assert auth.resolve_token("not-a-token") is None
    assert auth.resolve_token("") is None


def test_revoke_token():
    u = auth.create_user("hank", GOOD)
    tok = auth.issue_token(u["id"])
    assert auth.resolve_token(tok)
    auth.revoke_token(tok)
    assert auth.resolve_token(tok) is None


def test_disabled_user_token_stops_working():
    u = auth.create_user("ivy", GOOD)
    tok = auth.issue_token(u["id"])
    assert auth.resolve_token(tok)
    auth.set_disabled("ivy", True)
    assert auth.resolve_token(tok) is None


# ---------------------------------------------------------------- FastAPI 依赖

def test_current_user_rejects_missing_header():
    with pytest.raises(auth.Unauthorized) as e:
        auth.current_user(_request())
    assert e.value.status_code == 401


def test_current_user_rejects_bad_scheme():
    with pytest.raises(auth.Unauthorized):
        auth.current_user(_request({"Authorization": "Basic dXNlcjpwYXNz"}))


def test_current_user_accepts_bearer():
    u = auth.create_user("jack", GOOD)
    tok = auth.issue_token(u["id"])
    user = auth.current_user(_request({"Authorization": f"Bearer {tok}"}))
    assert user["id"] == u["id"]


def test_require_admin_blocks_plain_user():
    u = auth.create_user("kate", GOOD)
    with pytest.raises(auth.Forbidden):
        auth.require_admin({"id": u["id"], "username": "kate", "role": "user"})


def test_require_admin_allows_admin():
    a = auth.create_user("root-admin", GOOD, role="admin")
    user = auth.require_admin(a)
    assert user["role"] == "admin"
