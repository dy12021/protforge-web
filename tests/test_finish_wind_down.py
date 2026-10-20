"""agent 报完 finish 之后，任务必须自己走到终态——这条围栏钉的是一条**死锁**。

死锁长什么样
------------
收尾（``Dispatcher._settle``）只在**进程退出**时触发。而 ``finish`` 只是模型调的
一个工具：它 POST 完就返回给模型，进程继续挂在 ``runRpcMode`` 上等 stdin。串起来是::

    agent 调 finish → 标记落库 → 没人关 stdin → 进程不退 → 收尾不跑
    → 状态永远停在「计算中」→ 不进打包 → **用户永远下载不到任何东西**

每一环单独看都对，所以后端测试、前端测试、静态资源检查全绿——它们没有一个会
真的把一个「报完 finish 却还在跑」的进程放在调度器里，然后问它任务后来怎么了。
真机跑一次才现形：任务永远停在「计算中」，产物 0 个，下载恒 409。

所以这里起**真的子进程**，让它精确演出「报完收口但不退出」，
再断言调度器把它收掉、任务进 packaging。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import auth
import dispatcher
import store

#: 假 agent 的两种收尾脾气。
#:
#: - polite  —— stdin 一关就退出 0。这是**真 agent 该有的行为**（runRpcMode 读到 EOF
#:             就返回，main() 结束，node 正常退出）。用来验优雅路径。
#: - stubborn —— 收到 finish 信号也不退出，一直挂着。这是死锁的真正形态：
#:             它证明宽限期到了之后调度器会**杀进程树**，而不是陪着它一起卡住。
FAKE_AGENT = r'''
import json, os, sys, time

MODE = os.environ.get("FAKE_AGENT_MODE", "stubborn")


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


emit({"type": "event", "event": "hello",
      "task_id": os.environ.get("PROTFORGE_TASK_ID", "")})

if MODE == "settled":
    # **模型答完话就自己结束，没有调 finish。** 真机上最常见的一种收口缺失：
    # stdout 里只有一段 assistant 文本 + agent_settled，一个工具调用都没有。
    # 然后它像真 agent 一样挂在 runRpcMode 上等 stdin，永远不退出。
    emit({"type": "message_end", "message": {"role": "assistant",
          "content": [{"type": "text", "text": "算完了：1+1=2"}]}})
    emit({"type": "agent_settled"})

if MODE == "stubborn" or MODE == "settled":
    # 报完收口也不退：把 stdin 读干净，然后永远挂着。
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        if cmd.get("id"):
            emit({"type": "response", "id": cmd["id"], "ok": True, "result": {}})
    while True:
        time.sleep(0.2)

# polite：stdin 一关就退出。
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        cmd = json.loads(line)
    except ValueError:
        continue
    emit({"type": "response", "id": cmd.get("id"), "ok": True, "result": {}})
'''

WAIT = 30.0
POLL = 0.05
_BUILT: list = []


def wait_for(pred, timeout=WAIT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(POLL)
    return bool(pred())


@pytest.fixture
def fake_agent(tmp_path) -> Path:
    p = tmp_path / "winddown_agent.py"
    p.write_text(FAKE_AGENT, encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _reap_agents():
    yield
    # 先等收口线程自己收完。收口跑在后台线程里，它还要写库；测试一结束、
    # conftest 把临时库指到下一个用例的库上，那个线程再写就会撞到外键失败——
    # 报出来的是「事件类型或来源不合法」，跟真因（任务已经不在库里了）毫不相干。
    deadline = time.time() + 15.0
    for d in _BUILT:
        while any(not ap.settled for ap in d.running.values()) and time.time() < deadline:
            time.sleep(0.05)
    for d in list(_BUILT):
        for ap in list(d.running.values()):
            try:
                dispatcher.kill_tree(ap.proc)
                ap.proc.wait(timeout=5)
            except Exception:                          # noqa: BLE001 - 收尸失败不该让测试红
                try:
                    ap.proc.kill()
                except OSError:
                    pass
    _BUILT.clear()


@pytest.fixture
def disp(fake_agent, monkeypatch):
    monkeypatch.delenv("FAKE_AGENT_MODE", raising=False)
    d = dispatcher.Dispatcher(
        max_concurrency=2,
        node_exe=sys.executable,
        agent_script=str(fake_agent),
        gpu_probe=lambda: 2,
    )
    d.poll_interval = 0.05          # 别让测试干等一整秒
    _BUILT.append(d)
    return d


@pytest.fixture
def owner_id():
    return auth.create_user("winddown", "password-123")["id"]


def _running_task(disp, owner_id, monkeypatch, mode) -> tuple[dict, object]:
    """起一个**真的**在跑、且已经报过 finish 的任务。"""
    monkeypatch.setenv("FAKE_AGENT_MODE", mode)
    t = store.create_task("收口验证", "验证 finish 之后进程会被收掉", owner_id)
    disp.tick()                                   # 接单，spawn 子进程
    ap = disp.running[t["id"]]
    assert ap.proc.poll() is None, "假 agent 没起来"

    store.set_status(t["id"], "running", actor="system", message="计算进程已启动")
    store.record_finished(t["id"], actor="agent")  # agent 报完收口
    return t, ap


# --- 死锁本体 -------------------------------------------------------------------------


def test_finish_without_wind_down_leaves_the_task_stuck(disp, owner_id, monkeypatch):
    """先把缺陷本身钉住：只落 finished 标记、不收口，任务会永远停在 running。

    这条是**对照**，证明后面的用例验的确实是 wind_down 带来的改变，
    而不是"无论如何都会收口"。
    """
    monkeypatch.setenv("FAKE_AGENT_MODE", "stubborn")
    t = store.create_task("对照", "只落标记不收口", owner_id)
    disp.tick()
    ap = disp.running[t["id"]]
    store.set_status(t["id"], "running", actor="system", message="计算进程已启动")
    store.record_finished(t["id"], actor="agent")

    # 反复 tick（模拟调度器正常轮询）也不会推进——收尾只在进程退出时触发。
    for _ in range(20):
        disp.tick()
        time.sleep(0.05)

    assert ap.proc.poll() is None, "对照组的进程不该自己退出"
    assert store.get_task(t["id"])["status"] == "running", (
        "对照组跑完 20 轮还是 running，说明缺了收口这一步")


def test_wind_down_stops_a_stubborn_agent_and_moves_to_packaging(
        disp, owner_id, monkeypatch):
    """报完 finish 却赖着不走的进程，宽限期到了必须被杀，并且按正常收口进 packaging。"""
    t, ap = _running_task(disp, owner_id, monkeypatch, "stubborn")
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 0.5)   # 别让测试等满 10 秒

    assert disp.wind_down(t["id"]) is True

    assert wait_for(lambda: ap.proc.poll() is not None), "宽限期过了进程还活着"
    assert wait_for(lambda: store.get_task(t["id"])["status"] == "packaging"), (
        f"状态没进 packaging，实际 {store.get_task(t['id'])['status']}")


def test_wind_down_lets_a_polite_agent_exit_on_its_own(
        disp, owner_id, monkeypatch):
    """stdin 一关就退的 agent：走优雅路径，不该被强杀。"""
    t, ap = _running_task(disp, owner_id, monkeypatch, "polite")
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 10.0)

    assert disp.wind_down(t["id"]) is True

    assert wait_for(lambda: ap.proc.poll() is not None), "关 stdin 之后它没退出"
    assert ap.proc.returncode == 0, f"不该是被强杀的，退出码 {ap.proc.returncode}"
    assert wait_for(lambda: store.get_task(t["id"])["status"] == "packaging")


def test_wind_down_is_not_recorded_as_a_user_abort(disp, owner_id, monkeypatch):
    """收口**不能**写成中止。

    一次正常收口被解释成"用户中止了任务"，用户在详情页看到的是一条不存在的事故——
    这正是 _settle 里那四条互斥分支要防的事，收口这条路也得守住。
    """
    t, ap = _running_task(disp, owner_id, monkeypatch, "stubborn")
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 0.5)

    disp.wind_down(t["id"])
    assert wait_for(lambda: store.get_task(t["id"])["status"] == "packaging")

    assert ap.aborted is False, "收口把进程标成中止了"
    task = store.get_task(t["id"])
    assert not (task.get("error") or "").strip(), f"收口不该写失败原因：{task.get('error')}"
    kinds = [(e["kind"], e["to_status"]) for e in store.list_events(t["id"])]
    assert ("status", "packaging") in kinds, kinds
    assert ("status", "failed") not in kinds, kinds


def test_wind_down_is_a_noop_for_unknown_and_finished_tasks(disp, owner_id, monkeypatch):
    """不该对不存在的任务动手，也不该重复收尾。"""
    assert disp.wind_down(999999) is False

    t, ap = _running_task(disp, owner_id, monkeypatch, "polite")
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 5.0)
    disp.wind_down(t["id"])
    assert wait_for(lambda: ap.settled)
    first = len(store.list_events(t["id"]))
    assert disp.wind_down(t["id"]) is False, "已收尾的任务不该再收一次"
    assert len(store.list_events(t["id"])) == first


def test_wind_down_does_not_block_the_caller(disp, owner_id, monkeypatch):
    """/finish 的调用方就是 agent 自己，不能让它堵在请求里等自己退出。"""
    t, ap = _running_task(disp, owner_id, monkeypatch, "stubborn")
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 5.0)

    t0 = time.time()
    disp.wind_down(t["id"])
    elapsed = time.time() - t0

    assert elapsed < 1.0, f"wind_down 同步等了 {elapsed:.1f}s，agent 的请求会卡在这里"
    assert ap.proc.poll() is None, "返回时进程应该还活着，收口在后台线程里做"
    assert wait_for(lambda: ap.proc.poll() is not None)


# --- 端点接线 -------------------------------------------------------------------------


def test_internal_finish_endpoint_triggers_the_wind_down(tmp_path, owner_id, monkeypatch):
    """POST /api/internal/agent/{id}/finish 必须真的收口，否则死锁照旧。

    ``wind_down`` 存在还不够：内部端点不调它，缺陷原封不动。
    """
    from fastapi.testclient import TestClient

    import app as svc

    monkeypatch.setenv("FAKE_AGENT_MODE", "stubborn")
    d = dispatcher.Dispatcher(
        max_concurrency=2, node_exe=sys.executable,
        agent_script=str(tmp_path / "winddown_agent.py"),
        gpu_probe=lambda: 2,
    )
    d.poll_interval = 0.05
    (tmp_path / "winddown_agent.py").write_text(FAKE_AGENT, encoding="utf-8")
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 0.5)
    _BUILT.append(d)

    t = store.create_task("端点接线", "finish 端点必须收口", owner_id)
    d.tick()
    ap = d.running[t["id"]]
    token = ap.token
    store.set_status(t["id"], "running", actor="system", message="计算进程已启动")

    internal = svc.create_internal_app()
    internal.state.dispatcher = d
    with TestClient(internal) as c:
        r = c.post(f"/api/internal/agent/{t['id']}/finish",
                   json={"summary": "算完了", "artifacts": ["a.py"]},
                   headers={"X-Agent-Token": token})
    assert r.status_code == 200, r.text

    assert wait_for(lambda: ap.proc.poll() is not None), "finish 之后进程还活着"
    assert wait_for(lambda: store.get_task(t["id"])["status"] == "packaging"), (
        f"finish 端点没触发收口，状态是 {store.get_task(t['id'])['status']}")


def test_finish_endpoint_still_works_without_a_dispatcher(owner_id, monkeypatch):
    """没起 worker 时（测试、只读部署）finish 不能因为取不到调度器就 500。"""
    from fastapi.testclient import TestClient

    import app as svc

    internal = svc.create_internal_app()
    t = store.create_task("没有调度器", "只是别 500", owner_id)
    store.set_status(t["id"], "running", actor="system", message="计算进程已启动")
    token = dispatcher._TOKENS.issue(t["id"])
    try:
        with TestClient(internal) as c:
            r = c.post(f"/api/internal/agent/{t['id']}/finish",
                       json={"summary": "s"}, headers={"X-Agent-Token": token})
        assert r.status_code == 200, r.text
        assert store.has_finished(t["id"])
    finally:
        dispatcher._TOKENS.revoke(t["id"])


def test_the_wind_down_grace_is_short_enough_to_be_useful():
    """宽限期是个真实的产品参数：太长用户多等，太短会砍掉 agent 收尾的机会。"""
    assert 0 < dispatcher.WIND_DOWN_GRACE <= 30.0, (
        f"宽限期 {dispatcher.WIND_DOWN_GRACE}s 不合理："
        "太长则任务在收口后长时间停在「计算中」，太短会砍掉 agent 写最后一段的机会")


# --- agent 自己结束、却没调 finish -------------------------------------------------------
#
# 这是真机上抓到的第二条卡死：模型答完话就停（stdout 里只有一段 assistant
# 文本 + agent_settled，一个工具调用都没有），于是
#
#   * 进程还挂在 runRpcMode 上等 stdin → 任务永远停在「计算中」（技能超时 4 小时）；
#   * 管理员干预不了：steer / follow_up 只进队列——那一轮已经结束，pi 不会再
#     发起下一次模型调用，队列里的消息没人取（只有 abort 有用）。
#
# 也就是说「模型忘了调 finish」会把一个任务变成既跑不完又管不了的僵尸。


def _settled_task(disp, owner_id, monkeypatch) -> tuple[dict, object]:
    """起一个**真的**在跑、而且已经自己结束（没调 finish）的任务。"""
    monkeypatch.setenv("FAKE_AGENT_MODE", "settled")
    t = store.create_task("自行收口", "模型答完就停了", owner_id)
    disp.tick()
    ap = disp.running[t["id"]]
    assert ap.proc.poll() is None, "假 agent 没起来"
    store.set_status(t["id"], "running", actor="system", message="计算进程已启动")
    return t, ap


def test_an_agent_that_settles_without_finish_still_closes_the_task(
        disp, owner_id, monkeypatch):
    """**没调 finish 也必须收口**，而且要收成「已收口」而不是「卡住」。

    平台不能把收口整件事押在模型记不记得调工具上：模型忘了调，任务就不该
    永远停在「计算中」，更不该到 4 小时后才被了结。
    """
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 0.5)
    t, ap = _settled_task(disp, owner_id, monkeypatch)

    assert wait_for(lambda: store.has_finished(t["id"])), (
        "agent 自己结束了却没人收口：任务会永远停在「计算中」")
    assert wait_for(lambda: ap.proc.poll() is not None), (
        "收口之后进程还活着")
    assert wait_for(lambda: store.get_task(t["id"])["status"] == "packaging"), (
        f"状态没进 packaging，实际 {store.get_task(t['id'])['status']}")


def test_the_models_own_words_become_the_summary_when_it_never_called_finish(
        disp, owner_id, monkeypatch):
    """**模型答的那句话要留下来。**

    没调 finish 时，模型的回复就是这一轮唯一的结论。不把它当摘要，这条任务
    就只剩一句「没报收口」——用户连它到底答了什么都不知道，而那可能正是
    他要的东西。
    """
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 0.5)
    t, _ap = _settled_task(disp, owner_id, monkeypatch)

    assert wait_for(lambda: store.has_finished(t["id"]))
    summary = (store.get_task(t["id"]).get("summary") or "").strip()
    assert summary == "算完了：1+1=2", f"模型自己说的话没被留下：{summary!r}"

    events = [(e["kind"], e["message"]) for e in store.list_events(t["id"])]
    assert any(k == "note" and "没有报收口" in m for k, m in events), \
        f"时间线上没说清它是怎么结束的：{events}"


def test_a_task_that_already_finished_is_not_touched_by_the_settle(
        disp, owner_id, monkeypatch):
    """**已经报过收口的（正常路径）什么都不做。**

    正常收口时 agent 调 finish、标记落库，之后进程退出也会带来 agent_settled。
    那时候平台再收一次，就是把一次正常收口说成「模型自己结束的」。
    """
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 0.5)
    t = store.create_task("正常收口", "先 finish 再 settled", owner_id)
    disp.tick()
    ap = disp.running[t["id"]]
    store.set_status(t["id"], "running", actor="system", message="计算进程已启动")
    store.update_task(t["id"], summary="agent 自己报的收口")
    store.record_finished(t["id"], actor="agent")

    # 让它也发出 agent_settled：真实路径里 finish 之后进程退出前就是这一串。
    disp._note_agent_output(ap, {"type": "agent_settled"})

    assert (store.get_task(t["id"]).get("summary") or "") == "agent 自己报的收口", \
        "正常收口的摘要被 agent_settled 那条路改掉了"
    kinds = [e["kind"] for e in store.list_events(t["id"])]
    assert kinds.count("finished") == 1, kinds
    assert not any(e["kind"] == "note" and "没有报收口" in e["message"]
                   for e in store.list_events(t["id"])), \
        "正常收口被说成「模型自己结束的」"


def test_an_aborted_task_is_not_closed_by_the_settle(disp, owner_id, monkeypatch):
    """**中止过的任务不归这条路径管。**

    中止的结局由中止那条路决定；这里再收一次，就是把「用户中止了任务」
    改写成「平台按已收口处理」。
    """
    monkeypatch.setattr(dispatcher, "WIND_DOWN_GRACE", 0.5)
    t, ap = _settled_task(disp, owner_id, monkeypatch)
    ap.aborted = True
    ap.abort_reason = "用户中止了任务"

    time.sleep(0.5)
    assert not store.has_finished(t["id"]), \
        "被中止的任务被这条路径收成了「已收口」"


if os.name != "nt":
    pytest.skip("杀进程树按 taskkill /T 实现，只在 Windows 上验", allow_module_level=False)
