"""账号、口令与登录令牌。

三条约定：

1. **只用标准库**：pbkdf2_hmac 而不是 bcrypt/passlib。这台机器的 Python 是
   `C:\\ProgramData\\anaconda3`，装包要动环境；口令哈希又不依赖任何第三方算法，
   标准库足够，200000 轮迭代对本地服务足够。
2. **`Invalid` 的 message 直接作为 API 的 detail**，所以恒为中文——与 store
   层同一个约定（app.py 的异常处理器两边一起接）。
3. **鉴权用 `Authorization: Bearer`，不用 Cookie**：天然免 CSRF。
   浏览器端把令牌放 localStorage，由前端自己加头。

登录失败计数放进程内 dict（不入库）：跨进程的锁定要落库，而落库就得处理多
进程同时写同一个 key 的问题，收益（防暴力破解）远小于那个复杂度。代价是重启
服务即清零，本机服务 + 12 小时令牌的场景下可接受。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import re
import secrets
import sqlite3
import time

from fastapi import Depends, HTTPException, Request

import db

ITERATIONS = 200_000
TOKEN_TTL_HOURS = 12
MIN_PASSWORD = 10
MAX_PASSWORD = 200
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
ROLES = ("user", "admin")

MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60

# 登录失败时间戳：{用户名: [ts, ...]}。只保留失败次数用得到的部分。
_FAILURES: dict[str, list[float]] = {}


class Invalid(Exception):
    """入参不合法。message 直接作为 API 的 detail，所以恒为中文。"""


class Unauthorized(HTTPException):
    """未登录 / 令牌无效。401。"""

    def __init__(self, detail: str = "登录已失效，请重新登录"):
        super().__init__(status_code=401, detail=detail)


class Forbidden(HTTPException):
    """已登录但无权。403。"""

    def __init__(self, detail: str = "没有权限执行这个操作"):
        super().__init__(status_code=403, detail=detail)


# ---------------------------------------------------------------- 口令

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            bytes.fromhex(salt), ITERATIONS)
    return h.hex(), salt


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    calc, _ = hash_password(password or "", salt_hex)
    return hmac.compare_digest(calc, hash_hex or "")


# ---------------------------------------------------------------- 账号

def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _iso(dt: datetime.datetime) -> str:
    """与 db.now_iso() 同格式，于是 expires_at 可以直接按字符串比大小。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _public(row) -> dict:
    """对外的账号视图。**不**带 password_hash / salt。"""
    return {"id": int(row["id"]), "username": row["username"], "role": row["role"]}


def create_user(username: str, password: str, role: str = "user") -> dict:
    username = (username or "").strip()
    password = password or ""
    if not USERNAME_RE.match(username):
        raise Invalid("用户名只能包含字母、数字、下划线、点和短横，长度 3-32")
    if len(password) < MIN_PASSWORD:
        raise Invalid(f"密码至少 {MIN_PASSWORD} 位")
    if len(password) > MAX_PASSWORD:
        raise Invalid(f"密码最长 {MAX_PASSWORD} 位")
    if password.lower() == username.lower():
        raise Invalid("密码不能与用户名相同")
    if role not in ROLES:
        raise Invalid(f"未知角色：{role}")
    h, s = hash_password(password)
    c = db.connect()
    try:
        with c:
            try:
                cur = c.execute(
                    """INSERT INTO users (username, password_hash, salt, role,
                                          disabled, created_at)
                       VALUES (?, ?, ?, ?, 0, ?)""",
                    (username, h, s, role, db.now_iso()),
                )
            except sqlite3.IntegrityError:
                raise Invalid("用户名已被占用")
            uid = cur.lastrowid
        row = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    finally:
        c.close()
    return _public(row)


def get_user(username_or_id) -> dict | None:
    """按用户名或 id 取账号。找不到返回 None。"""
    c = db.connect()
    try:
        if isinstance(username_or_id, int) or str(username_or_id).isdigit():
            row = c.execute("SELECT * FROM users WHERE id = ?",
                            (int(username_or_id),)).fetchone()
        else:
            row = c.execute("SELECT * FROM users WHERE username = ?",
                            (str(username_or_id).strip(),)).fetchone()
    finally:
        c.close()
    return _public(row) if row is not None else None


def set_disabled(username_or_id, disabled: bool = True) -> None:
    c = db.connect()
    try:
        with c:
            if isinstance(username_or_id, int) or str(username_or_id).isdigit():
                c.execute("UPDATE users SET disabled = ? WHERE id = ?",
                          (1 if disabled else 0, int(username_or_id)))
            else:
                c.execute("UPDATE users SET disabled = ? WHERE username = ?",
                          (1 if disabled else 0, str(username_or_id).strip()))
    finally:
        c.close()


