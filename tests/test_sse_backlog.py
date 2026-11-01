"""Backlog draining must not wait between full batches."""
import asyncio

import pytest
import sse


def test_backlog_drains_before_poll_timeout_and_releases_subscription(monkeypatch):
    monkeypatch.setattr(sse, "BURST", 2)
    monkeypatch.setattr(sse, "POLL_SECONDS", 60)
    queries = []

    def events(task_id, after_id, limit):
        queries.append(after_id)
        return [{"id": i, "kind": "log"}
                for i in range(after_id + 1, min(5, after_id + limit) + 1)]

    monkeypatch.setattr(sse.store, "list_events", events)

    class Request:
        async def is_disconnected(self):
            return False

    async def run():
        before = sse.bus.waiter_count()
        stream = sse.stream(1, Request(), last_seen=1)
        try:
            frames = [await asyncio.wait_for(anext(stream), .5) for _ in range(4)]
            assert [int(frame.splitlines()[0][4:]) for frame in frames] == [2, 3, 4, 5]
            assert queries == [1, 3]
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(stream), .05)
        finally:
            await stream.aclose()
        assert sse.bus.waiter_count() == before

    asyncio.run(run())


def test_notification_during_frame_delivery_is_not_lost(monkeypatch):
    monkeypatch.setattr(sse, "POLL_SECONDS", 60)
    rows = [{"id": 1, "kind": "log"}]
    monkeypatch.setattr(sse.store, "list_events", lambda task_id, after_id, limit:
                        [row for row in rows if row["id"] > after_id][:limit])

    class Request:
        async def is_disconnected(self):
            return False

    async def run():
        before = sse.bus.waiter_count()
        stream = sse.stream(1, Request())
        try:
            await anext(stream)
            rows.append({"id": 2, "kind": "log"})
            sse.notify()
            await asyncio.sleep(0)  # Dispatch the thread-safe wake callback during yield.
            frame = await asyncio.wait_for(anext(stream), .5)
            assert frame.startswith("id: 2\n")
        finally:
            await stream.aclose()
        assert sse.bus.waiter_count() == before

    asyncio.run(run())
