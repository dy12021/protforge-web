"""``store.set_status`` 的原子性（缺陷 B：读 prev 与写库之间有窗口，中止会被吞掉）。

钉死的回归：旧实现是「连库读 status → 内存判断 → ``add_event`` → ``_update`` 再连库
写」，读与写之间没有任何保护。用户中止（另一个进程把状态写成 failed）正好落在窗口里
时，紧跟着的 ``set_status(..., "running")`` 会把它无声覆盖——任务等于没被中止，白烧
一轮 GPU，还交给用户一个"正在运行"。同源的第二处是 ``done_at``：prev 是旧值时"第一次
进终态"判错，多盖一次时间戳。

**中止落点怎么钉死**：靠线程抢时间是没用的，那个窗口只有几微秒。所以 ``_abort_in_window``
在**连接层**做文章——代理 ``db.connect``，谁在测试期间第一个执行写语句，就先用一个
干净连接把状态写成 failed（模拟另一个进程里用户点了中止），再让它照常执行。这样
"读 prev 之后、写入之前"这件事是被构造出来的，不是撞运气撞出来的。

其余用例走真并发：多条连接（``db.connect`` 每次都是新连接，与多进程等价）同时改同一个
任务，断言事件链自洽、终态不被回退、done_at 不被重盖。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import db  # noqa: E402
import store  # noqa: E402

# 并发用例的线程数与轮数。给到 8 次 CAS 重试的余量，又不至于把测试拖慢。
_THREADS = 6
_ROUNDS = 2
# 终态竞态用例里，狂刷线程每轮之间的间隔（毫秒）。
_HAMMER_SLEEP = 0.005
_HAMMER_ROUNDS = 30


@pytest.fixture
def task() -> dict:
    return store.create_task("设计一个结合蛋白", "要能表达的小分子结合蛋白", owner_id=1)


def _status_events(tid: int) -> list[dict]:
    return [e for e in store.list_events(tid) if e["kind"] == "status"]


def _assert_chain(events: list[dict], first: str) -> None:
    """状态事件的 from/to 必须首尾相接。

    这是「读改写」里那个读失真留下的最好证据：并发调用各自读到的 prev 都可能是
    同一个旧值，于是时间线上会出现 queued→running 之后又来一条 queued→packaging。
    """
    expect = first
    for e in events:
        assert e["from_status"] == expect, f"事件链断了：{e}，期望 from={expect}"
        expect = e["to_status"]


@pytest.fixture
def _abort_in_window(monkeypatch):
    """让"另一个进程的用户中止"精确落在本连接第一次写库之前。

    返回的 state 里 ``tid`` 设上之后才生效，``fired`` 用来断言这一枪真的开火了
    （否则这条用例会在什么都没发生的情况下"通过"，变成恒真）。
    """
    real_connect = db.connect
    state = {"tid": None, "fired": False, "done_at": None}

    class _Proxy:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kw):
            head = str(sql).lstrip()[:6].upper()
            if (not state["fired"] and state["tid"] is not None
                    and head.startswith(("INSERT", "UPDATE", "DELETE", "REPL"))):
                state["fired"] = True
                # 用干净的连接扮演另一个进程：它既不会被代理递归，也不受本事务影响。
                c2 = real_connect()
                try:
                    with c2:
                        c2.execute(
                            """UPDATE tasks SET status = 'failed', error = ?,
                                     done_at = ? WHERE id = ?""",
                            ("用户中止了任务", state["done_at"] or db.now_iso(),
                             int(state["tid"])))
                finally:
                    c2.close()
            return self._real.execute(sql, *args, **kw)

        def __getattr__(self, name):        # commit / close / row_factory 等照旧转发
            return getattr(self._real, name)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

    monkeypatch.setattr(db, "connect", lambda: _Proxy(real_connect()))
    return state


# ---------------------------------------------------------------- 核心围栏


def test_abort_landing_in_the_window_is_not_swallowed(task, _abort_in_window):
    """中止正好落在「读完 prev、还没写」之间时，不能被 running 覆盖掉。

    这条是缺陷 B 的核心回归护栏：调度器/打包器在另一个连接上把状态往回推时，
    库里必须仍是 failed（中止），而且返回值与事件都要如实反映中止，不能谎称
    "正在运行"。
    """
    tid = task["id"]
    _abort_in_window["tid"] = tid

    out = store.set_status(tid, "running", actor="system", message="计算进程已启动")

    assert _abort_in_window["fired"], "中止没落在窗口里，这条用例等于没测"
    assert store.get_task(tid, is_admin=True)["status"] == "failed", "中止被覆盖了"
    assert out["status"] == "failed", "返回值还报 running：调用方会以为它真跑上了"
    # 时间线上不许出现"中止之后又被改成 running"的状态事件
    assert not [e for e in _status_events(tid) if e["to_status"] == "running"]
    # 中止那条 log 必须留下来，否则用户界面上就是"点了没反应"
    assert any(e["kind"] == "log" for e in store.list_events(tid))


def test_done_at_is_not_restamped_when_a_terminal_race_is_lost(task, _abort_in_window):
    """终态竞态里输的那一方不许重盖 done_at。

    ``done_at`` 的语义是"**第一次**进终态的时刻"。并发两个终态落库时，prev 读到旧
    值的那个会把时间戳盖掉重算，于是"完成于"显示的是一个中途时刻。
    """
    tid = task["id"]
    store.claim_queued(tid)                       # queued -> running
    _abort_in_window["done_at"] = "2000-01-01T00:00:00.000"
    _abort_in_window["tid"] = tid

    store.set_status(tid, "done", actor="system", message="算完了")

    assert _abort_in_window["fired"], "中止没落在窗口里，这条用例等于没测"
    assert store.get_task(tid, is_admin=True)["done_at"] == "2000-01-01T00:00:00.000", \
        "done_at 被第二次进终态重盖了"


# ---------------------------------------------------------------- 真并发


def test_concurrent_set_status_has_no_lost_update(task):
    """多连接同时改同一个任务：每次成功改写都要留下一条事件，事件链自洽。

    防的是丢更新：读改写窗口里后写的那一方会拿着同一个 prev 覆盖，谁最后写谁赢，
    落库状态与事件记录对不上——前端时间线会显示一段根本没发生过的流转。
    """
    tid = task["id"]
    targets = ["running", "packaging"] * _THREADS
    errors: list[BaseException] = []
    barrier = threading.Barrier(_THREADS)

    def _worker(idx: int) -> None:
        try:
            for r in range(_ROUNDS):
                barrier.wait(timeout=20)
                store.set_status(tid, targets[idx], actor="system",
                                 message=f"线程{idx}第{r}轮")
        except BaseException as e:              # noqa: BLE001 —— 传回主线程再断言
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(i,), daemon=True)
               for i in range(_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"并发调用抛错了：{errors!r}"
    events = _status_events(tid)
    assert len(events) == _THREADS * _ROUNDS, "有改写没有留下事件（丢更新）"
    _assert_chain(events, "queued")
    final = store.get_task(tid, is_admin=True)
    assert final["status"] == events[-1]["to_status"], "最终状态与事件记录不一致"


def test_terminal_task_is_never_pushed_back_under_load(task):
    """狂刷 set_status(running) 的线程中，中途落库的中止必须活下来。

    防的是"运行中盖掉失败"这种回退：调度器/打包器的进度回写是无条件覆盖，
    中止之后再来一次就等于撤销了用户的操作。
    """
    tid = task["id"]
    errors: list[BaseException] = []
    stop = threading.Event()

    def _hammer(idx: int) -> None:
        try:
            while not stop.is_set():
                store.set_status(tid, "running", actor="system", message=f"线程{idx}回写")
                time.sleep(_HAMMER_SLEEP)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=_hammer, args=(i,), daemon=True)
               for i in range(4)]
    for t in threads:
        t.start()
    try:
        time.sleep(0.15)                         # 让它们先刷几轮
        before = len(_status_events(tid))
        assert before > 0, "回写线程根本没写成功，竞态没被构造出来"
        store.set_status(tid, "failed", actor="human", message="用户中止了任务")
        abort_at = _status_events(tid)[-1]["id"]
        time.sleep(0.15)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)

    assert not errors, f"并发调用抛错了：{errors!r}"
    assert store.get_task(tid, is_admin=True)["status"] == "failed", "中止被回退成了运行中"
    after = [e for e in _status_events(tid) if e["id"] > abort_at]
    assert not [e for e in after if e["to_status"] == "running"], \
        f"中止之后还有状态回写：{after!r}"


# ---------------------------------------------------------------- 边界不被误伤


def test_admin_override_of_a_terminal_task_still_works(task):
    """一次干净的管理员改写（终态 → 非终态）必须照旧生效。

    这是本次收紧的**边界**：只有 system/agent 的自动回写不许把终态推回非终态，
    人与管理员的显式动作（改状态、重跑、重新打包）不该被拦——否则 test_wiring 里
    那些重跑/重打包用例会被误伤。
    """
    tid = task["id"]
    store.set_status(tid, "failed", actor="system", message="产物未确认")

    out = store.set_status(tid, "packaging", actor="admin", message="重新打包")

    assert out["status"] == "packaging"
    _assert_chain(_status_events(tid), "queued")


def test_system_write_cannot_leave_a_terminal_state(task):
    """system/agent 的自动回写不许把终态推回非终态——哪怕没有任何并发。

    这是中止不被吞的**兜底**：光靠 CAS 只能挡住"恰好卡在窗口里"的那一发，中止完成
    之后再来一次普通回写照样能翻回去。调度器收口推 packaging、打包器收口推 done 之后
    混进来的进度回写就属于这种，必须在这里挡住。
    """
    tid = task["id"]
    store.set_status(tid, "failed", actor="human", message="用户中止了任务")

    out = store.set_status(tid, "running", actor="agent", message="计算进程已收口")

    assert out["status"] == "failed", "自动回写把中止覆盖了"
    assert store.get_task(tid, is_admin=True)["status"] == "failed"
    assert any(e["kind"] == "log" for e in store.list_events(tid)), \
        "不改写也要留痕，否则界面上看不出这次回写被挡了"


def test_set_status_signature_and_return_type_unchanged(task):
    """返回类型仍是一行 dict（含 status/done_at），签名未变——别的模块在调它。"""
    tid = task["id"]
    out = store.set_status(tid, "running", actor="system", message="接走了")

    assert isinstance(out, dict)
    assert out["id"] == tid and out["status"] == "running"
    assert out["done_at"] is None, "非终态不该有完成时间"
    assert store.set_status(tid, "done", actor="system", message="收口")["done_at"], \
        "进终态必须写 done_at"


def test_unknown_task_and_status_still_rejected(task):
    """校验语义没被这次改动带跑：未知状态抛 Invalid，不存在的任务抛 NotFound。"""
    with pytest.raises(store.Invalid):
        store.set_status(task["id"], "paused")
    with pytest.raises(store.NotFound):
        store.set_status(9999, "running")