# ---------------------------------------------------------------- 登录

def locked_out(username: str) -> float:
    """还锁着就返回剩余秒数，否则 0。"""
    fails = _FAILURES.get((username or "").strip() or "", [])
    if len(fails) < MAX_FAILURES:
        return 0.0
    left = LOCK_SECONDS - (time.time() - fails[0])
    return left if left > 0 else 0.0


def reset_lockouts(username: str | None = None) -> None:
    """清空失败计数（测试与运维用）。不传用户名则全清。"""
    if username is None:
        _FAILURES.clear()
    else:
        _FAILURES.pop((username or "").strip(), None)


def _record_failure(username: str) -> None:
    key = (username or "").strip()
    fails = [t for t in _FAILURES.get(key, []) if time.time() - t < LOCK_SECONDS]
    fails.append(time.time())
    _FAILURES[key] = fails[-MAX_FAILURES:]


def authenticate(username: str, password: str) -> dict | None:
    """核对口令。**任何失败都返回 None**，不区分"用户不存在"与"口令不对"。

    返回的是对外视图（无 hash/salt）。
    """
    username = (username or "").strip()
    if locked_out(username):
        return None
    c = db.connect()
    try:
        row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    finally:
        c.close()
    if row is None or int(row["disabled"]):
        _record_failure(username)
        return None
    if not verify_password(password or "", row["password_hash"], row["salt"]):
        _record_failure(username)
        return None
    _FAILURES.pop(username, None)
    return _public(row)


# ---------------------------------------------------------------- 令牌

def issue_token(user_id: int, hours: float = TOKEN_TTL_HOURS) -> str:
    """签发令牌，落库（sessions 表）——多进程共用一个库，不落库就跨进程失效。"""
    token = secrets.token_urlsafe(32)
    now = _now()
    c = db.connect()
    try:
        with c:
            c.execute(
                """INSERT INTO sessions (token, user_id, created_at, expires_at,
                                         last_seen_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (token, int(user_id), _iso(now),
                 _iso(now + datetime.timedelta(hours=hours)), _iso(now)),
            )
    finally:
        c.close()
    return token


def revoke_token(token: str) -> None:
    c = db.connect()
    try:
        with c:
            c.execute("DELETE FROM sessions WHERE token = ?", (token or "",))
    finally:
        c.close()


def token_expires_at(token: str) -> str | None:
    c = db.connect()
    try:
        row = c.execute("SELECT expires_at FROM sessions WHERE token = ?",
                        (token or "",)).fetchone()
    finally:
        c.close()
    return row["expires_at"] if row is not None else None


def resolve_token(token: str) -> dict | None:
    """令牌 -> 账号视图。过期、查不到、账号被停用，一律 None。"""
    token = (token or "").strip()
    if not token:
        return None
    c = db.connect()
    try:
        row = c.execute(
            """SELECT u.id AS id, u.username AS username, u.role AS role,
                      u.disabled AS disabled, s.expires_at AS expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ?""", (token,)).fetchone()
    finally:
        c.close()
    if row is None or int(row["disabled"]):
        return None
    # expires_at 与 _iso() 同格式，字符串比较即时间比较（都是本地时间、无时区）
    if row["expires_at"] <= _iso(_now()):
        return None
    return {"id": int(row["id"]), "username": row["username"], "role": row["role"]}


# ---------------------------------------------------------------- FastAPI 依赖

def bearer_token(request: Request) -> str:
    """从 Authorization 头取 Bearer 令牌。

    不用 Cookie：Cookie 会被浏览器自动带上，于是每个写接口都要额外防 CSRF；
    前端把令牌放 localStorage、自己加头，代价只是几行 JS。
    """
    raw = request.headers.get("Authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise Unauthorized("缺少登录令牌，请在 Authorization 头里带 Bearer <token>")
    return parts[1].strip()


def current_user(request: Request) -> dict:
    """FastAPI 依赖：无令牌 / 令牌过期 / 账号停用一律 401。"""
    user = resolve_token(bearer_token(request))
    if user is None:
        raise Unauthorized("登录已失效，请重新登录")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    """FastAPI 依赖：必须是 admin，否则 403。内部管理通道用它。"""
    if (user or {}).get("role") != "admin":
        raise Forbidden("需要管理员权限")
    return user


__all__ = [
    "Invalid", "Unauthorized", "Forbidden", "ITERATIONS", "TOKEN_TTL_HOURS",
    "hash_password", "verify_password", "create_user", "get_user",
    "set_disabled", "authenticate", "locked_out", "reset_lockouts",
    "issue_token", "revoke_token", "resolve_token", "token_expires_at",
    "bearer_token", "current_user", "require_admin",
]
