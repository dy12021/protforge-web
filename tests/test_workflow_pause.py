"""工作流的「暂停」到底是什么意思（2026-10-23 用户定的语义）。

用户原话：「暂停工作流开始新任务（不让工作流开始下一行，但是当前进行的不变）」。

拆成三条可验的语义，各有专门用例：

1. **暂停不杀当前在跑的那一行** —— 那是「中止」干的事。暂停只是「不再放下一行」。
2. **暂停后工作流不再放下一行** —— 这才是暂停的作用。
3. **暂停期间新任务照样能开始** —— 常规任务与工作流行是两条通道，
   暂停的是工作流，不该把用户的别的事一起停掉。

第 3 条是这套语义里最容易被顺手做坏的：把「暂停」做成「全局停一下」的话，
用户暂停一个批次就会发现自己新交的任务也起不来——那不是暂停，是停机。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for _d in (REPO / "server",):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import auth          # noqa: E402
import db            # noqa: E402
import dispatcher    # noqa: E402
import store         # noqa: E402

PWD = "password-123"


class _FakeProc:
    """假的 agent 进程：只占位，不真的起进程。"""

    def __init__(self, task_id):
        self.task_id = task_id
        self.aborted = False
        self.proc = self
        self.stdin = None
        self.stdout = None

    def poll(self):
        return None


@pytest.fixture()
def user():
    return int(auth.create_user("pauser", PWD, role="user")["id"])


@pytest.fixture()
def disp(monkeypatch):
    """不真起 agent 的调度器（把 _spawn 换成记账）。"""
    monkeypatch.setenv("PROTFORGE_WEB_NODE", "node")
    d = dispatcher.Dispatcher(db, max_concurrency=4, node_exe="node",
                              gpu_probe=lambda: 4)
    spawned: list[int] = []

    def fake_spawn(task_id, brief, work_dir):
        spawned.append(int(task_id))
        d._running[int(task_id)] = _FakeProc(task_id)
        return _FakeProc(task_id)

    monkeypatch.setattr(d, "_spawn", fake_spawn)
    monkeypatch.setattr(d, "_mark_failed",
                        lambda task_id, msg: store.set_status(
                            task_id, "failed", actor="system", message=msg))
    d._spawned = spawned
    yield d
    d._archive_stop()


def _workflow(owner_id, tmp_path, rows=3, name="wf"):
    """建一个带真实 xlsx 源表的工作流（放行时能真的渲染出 brief）。"""
    import workflow as wfmod
    from openpyxl import Workbook
    root = Path(tmp_path) / "data" / "workflows"
    st = wfmod.staging_path(root)
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "序列"])
    for i in range(rows):
        ws.append([f"ID{i}", f"SEQ{i}"])
    wb.save(st)
    source = wfmod.finalize_upload(root, st, 9201)
    wf = store.create_workflow(owner_id, name, "查 {{seq}}",
                               sheet="S1", source_path=str(source),
                               header_row=1, key_col=0, total_rows=rows,
                               writeback=1)
    store.set_workflow_columns(int(wf["id"]), [
        {"col_index": 0, "header": "编号", "role": "key"},
        {"col_index": 1, "header": "序列", "role": "input", "slot": "seq"},
    ])
    store.update_workflow(int(wf["id"]), status="running")
    return store.get_workflow(int(wf["id"]), is_admin=True)


# ---------------------------------------------------------------- 三条语义


def test_pause_does_not_kill_the_running_row(disp, user, tmp_path):
    """**暂停不杀当前在跑的那一行**——那是「中止」干的事。

    暂停只是「不再放下一行」。当前行该跑完还是跑完：中途杀掉它，
    等于把「暂停」做成了「中止」，而这两个在界面上是**两个不同的按钮**。
    """
    wf = _workflow(user, tmp_path)
    disp._workflow_run_pass()
    row = store.list_workflow_rows(int(wf["id"]))[0]
    assert row["status"] == "running", "第一行应该已经放出去跑着"

    # 暂停 = 只改工作流状态，不碰行任务
    store.update_workflow(int(wf["id"]), status="paused")

    again = store.get_task(int(row["id"]), is_admin=True)
    assert again["status"] == "running", "暂停把在跑的那一行弄没了"
    assert not again.get("error"), "暂停不该写失败原因"


def test_pause_stops_the_next_row(disp, user, tmp_path):
    """暂停后**不再放下一行**。这才是暂停的作用。"""
    wf = _workflow(user, tmp_path)
    disp._workflow_run_pass()
    assert len(disp._spawned) == 1

    store.update_workflow(int(wf["id"]), status="paused")
    disp._workflow_run_pass()
    assert len(disp._spawned) == 1, "暂停之后还放行了下一行"


def test_resume_lets_the_next_row_go(disp, user, tmp_path):
    """继续之后，下一行放得出来（否则「暂停」就是单向门了）。

    **当前行要先收工**：工作流是串行的，当前行没跑完下一行本来就放不出来
    （闸门⑤：有非终态行就不放下一行）。所以这里先把第一行置成终态并归档，
    再继续——这才是「当前行跑完了，继续放下一行」的真实场景。
    """
    wf = _workflow(user, tmp_path)
    disp._workflow_run_pass()
    first = store.list_workflow_rows(int(wf["id"]))[0]
    assert len(disp._spawned) == 1

    store.update_workflow(int(wf["id"]), status="paused")
    disp._workflow_run_pass()
    assert len(disp._spawned) == 1

    # 当前行跑完了（并已归档）
    store.set_status(int(first["id"]), "done", actor="system", message="ok")
    store.update_task(int(first["id"]), workflow_archived=1)

    store.update_workflow(int(wf["id"]), status="running")
    disp._workflow_run_pass()
    assert len(disp._spawned) == 2, "继续之后下一行没放出来"


def test_a_new_task_starts_while_the_workflow_is_paused(disp, user, tmp_path):
    """**暂停工作流期间，新任务照样能开始。**

    常规任务与工作流行是两条通道（调度器先接常规任务，再放工作流行），
    所以暂停一个批次**不该**把用户别的事一起停掉。

    这条最容易在重构时被顺手做坏：比如把「暂停」做成「全局停一下」，
    或者把闸门挪到常规任务那一侧。用户暂停一个 61 行的批次，
    结果自己新交的任务也起不来——那不是暂停，是停机。
    """
    wf = _workflow(user, tmp_path)
    disp._workflow_run_pass()
    assert len(disp._spawned) == 1

    store.update_workflow(int(wf["id"]), status="paused")

    # 用户在暂停期间交了个新任务
    t = store.create_task("暂停期间交的新任务", "算一下", owner_id=user,
                          actor="human")
    disp.tick()

    assert int(t["id"]) in disp._spawned, \
        "工作流暂停着，新任务却没被接走——暂停做成了全局停机"
    # 工作流那一行仍然没放出去
    assert len(disp._spawned) == 2, "暂停期间不该放工作流的下一行"


def test_pause_then_resume_keeps_the_new_task_running(disp, user, tmp_path):
    """暂停→交新任务→继续：新任务不受影响，工作流接着往下走。"""
    wf = _workflow(user, tmp_path)
    disp._workflow_run_pass()

    store.update_workflow(int(wf["id"]), status="paused")
    t = store.create_task("新任务", "算一下", owner_id=user, actor="human")
    disp.tick()
    assert int(t["id"]) in disp._spawned

    store.update_workflow(int(wf["id"]), status="running")
    disp.tick()
    # 工作流的下一行要等当前行收工（串行），所以这里断言的是
    # 「新任务不受影响」，不是「下一行立刻放出来」。
    assert store.get_task(int(t["id"]), is_admin=True)["status"] == "running", \
        "继续工作流不该把暂停期间起的新任务也带走"
    assert len(disp._spawned) == 2, "工作流的下一行不该在当前行还在跑时放出来"
