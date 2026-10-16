"""SSE 实时流的测试（Task 6）。

三组测试各自的钉死项：

1. **内存总线层**——把轮询拉到 300 秒（等价于关掉兜底层），本进程经 API 写入的
   事件仍要在 5 秒内送到。轮询还在的话这条恒过，所以它钉的是"第一层真的存在"。
2. **断线重连不丢**——断开期间产生的事件靠 `Last-Event-ID` 游标补齐。这条比
   "能收到"重要：它测的是**没丢**。
3. **跨进程兜底层**——由**另一个进程**写库。主服务进程的内存总线**结构上**不可能
   被触发（两个进程不共享内存），只能靠 3 秒轮询。前两条都能靠总线过，只有这条
   能证伪"兜底层真的存在"。

**帧解析器是 web/app.js 的 parseFrame + readFrames 的忠实搬运**（注释行、
`\r\n` 归一、缓冲切 `\n\n` 三处都照抄）。SSE 的契约是前端定的，测试里另写一套
宽松解析等于没测。

**每条流都用 with 包住**：SSE 连接不关，uvicorn 关停期就会撞上 Windows 的
WinError 1005。take() 内部 catch 掉 ReadTimeout 转成"读到了几帧"，
不给失败留挂死的余地。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

import auth
import sse
import store
import app as svc

GOOD = "password-123"
SERVER_DIR = Path(__file__).resolve().parent.parent / "server"

# 收帧等待上限。轮询兜底是 3 秒，所以"应该看得见"的场景一律给到 8 秒；
# 读超时（下面 client.stream 的 read=）比它大，失败时靠 take 的 deadline 收场。
WAIT = 8.0


# ---------------------------------------------------------------- 夹具

@contextmanager
def _serve(application):
    """在回环地址的临时端口上真起一个 uvicorn。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error"))

    def _run():
        try:
            server.run()
        except OSError:
            # 关停期的 WinError 1005（socket 已被关闭）是 Windows 上的竞态，
            # 无害：启动阶段的 OSError（端口被占）反而必须暴露，所以判 started。
            if not server.started:
                raise

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        if not th.is_alive():
            break
        time.sleep(0.01)
    assert server.started, "uvicorn 没能启动"
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        server.should_exit = True
        th.join(timeout=15)


@pytest.fixture
def public():
    with _serve(svc.create_public_app()) as base:
        with httpx.Client(base_url=base, timeout=30) as c:
            yield c


def login(client, username="user1", password=GOOD):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def bearer(token, **extra):
    h = {"Authorization": f"Bearer {token}"}
    h.update(extra)
    return h


def make_user(username="user1", password=GOOD, role="user"):
    return auth.create_user(username, password, role=role)


