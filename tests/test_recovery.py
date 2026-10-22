"""自动恢复（server/recovery.py）+ 收尾的退出码判据。

两块东西在这里，因为它们是同一件事的两端：

**根因**：2026-10-22 真机上的工作流行任务 30——agent OOM 死掉（退出码 1），
它没调 ``finish``，于是「自行收口」那条路替它补了一个收口标记；而收尾分流
只看标记不看**退出码**，于是「计算进程正常退出（退出码 1）」被写进时间线，
223 个文件被打成包，这一行在结果表里是绿的——而 ``deliverables/`` 是空的。

**这两端必须一起修**：
  * :mod:`server.dispatcher` 的 ``_settle`` 看到「平台补的标记 + 非零退出」
    就该判失败（不是 packaging）；
  * :mod:`server.recovery` 对真的失败做一次评估，能自动救就自己重投，
    不能救就把原因写清楚给人看。

围栏要求双向：判据写错会把正常收口误判成失败（那是更糟的错——白烧 GPU），
所以「agent 自己报过收口 + 非零退出」必须**仍然**走 packaging。
"""
from __future__ import annotations

import json
import sys
import time

import pytest

import auth
import db
import dispatcher
import recovery
import store

WAIT = 15.0
POLL = 0.05

# 假 agent：握手一下，然后按 FAKE_AGENT_MODE 决定怎么退。
FAKE_AGENT = r'''
import json, os, sys

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

emit({"type": "event", "event": "hello",
      "task_id": os.environ.get("PROTFORGE_TASK_ID", "")})

MODE = os.environ.get("FAKE_AGENT_MODE", "echo")

if MODE == "exit_abnormal":
    sys.stderr.write("fake agent: 炸了\n")
    sys.stderr.flush()
    os._exit(3)

if MODE == "exit_clean":
    os._exit(0)

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


def wait_for(pred, timeout=WAIT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(POLL)
    return pred()


# ------------------------------------------------------------------ 夹具

@pytest.fixture
def fake_agent(tmp_path):
    p = tmp_path / "fake_agent.py"
    p.write_text(FAKE_AGENT, encoding="utf-8")
    return p


@pytest.fixture
def owner_id():
    return auth.create_user("user01", "password-123")["id"]


@pytest.fixture
def make_task(owner_id):
    def _make(title="t", brief="分析 1ubq"):
        return store.create_task(title, brief, owner_id)
    return _make


_BUILT: list = []


@pytest.fixture(autouse=True)
def _reap_agents():
    yield
    for d in list(_BUILT):
        for ap in list(d.running.values()):
            try:
                dispatcher.kill_tree(ap.proc)
                ap.proc.wait(timeout=5)
            except Exception:                             # noqa: BLE001
                try:
                    ap.proc.kill()
                except OSError:
                    pass
    _BUILT.clear()


@pytest.fixture
def disp(fake_agent, monkeypatch):
    monkeypatch.delenv("FAKE_AGENT_MODE", raising=False)
    d = dispatcher.Dispatcher(
        db,
        max_concurrency=2,
        node_exe=sys.executable,
        agent_script=str(fake_agent),
        gpu_probe=lambda: 2,
    )
    _BUILT.append(d)
    return d


@pytest.fixture
def failed_task(make_task):
    """一个失败了的任务（带 error 字段）。"""
    t = make_task()
    store.set_status(t["id"], "failed", actor="system", message="炸了")
    return store.get_task(t["id"], is_admin=True)


def _fake_verdict(monkeypatch, payload: str):
    calls = []

    def _fake(prompt, **kw):
        calls.append(prompt)
        return payload
    monkeypatch.setattr(recovery.llm, "chat_completion", _fake)
    return calls


# ================================================================ 退出码判据

def test_platform_backed_finish_marker_plus_nonzero_exit_is_a_failure(disp, make_task,
                                                                    monkeypatch):
    """平台补的收口标记 + 非零退出 = 它自己崩了 → 必须判失败。

    这就是任务 30 的现场：agent OOM 死掉（退出码 1）→ 平台补标记 →
    收尾只看标记不看退出码 → 被洗成「正常收口」。
    """
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_abnormal")
    t = make_task()
    disp.tick()
    ap = disp.running[t["id"]]
    # 平台在「进程自己结束了、没报收口」时补的标记，actor 是 system
    store.record_finished(t["id"], actor="system")
    assert wait_for(lambda: ap.proc.poll() is not None)
    disp.tick()

    task = store.get_task(t["id"], is_admin=True)
    assert task["status"] == "failed", (
        "非零退出被当成了正常收口：状态=%s error=%r"
        % (task["status"], task["error"]))
    assert "3221226505" not in task["error"]     # 不是原生崩溃，是退出码 3
    assert "退出码 3" in task["error"] or "退出码" in task["error"]


def test_agents_own_finish_marker_survives_a_nonzero_exit(disp, make_task, monkeypatch):
    """agent 自己报过收口 → 进程最后非零退出**不**判失败。

    这是反向围栏：收口判据写太狠会把正常收口误判成失败，然后自动恢复
    会白烧一次 GPU——那比误判成功还贵。
    """
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_abnormal")
    t = make_task()
    disp.tick()
    ap = disp.running[t["id"]]
    store.record_finished(t["id"], actor="agent")     # agent 自己报的
    assert wait_for(lambda: ap.proc.poll() is not None)
    disp.tick()

    task = store.get_task(t["id"], is_admin=True)
    assert task["status"] == "packaging", (
        "agent 自己报过收口却被判成失败：status=%s error=%r"
        % (task["status"], task["error"]))
    assert task["error"] == ""


def test_settled_without_finish_then_crash_is_a_failure(disp, make_task, monkeypatch):
    """完整走一遍真机路径：agent_settled（平台补标记）→ 进程非零退出。"""
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_abnormal")
    t = make_task()
    disp.tick()
    ap = disp.running[t["id"]]
    # 真机路径：agent 自己结束了、没报收口 → 平台按已收口处理，补一个 system 标记
    disp._note_agent_output(ap, {"type": "agent_settled"})
    assert store.finished_actor(t["id"]) == "system"
    assert wait_for(lambda: ap.proc.poll() is not None)
    disp.tick()

    task = store.get_task(t["id"], is_admin=True)
    assert task["status"] == "failed", (
        "自行收口 + 非零退出没有被判成失败：status=%s" % task["status"])


# ================================================================ 候选筛选

def test_a_failed_task_is_a_candidate(failed_task):
    ids = [int(t["id"]) for t in recovery.candidates()]
    assert int(failed_task["id"]) in ids


def test_admin_aborted_failures_are_not_auto_retried(make_task):
    """人把它停掉的不是故障，不该自动重跑。"""
    t = make_task()
    store.set_status(t["id"], "failed", actor="system", message="炸了")
    # 真实的中止路径：dispatcher.abort 记的是 actor=admin 的
    # 「中止：已杀掉进程树 pid=...」，error 里装的是用户自己填的原因。
    store.add_event(t["id"], "log", "admin", "中止：已杀掉进程树 pid=123")
    ids = [int(x["id"]) for x in recovery.candidates()]
    assert int(t["id"]) not in ids


def test_failures_whose_error_says_aborted_are_not_auto_retried(make_task):
    """另一条路：error 字段里直接写着中止。也要跳过。"""
    t = make_task()
    store.set_status(t["id"], "failed", actor="system", message="中止")
    store.update_task(t["id"], error="已由管理员中止")
    ids = [int(x["id"]) for x in recovery.candidates()]
    assert int(t["id"]) not in ids


def test_unrecoverable_tasks_are_not_asked_again(failed_task, monkeypatch):
    _fake_verdict(monkeypatch, json.dumps(
        {"recoverable": False, "reason": "需求本身做不了",
         "hint": "", "user_message": "这个抗原做不了"}))
    recovery.process(int(failed_task["id"]))
    ids = [int(x["id"]) for x in recovery.candidates()]
    assert int(failed_task["id"]) not in ids, "判过不可恢复的任务又被问了一遍"


def test_recovery_stops_after_the_retry_limit(failed_task, monkeypatch):
    """每个任务最多自动重投 MAX_AUTO_RETRY 次。"""
    _fake_verdict(monkeypatch, json.dumps(
        {"recoverable": True, "reason": "OOM", "hint": "分块算",
         "user_message": ""}))
    tid = int(failed_task["id"])
    for _ in range(recovery.MAX_AUTO_RETRY + 1):
        recovery.process(tid)
    st = recovery.load_state()
    assert recovery.attempts_of(st, tid) == recovery.MAX_AUTO_RETRY
    ids = [int(x["id"]) for x in recovery.candidates()]
    assert tid not in ids, "超过上限之后还在自动重投"


# ================================================================ 评估解析

@pytest.mark.parametrize("raw", [
    '{"recoverable": true, "reason": "r", "hint": "h", "user_message": "m"}',
    '```json\n{"recoverable": true, "reason": "r", "hint": "h", "user_message": "m"}\n```',
    '我的判断是：\n{"recoverable": true, "reason": "r", "hint": "h", "user_message": "m"}\n就这样。',
])
def test_verdict_parsing_handles_the_shapes_models_actually_produce(raw):
    v = recovery._parse_verdict(raw)
    assert v["recoverable"] is True
    assert v["reason"] == "r"
    assert v["hint"] == "h"


def test_unparseable_verdict_fails_closed():
    """模型没给出可解析的 JSON → **按不可恢复处理**，不拿半截话去重跑。"""
    v = recovery._parse_verdict("我觉得应该可以再试试")
    assert v["recoverable"] is False
    assert v["reason"]


# ================================================================ 执行

def test_recoverable_failure_gets_requeued_cleanly(failed_task, monkeypatch):
    _fake_verdict(monkeypatch, json.dumps(
        {"recoverable": True, "reason": "内存不够",
         "hint": "把全对全距离矩阵改成分块算", "user_message": ""}))
    tid = int(failed_task["id"])

    # 先给它一点上一轮的痕迹，确认重投时被清干净
    store.record_finished(tid, actor="system")
    store.update_task(tid, workflow_archived=1, package_path="E:/x.zip",
                      summary="上一轮的摘要", step="上一轮走到哪")

    assert recovery.process(tid) == "requeued"
    task = store.get_task(tid, is_admin=True)
    assert task["status"] == "queued"
    assert task["error"] == ""
    assert task["summary"] == ""
    assert task["step"] == ""
    assert task["package_path"] == ""
    assert task["done_at"] is None
    assert task["workflow_archived"] == 0, "归档标记没清 → 这一行永远写不回结果表"
    assert store.has_finished(tid) is False, "收口标记没清 → 第二轮 finish 会被 422 拒掉"

    # 提示留下来了，下一轮 agent 能读到
    hint = recovery.read_hint(tid)
    assert "分块算" in hint


def test_unrecoverable_failure_gets_a_readable_reason(failed_task, monkeypatch):
    _fake_verdict(monkeypatch, json.dumps(
        {"recoverable": False, "reason": "抗原序列是假的，查不到",
         "hint": "", "user_message": "UniProt 上查不到这个抗原"}))
    tid = int(failed_task["id"])

    assert recovery.process(tid) == "unrecoverable"
    task = store.get_task(tid, is_admin=True)
    assert task["status"] == "failed", "不可恢复的任务被放回队列了"
    assert "查不到" in task["summary"], "失败原因没展示出来：%r" % task["summary"]
    st = recovery.load_state()
    assert st[str(tid)]["verdict"] == "unrecoverable"


def test_a_broken_state_file_is_treated_as_empty(monkeypatch):
    """状态文件坏了不能让整个巡检停摆——当没有就行。"""
    p = recovery.state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 这不是 json", encoding="utf-8")
    assert recovery.load_state() == {}


def test_reading_a_missing_hint_is_not_an_error():
    assert recovery.read_hint(999999) == ""


def test_assess_failure_makes_the_task_failed_not_crashed(failed_task, monkeypatch):
    """评估模型调不通 → 记一笔，不重投，也不让后台线程死掉。"""

    def _boom(prompt, **kw):
        raise recovery.llm.LLMError("连不上模型端点")
    monkeypatch.setattr(recovery.llm, "chat_completion", _boom)

    assert recovery.process(int(failed_task["id"])) == "error"
    task = store.get_task(failed_task["id"], is_admin=True)
    assert task["status"] == "failed", "评估失败不该把任务改成别的状态"
    st = recovery.load_state()
    assert st[str(failed_task["id"])]["verdict"] == "error"

def test_superseded_row_tasks_are_never_offered_to_the_sweep(make_task, monkeypatch):
    """**被新一轮顶替的旧行任务不该再进恢复流程。**

    一次重投会把旧行任务留在 failed 里，由新任务接管那一行。旧任务不是
    「这一行又失败了」——判成「可恢复」并重投，等于把同一行跑第二遍：
    白烧几小时 GPU，结果还是那一个。
    """
    wf = store.create_workflow(1, "批次", "做 {x}")
    wid = int(wf["id"])
    old = store.create_task("行 12 · 第一轮", "b", 1,
                            workflow_id=wid, workflow_row=11)
    store.set_status(old["id"], "failed", actor="system", message="炸了")
    new = store.create_task("行 12 · 重跑", "b", 1,
                            workflow_id=wid, workflow_row=11)
    store.set_status(new["id"], "failed", actor="system", message="也炸了")

    ids = [int(x["id"]) for x in recovery.candidates()]
    assert int(old["id"]) not in ids, "被顶替的旧行任务又被拿出来了"
    assert int(new["id"]) in ids, "真正在失败的那一行反而没进候选"
