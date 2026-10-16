"""SSE 实时流（Task 6）。

这个模块存在的理由是用户那句"计算几小时，不能干等着看不到发生什么"：一次
rfdiffusion 跑几小时，前端能做的只有等，等到用户以为服务死了。所以时间线的
每一条都必须**当场**推到浏览器，而不是让前端去轮。

两层机制，**缺一不可**：

1. **进程内事件总线**（`_Bus`）——本进程写完事件调 `notify()`，立刻叫醒所有
   订阅它的 SSE 连接。零延迟，覆盖"用户自己改标题/中止"这类同进程写入。
2. **低频轮询兜底**——每 `POLL_SECONDS`（3 秒）扫一次 `events` 的 `id > 游标`。

第二条不是可选的装饰。**管理员面板是独立进程**（8474），agent 通道也是独立
进程（8475，`python server/app.py internal` 与 public 各起一个 uvicorn）：它
们写库时主服务进程的 `asyncio.Event` 根本不会被触发——两个进程不共享内存。只靠
总线的话，管理员改状态用户侧永远看不到变化。tests/test_sse.py 里
test_sse_sees_events_written_by_another_process 专门证伪这一点。

**总线只负责叫醒，内容永远从库里读。** 叫醒之后仍然走 `store.list_events` 的
游标查询：这样"漏了一次叫醒"最多是延迟 3 秒，不会丢事件。宁可延迟，不可达。
反过来说，如果在总线里直接塞事件对象，多进程那条路就会各走各的游标，各补各的。

游标用 `events.id`（自增主键），所以 `id:` 行直接给事件 id，浏览器重连带
`Last-Event-ID` 就能从断点续上，不重不漏。

**调用点是 API 层**（app.py 里写完事件后调 `notify()`），不是 store.py：store
属于别的 worker，且数据层不该知道有没有人在听。直接调 `store.add_event` 的地方
（测试、将来的 janitor/dispatcher）不会触发总线，靠 3 秒轮询兜住。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import AsyncIterator

import store

# 兜底轮询间隔。3 秒是取舍：管理员改状态到用户看见最多晚 3 秒，而一次扫描是
# 一条 `WHERE task_id = ? AND id > ?` 的索引查询，代价可以忽略。
POLL_SECONDS = 3.0

# 注释心跳。留连接活着（反代的 proxy_read_timeout 一般 60s），也顺带暴露
# "对端已经没了"。心跳是 `:` 开头的注释行：前端 parseFrame 直接跳过，
# 不会误当成事件帧去触发一次任务详情刷新。
KEEPALIVE_SECONDS = 15.0
KEEPALIVE = ": keepalive\n\n"

# 一次唤醒最多补多少帧。一次 rfdiffusion 也就几百条事件，取不满也不影响
# 正确性：取满就立刻再扫一轮继续补，游标不会漏。
BURST = 500


class _Bus:
    """进程内事件总线：只做"叫醒"，不做分发队列。

    每个订阅者一个 `asyncio.Event`，而不是共用一个：共用的话 A 连接清掉标志
    会把 B 连接的唤醒吃掉。

    **为什么 notify 走 `call_soon_threadsafe`**：app.py 的路由处理器是同步
    `def`，Starlette 把它们扔进线程池执行，于是 `store.add_event` 跑在
    worker 线程里。`asyncio.Event.set()` 从别的线程调会撞上
    `call_soon` 的线程检查（直接抛 RuntimeError），所以必须显式回到事件循环
    上设标志。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
        self._seq = 0

    def subscribe(self) -> tuple[int, asyncio.Event]:
        """登记一个等待者，返回 (注销用的 key, Event)。只能在事件循环线程调。"""
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        with self._lock:
            self._seq += 1
            key = self._seq
            self._waiters[key] = (loop, wake)
        return key, wake

    def unsubscribe(self, key: int) -> None:
        with self._lock:
            self._waiters.pop(key, None)

    def notify(self) -> None:
        """叫醒所有订阅者。任何线程都能调；没有订阅者是零成本的空转。"""
        with self._lock:
            waiters = list(self._waiters.values())
        for loop, wake in waiters:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                # 循环已关（服务正在停）：这条订阅会随连接一起消失，忽略。
                pass

    def waiter_count(self) -> int:
        with self._lock:
            return len(self._waiters)


bus = _Bus()


def notify() -> None:
    """本进程刚写完事件后调它。**API 层在写完 store 事件后必须调这个。**"""
    bus.notify()


def format_sse(event_id, kind: str, data) -> str:
    """一条事件 -> 一帧。

    `id:` 行给 events.id（前端重连时回填 `Last-Event-ID`），`data` 是事件行
    本身——与 `GET /api/tasks/{id}` 里的 events 字段同构，前端时间线才能把
    推来的和拉来的混在同一个 Map 里排。

    帧用 LF 换行而不是 CRLF：前端会把 `\r\n` 归一成 `\n` 再切帧，而 `\r` 和
    `\n` 一旦被分到两个 TCP 分片上就归一不掉了。纯 LF 没有这个边界情况。

    `data` 保证单行：json.dumps 会把消息里的换行转义成 `\\n` 两个字符，
    不会真的断行。
    """
    body = json.dumps(data, ensure_ascii=False, default=str)
    return f"id: {int(event_id)}\nevent: {kind}\ndata: {body}\n\n"


def format_event(row: dict) -> str:
    """store.list_events 的一行 -> 一帧。"""
    return format_sse(row.get("id") or 0, row.get("kind") or "log", dict(row))


async def stream(task_id: int, request, last_seen: int = 0) -> AsyncIterator[str]:
    """一个任务的 SSE 帧流。

    `last_seen` 是客户端的游标（`Last-Event-ID` 头或 `last_event_id` 查询参数），
    0 表示"从头补"——前端打开终态任务的抽屉就是走这条补读整条时间线。

    每一轮都是同一套：扫游标 -> 有就发 -> 没有才等。**首轮不等**，所以重连时
    断线期间攒下的事件是立刻补齐的，不用先睡一个轮询周期。

    等待用 `wait_for(wake.wait(), POLL_SECONDS)`：总线叫醒就立刻醒，没人叫
    就三秒后自己醒一次再扫。两个条件合成一个等待，不需要 select 或定时器。

    先 clear 再 wait 不会漏：clear 与上一个扫描之间没有 await 点，而
    notify() 是从别的线程把 `set` 排进事件循环的，真正执行必然发生在下一次
    await 之后——也就是 clear 之后。
    """
    cursor = max(0, int(last_seen or 0))
    key, wake = bus.subscribe()
    last_beat = time.monotonic()
    try:
        while True:
            if await request.is_disconnected():
                return
            rows = store.list_events(task_id, after_id=cursor, limit=BURST)
            for row in rows:
                cid = int(row["id"])
                if cid > cursor:
                    cursor = cid
                    yield format_event(row)
            if not rows:
                # 安静期：够久就发个注释心跳（前端会跳过），再等
                now = time.monotonic()
                if now - last_beat >= KEEPALIVE_SECONDS:
                    last_beat = now
                    yield KEEPALIVE
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                # 没人叫醒：兜底轮询到点了，回到循环顶再扫一次
                pass
    finally:
        bus.unsubscribe(key)


__all__ = [
    "POLL_SECONDS", "KEEPALIVE_SECONDS", "KEEPALIVE", "BURST", "bus", "notify",
    "format_sse", "format_event", "stream",
]