def create_task(client, token, title="分析 1ubq", brief="分析 1ubq 的折叠稳定性"):
    r = client.post("/api/tasks", json={"title": title, "brief": brief},
                    headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["task"]


def edit(client, token, task_id, title="分析 1ubq（改）"):
    """走 PATCH 写一条 edited 事件。

    **必须走 API**：只有 API 层写完事件才调 sse.notify()，测试直接调
    store.add_event 不会叫醒总线，那样测的就不是第一层了。
    """
    r = client.patch(f"/api/tasks/{task_id}", json={"title": title},
                     headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()["events"][-1]


# ---------------------------------------------------------------- SSE 客户端
#
# 与 web/app.js:357-415 同构：字节流 -> 缓冲 -> 按 \n\n 切帧 -> 解析字段。

def _parse_frame(raw: str):
    """照搬 app.js 的 parseFrame。返回 None 表示注释/心跳（前端会跳过）。"""
    eid, ev, data = 0, "", ""
    for line in raw.split("\n"):
        if not line or line[0] == ":":
            continue                                  # 注释/心跳
        c = line.find(":")
        field = line if c < 0 else line[:c]
        value = "" if c < 0 else line[c + 1:]
        if value.startswith(" "):                     # 只去掉一个前导空格
            value = value[1:]
        if field == "id":
            eid = int(value or 0)
        elif field == "event":
            ev = value
        elif field == "data":
            data += (data and "\n" or "") + value
    if not data and not ev:
        return None
    payload = json.loads(data) if data else None
    return {"id": eid, "ev": ev, "data": data, "payload": payload}


class _Sse:
    """一条 SSE 连接。**必须用 with**，理由见模块 docstring。"""

    def __init__(self, client, task_id, token, last_event_id=None):
        headers = bearer(token)
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        # read 超时比 WAIT 大：测试里 POLL_SECONDS 被拉大时，靠它兜底不让
        # take() 挂死。正常路径上根本等不到它。
        self._cm = client.stream("GET", f"/api/tasks/{task_id}/stream",
                                 headers=headers, timeout=httpx.Timeout(WAIT * 2))
        self._resp = None
        self._raw = None

    def __enter__(self):
        self._resp = self._cm.__enter__()
        self._resp.raise_for_status()
        assert "text/event-stream" in self._resp.headers.get("content-type", "")
        self._raw = self._resp.iter_bytes()
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)

    def take(self, n=1, timeout=WAIT):
        """读满 n 帧就返回；超时返回已读到的（可能不足 n）。

        deadline 只在**收到数据之后**才检查——安静期里卡的是 read 超时，
        那条路也要能收场，所以上面把 read 设成 WAIT * 2。
        """
        out, buf = [], ""
        deadline = time.monotonic() + timeout
        try:
            for chunk in self._raw:
                buf += chunk.decode("utf-8", "replace").replace("\r\n", "\n")
                while "\n\n" in buf:
                    raw, _, buf = buf.partition("\n\n")
                    f = _parse_frame(raw)
                    if f is not None:
                        out.append(f)
                        if len(out) >= n:
                            return out
                if time.monotonic() > deadline:
                    break
        except httpx.ReadTimeout:
            pass
        return out


def _write_in_other_process(task_id: int, kind: str, message: str) -> int:
    """在**另一个进程**里写一条事件，返回它的事件 id。

    这是管理员面板（8474，独立进程）改状态时的真实拓扑：那个进程写完库就
    结束了，主服务进程的 asyncio.Event 绝不会替它触发。子进程靠继承环境变量
    拿到 conftest 指的临时库（db.py 每次调用现读 env，不在 import 时算死）。
    """
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(SERVER_DIR)!r})\n"
        "import store\n"
        f"print(store.add_event({int(task_id)}, {kind!r}, 'admin', {message!r}))\n"
    )
    out = subprocess.run([sys.executable, "-c", script], env=dict(os.environ),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, "子进程写事件失败：%s" % out.stderr
    return int(out.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- 契约

def test_stream_requires_a_token(public):
    make_user()
    task = create_task(public, login(public))
    r = public.get(f"/api/tasks/{task['id']}/stream")
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert isinstance(detail, str) and any("\u4e00" <= c <= "\u9fff" for c in detail)


def test_stream_hides_other_peoples_task(public):
    make_user("owner1")
    make_user("other1")
    task = create_task(public, login(public, "owner1"))
    r = public.get(f"/api/tasks/{task['id']}/stream",
                   headers=bearer(login(public, "other1")))
    # 404 不是 403：403 等于承认"这个 id 存在"
    assert r.status_code == 404, r.text


def test_frame_shape_matches_frontend_contract(public):
    """帧形状：id 行给事件 id（Last-Event-ID 靠它），data 是事件行本身。"""
    make_user()
    token = login(public)
    task = create_task(public, token)
    with _Sse(public, task["id"], token) as s:
        frames = s.take(1, timeout=5)
    assert len(frames) == 1, frames
    f = frames[0]
    assert f["id"] > 0, "id: 行必须是事件 id，否则重连补不齐"
    assert f["ev"] == "created", f
    p = f["payload"]
    # 前端 pushEvent 认 kind / to_status，eventLine 认 actor / created_at / message
    assert p["kind"] == "created" and p["actor"] == "human", p
    assert p["id"] == f["id"] and p["created_at"] and task["title"] in p["message"], p


def test_bad_last_event_id_falls_back_to_history(public):
    """Last-Event-ID 是脏数据时从头补，而不是 500 或静默丢事件。"""
    make_user()
    token = login(public)
    task = create_task(public, token)
    with _Sse(public, task["id"], token, last_event_id="not-a-number") as s:
        frames = s.take(1, timeout=5)
    assert len(frames) == 1 and frames[0]["payload"]["kind"] == "created", frames


# ---------------------------------------------------------------- 测试点 1

def test_sse_delivers_new_event_via_bus(public, monkeypatch):
    """订阅后写入一条事件，5 秒内应收到——**靠内存总线**。

    轮询拉到 300 秒：还能收到，就只能是总线叫醒的。删掉 notify() 这条就红，
    所以它钉的是"第一层真的存在"，而不是"兜底慢一点也能到"。
    """
    monkeypatch.setattr(sse, "POLL_SECONDS", 300.0)
    make_user()
    token = login(public)
    task = create_task(public, token)
    with _Sse(public, task["id"], token, last_event_id=0) as s:
        s.take(1, timeout=5)                       # 先吃掉 created，游标落在它身上
        ev = edit(public, token, task["id"])
        frames = s.take(1, timeout=5)
    assert len(frames) == 1, "写入后 5 秒内没收到事件（内存总线失灵？）"
    assert frames[0]["id"] == ev["id"], frames
    assert frames[0]["payload"]["kind"] == "edited"


# ---------------------------------------------------------------- 测试点 2

def test_sse_resumes_from_last_event_id(public):
    """断线重连靠 id 游标补齐，**不能丢事件**。

    这条比"能收到"重要：它测的是断开那段时间里写下的事件，一条都不能少。
    离线期间这几条是直接调 store.add_event 写的（不叫醒总线也不发 HTTP），
    所以补齐完全依赖重连后那次游标扫描。
    """
    make_user()
    token = login(public)
    task = create_task(public, token)

    with _Sse(public, task["id"], token, last_event_id=0) as s:
        first = s.take(1, timeout=5)
    assert len(first) == 1, first
    cursor = first[0]["id"]

    # —— 断线期间产生 3 条
    offline = [store.add_event(task["id"], "log", "agent", f"离线第 {i} 步")
               for i in range(3)]

    with _Sse(public, task["id"], token, last_event_id=cursor) as s:
        frames = s.take(3, timeout=5)

    got = [f["id"] for f in frames]
    assert got == offline, f"断线期间的事件丢了：期望 {offline}，实到 {got}"
    # 一条都不重不漏：重连后第一帧必须是断线前的下一条
    assert frames[0]["id"] > cursor


def test_reconnect_never_replays_before_the_cursor(public):
    """游标之前的历史不重推——否则重连一次时间线就翻倍。"""
    make_user()
    token = login(public)
    task = create_task(public, token)
    with _Sse(public, task["id"], token, last_event_id=0) as s:
        first = s.take(1, timeout=5)
    cursor = first[0]["id"]
    nxt = store.add_event(task["id"], "log", "agent", "断线之后")
    with _Sse(public, task["id"], token, last_event_id=cursor) as s:
        frames = s.take(1, timeout=5)
    assert [f["id"] for f in frames] == [nxt], frames


# ---------------------------------------------------------------- 测试点 3

def test_sse_sees_events_written_by_another_process(public):
    """跨进程专项：**另一个进程**写的事件，3 秒轮询窗口内必须看见。

    子进程写完就退出，主服务进程的内存总线不可能被触发——所以这条能证伪
    "兜底层真的存在"。只测前两条的话，删掉轮询测试照样全绿。
    轮询用真实的 3 秒（不 monkeypatch），管理员面板那条路就是这个配置。
    """
    make_user()
    token = login(public)
    task = create_task(public, token)
    with _Sse(public, task["id"], token, last_event_id=0) as s:
        s.take(1, timeout=5)                       # created，游标就位

        eid = _write_in_other_process(task["id"], "status", "管理员改成了计算中")
        frames = s.take(1, timeout=WAIT)           # 轮询 3 秒，WAIT 8 秒留余量

    assert len(frames) == 1, "跨进程写入的事件 %d 没有被轮询捞到" % eid
    assert frames[0]["id"] == eid
    assert frames[0]["payload"]["actor"] == "admin"


# ---------------------------------------------------------------- 连接寿命

def test_stream_stays_open_and_sends_ignorable_keepalive(public, monkeypatch):
    """服务端**不主动断流**，只发注释心跳。

    前端 catchUpHistory 靠 abort 收尾、startStream 靠退避重连，都假设长连接
    由客户端结束；服务端擅自 close 会让前端每 3 秒白重连一次。心跳是注释行，
    前端 parseFrame 直接跳过它（不触发 scheduleRefresh），所以必须 `:` 开头。
    """
    monkeypatch.setattr(sse, "KEEPALIVE_SECONDS", 0.5)
    make_user()
    token = login(public)
    task = create_task(public, token)
    with _Sse(public, task["id"], token, last_event_id=0) as s:
        s.take(1, timeout=5)                       # created
        # 等过一个轮询周期：心跳（约 3s）该发过了，且不能被当成事件帧
        assert s.take(1, timeout=0.5) == [], "注释心跳被当成了事件帧"
        # 连接还活着：改一次标题，总线一叫醒就该到
        ev = edit(public, token, task["id"])
        frames = s.take(1, timeout=5)
    assert len(frames) == 1 and frames[0]["id"] == ev["id"], frames
