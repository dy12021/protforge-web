"""值班台实时流端点的真跑测试（spec §8.1 / §13 `GET /api/admin/stream/{id}`）。

这个端点曾经**每个请求都抛 TypeError**，而界面完全看不出来：

    resp = await iterate_in_threadpool(_open)
    TypeError: object async_generator can't be used in 'await' expression

`iterate_in_threadpool` 是把同步**生成器**包成异步生成器的，调用它得到的
是一个 async generator，再 await 它必然炸。_open 是普通函数、返回一个
HTTPResponse，该用的是 run_in_threadpool。

为什么整轮真机测试都没发现：前端开流失败后是**静默**的——

    try { const r = await fetch(apiBase() + "/stream/" + id, ...); ... }
    catch (e) { if (ctl.stop) return; }
    await sleep(3000);

于是每 3 秒重试一次、每次都在服务端抛异常，而卡片照样靠 10 秒轮询刷新，
看起来一切正常。这已经是本项目同一种形状的第七次：前端加载时不报错、
界面上却是死的。

所以这里**真起 app、真打这个端点**，而不是扫源码：
源码断言只能证明"写得像对的"，证明不了 FastAPI/Starlette 真的会返回 200。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import store

ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

import app_admin      # noqa: E402

APP_ADMIN_SRC = Path(app_admin.__file__)

PWD = "password-123"

PAYLOAD = (b"event: step\n"
           b"data: {\"seq\": 1, \"kind\": \"step\", \"message\": \"\\u6b63\\u5728\\u7b97\"}\n\n")


class _FakeUpstream:
    """内部通道的替身：给一段会读完就结束的内容。

    真接内部 SSE 会永不结束，测试就会挂住——所以这里只验证**代理这一段**
    （开流、转发、收尾），不验证内部端口自己的无限流。
    """

    def __init__(self, payload: bytes = PAYLOAD):
        self._buf = payload
        self.closed = False

    def read(self, n: int = 4096) -> bytes:
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def owner():
    return auth.create_user("owner1", PWD, role="user")


@pytest.fixture
def admin():
    return auth.create_user("boss", PWD, role="admin")


@pytest.fixture
def task_id(owner):
    return store.create_task(title="t", brief="b", owner_id=owner["id"])["id"]


@pytest.fixture
def upstream(monkeypatch):
    """把面板打开内部流的这一次调用换掉，其余一概不动。"""
    fake = _FakeUpstream()
    opened = []

    def fake_urlopen(req, timeout=None):
        opened.append(req)
        return fake

    monkeypatch.setattr(app_admin.urllib.request, "urlopen", fake_urlopen)
    fake.opened = opened
    return fake


def _bearer(user):
    return {"Authorization": f"Bearer {auth.issue_token(user['id'])}"}


def test_the_stream_endpoint_actually_returns_a_stream(upstream, task_id, admin):
    """端点必须真的返回 200 + text/event-stream。

    修复前这里抛 TypeError：TestClient 默认 raise_server_exceptions=True，
    异常会直接冒出测试函数，于是这条断言是红的。
    """
    app = app_admin.create_admin_app()
    with TestClient(app) as c:
        r = c.get(f"/api/admin/stream/{task_id}", headers=_bearer(admin))

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream"), (
        f"不是 SSE 响应：{r.headers.get('content-type')}")
    assert "event: step" in r.text, f"上游内容没有被转发过来：{r.text[:200]}"


def test_the_stream_actually_reached_the_internal_port(upstream, task_id, admin):
    """不能为了"不报错"而假装成功——必须真的去连内部端口，
    并且把管理员 Bearer 原样带过去（内部端点校验的是它，不是 agent_token）。"""
    app = app_admin.create_admin_app()
    with TestClient(app) as c:
        c.get(f"/api/admin/stream/{task_id}", headers=_bearer(admin))

    assert len(upstream.opened) == 1, "没有真的去连内部通道"
    req = upstream.opened[0]
    assert req.full_url.endswith(f"/api/internal/admin/stream/{task_id}"), req.full_url
    sent = {k.lower(): v for k, v in req.headers.items()}
    # 键名用 urllib 的 add_header 会被首字母大写（Authorization），
    # 所以先统一小写再查，别直接找 "bearer" 当键名。
    assert "authorization" in sent, f"没带 Authorization 头：{sent}"
    assert sent["authorization"].startswith("Bearer "), sent


def test_the_upstream_body_gets_closed(upstream, task_id, admin):
    """读完必须 close 上游连接，否则每次开流漏一个 socket。"""
    app = app_admin.create_admin_app()
    with TestClient(app) as c:
        c.get(f"/api/admin/stream/{task_id}", headers=_bearer(admin))
    assert upstream.closed, "上游连接没有关闭——每开一次流漏一个 socket"


def test_stream_still_requires_an_admin_token(upstream, owner, task_id):
    """修 bug 不能顺手把鉴权放松：普通用户订阅别人的实时流必须被拒。"""
    app = app_admin.create_admin_app()
    with TestClient(app) as c:
        r = c.get(f"/api/admin/stream/{task_id}", headers=_bearer(owner))
    assert r.status_code in (401, 403), r.text


def test_last_event_id_is_forwarded(upstream, admin, task_id):
    """断线重连靠 Last-Event-ID 续传，面板转发时必须带上。"""
    app = app_admin.create_admin_app()
    with TestClient(app) as c:
        c.get(f"/api/admin/stream/{task_id}",
              headers={**_bearer(admin), "Last-Event-ID": "42"})
    sent = {k.lower(): v for k, v in upstream.opened[0].headers.items()}
    assert sent.get("last-event-id") == "42", sent


def test_no_stream_for_a_stopped_task_says_so_instead_of_blaming_the_channel(
        monkeypatch, admin, task_id):
    """409 必须原样透出，不能压成 502「内部通道没有响应」。

    内部端口对"这个任务的计算进程不在运行，没有实时流"回 409——那是**一句
    准确答案**。任务刚好在轮询与开流之间收口就会走到这里，属于常态。
    压成 502 的话，排查的人会去查内部通道是不是挂了，而通道明明活着。
    （_public_status 那套压法对 RPC 转发是对的，对开流不成立，两者不能混用。）
    """
    import io
    import urllib.error

    body = json.dumps({"detail": "这个任务的计算进程不在运行，没有实时流"}).encode()

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(body))

    monkeypatch.setattr(app_admin.urllib.request, "urlopen", fake_urlopen)
    app = app_admin.create_admin_app()
    with TestClient(app) as c:
        r = c.get(f"/api/admin/stream/{task_id}", headers=_bearer(admin))

    assert r.status_code == 409, f"状态码被压掉了：{r.status_code} {r.text}"
    assert "计算进程不在运行" in r.text, (
        f"内部端口的准确原因没有透出来：{r.text[:200]}")


def test_opening_the_stream_goes_through_the_threadpool():
    """开流必须丢进线程池——这一条**只能扫源码**，上面那些测不到。

    上面四条替身里的上游是瞬间返回的，所以"直接同步调 _open()"在行为上
    同样能返回 200：测试原理上看不见差别。但真的 urlopen 是阻塞的，
    在 async 端点里同步调会把整个事件循环卡住——值班台每 10 秒还要轮询
    一次，卡住的表现就是全面板一起僵住。

    明说覆盖边界：这条是源码断言，它能证明"写了 run_in_threadpool"，
    证明不了线程池真的没被阻塞。真要证明后者得让替身 read() 睡几秒、
    再并发打一个普通请求看它是否被拖住——那是性能回归的测试，
    而这次修的是"端点每个请求都抛异常"，不在那一档。
    """
    src = APP_ADMIN_SRC.read_text(encoding="utf-8")
    assert "resp = await run_in_threadpool(_open)" in src, (
        "开内部流没有走 run_in_threadpool：替身瞬间返回时测试看不出区别，"
        "但真的 urlopen 是阻塞的，同步调会卡住整个事件循环")
    # 读 body 那边反过来必须是 iterate_in_threadpool：_iter_body 是同步生成器
    assert "iterate_in_threadpool(_iter_body(resp))" in src, (
        "_iter_body 是同步生成器，迭代要交给 iterate_in_threadpool，"
        "别和开流那处搞混")

