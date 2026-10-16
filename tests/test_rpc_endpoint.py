"""内部 RPC 转发端点的钉死项（/api/internal/admin/rpc/{task_id}）。

这一层存在的唯一理由是**翻译**：面板发 ``{"command": "steer", "message": ...}``，
agent 认 ``{"type": "steer", "message": ...}``。翻译错了不会抛异常——命令会被
agent 当成不认识的类型丢掉，而面板上显示"已送达"。所以逐条钉死。

这里不用 test_app 里的 internal 夹具：那是 httpx Client，拿不到 app.state，
没法塞假的调度器进去。直接建 app + TestClient，app.state 由测试自己控制。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import auth
import dispatcher
import store
import app as svc

ADMIN_PWD = "password-123"


@pytest.fixture
def env():
    """(client, app)。调度器没起（测试不开 PROTFORGE_WEB_WORKERS），由各用例自己塞。"""
    app = svc.create_internal_app()
    with TestClient(app) as c:
        c.app = app                      # 仅为测试方便：挂上去好设 state
        yield c, app


@pytest.fixture
def admin_tok():
    admin = auth.create_user("boss", ADMIN_PWD, role="admin")
    return auth.issue_token(admin["id"])


@pytest.fixture
def plain_tok():
    u = auth.create_user("plain", ADMIN_PWD, role="user")
    return auth.issue_token(u["id"])


@pytest.fixture
def tid(owner):
    return store.create_task(title="t", brief="b", owner_id=owner["id"])["id"]


@pytest.fixture
def owner():
    return auth.create_user("owner1", ADMIN_PWD, role="user")


class FakeDisp:
    """只记录被转发出去的命令，不真起进程。"""

    def __init__(self):
        self.sent = []
        self.aborted = []

    def rpc(self, task_id, wire, timeout=None):
        self.sent.append((task_id, wire))
        return {"type": "response", "success": True}

    def abort(self, task_id, reason=None):
        self.aborted.append((task_id, reason))
        return True


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------- 白名单与鉴权

def test_rpc_needs_admin(env, tid, plain_tok):
    c, app = env
    app.state.dispatcher = FakeDisp()
    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "steer", "message": "停下"}, headers=bearer(plain_tok))
    assert r.status_code == 403, r.text


def test_rpc_needs_admin_on_unknown_task(env, plain_tok):
    c, app = env
    app.state.dispatcher = FakeDisp()
    r = c.post("/api/internal/admin/rpc/999999",
               json={"command": "steer", "message": "停下"}, headers=bearer(plain_tok))
    assert r.status_code in (403, 404), r.text


def test_command_outside_whitelist_is_refused(env, tid, admin_tok):
    """面板是值班台，不该成为往计算进程里塞任意命令的口子。"""
    c, app = env
    fake = FakeDisp()
    app.state.dispatcher = fake
    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "run_shell", "message": "rm -rf /"},
               headers=bearer(admin_tok))
    assert r.status_code == 400, r.text
    assert "干预命令" in r.json()["detail"]
    assert fake.sent == [], "被拒的命令不该发出去"


# ---------------------------------------------------------------- 形状翻译

def test_steer_is_translated_to_the_wire_protocol(env, tid, admin_tok):
    c, app = env
    fake = FakeDisp()
    app.state.dispatcher = fake

    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "steer", "message": "先跑 freesasa"},
               headers=bearer(admin_tok))

    assert r.status_code == 200, r.text
    # 面板发 command/message，发给 agent 的必须是 type/message —— 少这层翻译，
    # agent 会静默丢掉，面板上却显示"已送达"
    assert fake.sent == [(tid, {"type": "steer", "message": "先跑 freesasa"})]
    assert r.json()["ok"] is True


def test_follow_up_is_translated(env, tid, admin_tok):
    c, app = env
    fake = FakeDisp()
    app.state.dispatcher = fake
    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "follow_up", "message": "再补个图表"},
               headers=bearer(admin_tok))
    assert r.status_code == 200, r.text
    assert fake.sent[0][1] == {"type": "follow_up", "message": "再补个图表"}


def test_set_model_fills_the_platform_provider(env, tid, admin_tok):
    """面板只给模型名，provider 由平台补——它知道自己在用哪个 provider。"""
    c, app = env
    fake = FakeDisp()
    app.state.dispatcher = fake

    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "set_model", "model": "MiniMax-M3.1-Flash-Preview"},
               headers=bearer(admin_tok))

    assert r.status_code == 200, r.text
    assert fake.sent[0][1] == {"type": "set_model",
                               "provider": dispatcher.LLM_PROVIDER_ID,
                               "modelId": "MiniMax-M3.1-Flash-Preview"}


def test_set_model_with_empty_name_is_refused(env, tid, admin_tok):
    c, app = env
    fake = FakeDisp()
    app.state.dispatcher = fake
    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "set_model"}, headers=bearer(admin_tok))
    assert r.status_code == 400, r.text
    assert fake.sent == []


def test_set_thinking_level_is_translated(env, tid, admin_tok):
    c, app = env
    fake = FakeDisp()
    app.state.dispatcher = fake
    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "set_thinking_level", "thinking_level": "high"},
               headers=bearer(admin_tok))
    assert r.status_code == 200, r.text
    assert fake.sent[0][1] == {"type": "set_thinking_level", "level": "high"}


def test_steer_with_empty_message_is_refused(env, tid, admin_tok):
    c, app = env
    fake = FakeDisp()
    app.state.dispatcher = fake
    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "steer", "message": "   "},
               headers=bearer(admin_tok))
    assert r.status_code == 400, r.text
    assert fake.sent == [], "空内容不该被发出去"


# ---------------------------------------------------------------- abort 走另一条路

def test_abort_kills_the_process_rather_than_forwarding(env, tid, admin_tok):
    """abort 不透传：面板上那个按钮的意思是"停掉这个计算"。

    透传给 agent 只会让当前这轮回答停下，进程还活着继续烧 GPU。
    """
    c, app = env
    fake = FakeDisp()
    app.state.dispatcher = fake

    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "abort"}, headers=bearer(admin_tok))

    assert r.status_code == 200, r.text
    assert fake.sent == [], "abort 不该走透传"
    assert fake.aborted and fake.aborted[0][0] == tid


# ---------------------------------------------------------------- 故障要说人话

def test_503_when_dispatcher_is_not_running(env, tid, admin_tok):
    """PROTFORGE_WEB_WORKERS 没开时要说人话，不能变成 500 或 AttributeError。"""
    c, app = env
    app.state.dispatcher = None
    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "steer", "message": "在吗"}, headers=bearer(admin_tok))
    assert r.status_code == 503, r.text
    assert "调度器" in r.json()["detail"]


def test_agent_gone_becomes_409(env, tid, admin_tok):
    class Gone(FakeDisp):
        def rpc(self, task_id, wire, timeout=None):
            raise dispatcher.AgentGone("计算进程已退出")

    c, app = env
    app.state.dispatcher = Gone()
    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "steer", "message": "在吗"}, headers=bearer(admin_tok))
    assert r.status_code == 409, r.text


def test_rpc_timeout_becomes_504(env, tid, admin_tok):
    class Slow(FakeDisp):
        def rpc(self, task_id, wire, timeout=None):
            raise dispatcher.RpcTimeout("没在期限内回应")

    c, app = env
    app.state.dispatcher = Slow()
    r = c.post(f"/api/internal/admin/rpc/{tid}",
               json={"command": "steer", "message": "在吗"}, headers=bearer(admin_tok))
    assert r.status_code == 504, r.text


def test_public_app_has_no_rpc_route():
    """内部通道的端点绝不能出现在 public app 上——反代会把它一并转发出去。"""
    with TestClient(svc.create_public_app()) as c:
        r = c.post("/api/internal/admin/rpc/1", json={"command": "steer"})
        assert r.status_code in (404, 405), r.text
