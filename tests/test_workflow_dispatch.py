"""放行与归档（规格 6.2 / 6.3 / 6.4 / 7.2）。

本任务钉的是**顺序**与**谓词**，两样都是「写对了也不报错、写错了静默卡死」
的东西：

- 闸门顺序必须是 ①④⑤②③⑥。④ 排在②前面是硬要求——重跑的行同时满足
  ②和④，②在前就把它挡死，而④永远走不到。
- ②的判据必须是 ``workflow_archived = 0``。写成 ``workflow_row < cursor``
  会把所有历史行算成没归档，整批永不放行，且只在用过重跑之后才发作。
- 归档要等打包结束（终态），不是等进程退出。
- **归档失败时「不置位」是可接受的，「置位」不可接受**：置位之后待归档行的取数
  （带 ``AND workflow_archived = 0``）再也查不到这一行，用户开着 Excel 跑完整批，
  结果表里对应行是空的，而且不报错。卡住能告诉用户，丢数据不能。
- tick **不许等一次完整的 write_back**：openpyxl 不支持增量写，宽表单次 2.2 秒，
  而 POLL_INTERVAL 只有 1 秒。归档跑在后台线程上。

这些用例直接调 ``_workflow_run_pass`` / ``_workflow_archive_pass``，
不真起 agent 进程——放行成败看的是库里那一行状态变了没。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import db
import dispatcher
import store


class _FakeProc:
    """占位的 AgentProc：只够 _reap 走一圈（既没中止、也没退出）。"""
    aborted = False
    exit_code = None

    def __init__(self, task_id):
        self.task_id = task_id
        self.proc = self

    def poll(self):
        return None


@pytest.fixture()
def disp(tmp_path, monkeypatch):
    """一个不真的 spawn 的调度器：把 _spawn 换成记账。

    用真的 ``Dispatcher(...)`` 而不是 ``__new__``：``running`` 是只读 property，
    而且它底下要 ``self._lock``——``__new__`` 出来的对象两者都没有，一碰就
    AttributeError，测的就不是闸门而是构造技巧了。
    """
    monkeypatch.setenv("PROTFORGE_WEB_NODE", "node")
    d = dispatcher.Dispatcher(db, max_concurrency=2, node_exe="node",
                              gpu_probe=lambda: 2)
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
    _shutdown_archive(d)


def _shutdown_archive(d, timeout: float = 30.0) -> None:
    """夹具收尾：**先把归档做完**，再停机；并断言收尾之后一条线程都不剩。

    为什么不能只调 ``d._archive_stop()``（复审已用 threading.Event 确定性复现）：

    - ``_archive_stop`` 给每条线程投结束哨兵，但线程正在 ``write_back`` 里
      （openpyxl 整表重写，结果表被 Excel 独占时更久）就看不见哨兵，join 的
      ``poll_interval * 3 + 2.0`` 一到就返回；紧接着 ``self._archive_threads = []``
      把唯一的引用也抹了——活着的线程既没被等到，也不再被任何一次后续 join 引用。
    - ``db.connect()`` **每次现读** ``PROTFORGE_WEB_DB``，而 autouse 夹具每个测试
      换一块库。于是收尾返回之后那条线程连到的是**下一个测试**的库：轻则
      ``no such table: tasks`` 告警，重则 id 撞上时**静默**把下一个测试的一号行
      任务改成 ``workflow_archived = 1``——失败落在无关的用例上，没人查得回根因。

    **只改测试侧**：产品里环境变量不变、``stop()`` 之后进程立刻退出，而
    ``write_back`` 是临时文件 + ``os.replace`` 原子写，最坏只是某行没置位，
    闸门② 与 ``recover()`` 兜住，那是设计里已经写明并接受的形状。产品侧那半步
    （留住引用、让第二次 stop 还能重试 join）修不了这条：线程照样活着去按新环境
    连库——它只能让「还有线程没停」看得见，挡不住污染。而 Python 也没有杀线程
    的办法（卡在 write_back 里的那条只能等它自己返回）。真正能改的是夹具：
    **别把没做完的活扔下就走**。
    """
    # _archive_stop 会把 _archive_threads 清空，先留住这份引用——否则连
    # 「到底有没有线程没停」都没法回答了。
    mine = list(d._archive_threads)
    drained = d._archive_wait(timeout)
    d._archive_stop()
    # 自己再等一轮真退出，不指望 _archive_stop 那个预算（它就是本条缺陷的出处）。
    deadline = time.time() + timeout
    while time.time() < deadline and any(t.is_alive() for t in mine):
        time.sleep(0.02)
    alive = [t.name for t in mine if t.is_alive()]
    assert drained, f"归档队列 {timeout} 秒没排空，收尾放行了没收完的活"
    assert not alive, f"夹具收尾返回之后还有归档线程活着：{alive}"


def _user(name="u1", role="user"):
    c = db.connect()
    try:
        with c:
            c.execute("INSERT INTO users (username, password_hash, salt, role,"
                      " disabled, created_at) VALUES (?, 'h','s', ?, 0, 't')",
                      (name, role))
        return c.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    finally:
        c.close()


def _workflow(owner_id, tmp_path, rows=5, name="wf", wid=9001, sheet="S1"):
    """建一个带真实 xlsx 源表的工作流，所以放行时能真的渲染出 brief。"""
    import workflow as wfmod
    root = Path(tmp_path) / "data" / "workflows"
    st = wfmod.staging_path(root)
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(["编号", "序列", "项目"])
    for i in range(rows):
        ws.append([f"ID{i}", f"SEQ{i}", "XBR"])
    wb.save(st)
    source = wfmod.finalize_upload(root, st, wid)
    wf = store.create_workflow(owner_id, name, "查 {{seq}}（项目 {{proj}}）",
                               sheet=sheet, source_path=str(source),
                               header_row=1, key_col=0, total_rows=rows,
                               writeback=1)
    store.set_workflow_columns(wf["id"], [
        {"col_index": 0, "header": "编号", "role": "key"},
        {"col_index": 1, "header": "序列", "role": "input", "slot": "seq"},
        {"col_index": 2, "header": "项目", "role": "context", "slot": "proj"},
        {"col_index": 3, "header": "状态", "role": "output", "field": "status"},
        {"col_index": 4, "header": "说明", "role": "output", "field": "summary"},
    ])
    store.update_workflow(wf["id"], status="running")
    return wf


def _first_row(wf, index=0):
    return store.list_workflow_rows(wf["id"], index)[0]


def _archived(task_id) -> int:
    return int(store.get_task(task_id)["workflow_archived"])


# --- 放行基本流 -------------------------------------------------------------

def test_admits_first_row_and_advances_cursor(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    got = disp._workflow_run_pass()
    assert len(got) == 1
    assert store.get_workflow(wf["id"])["cursor"] == 1
    row = _first_row(wf)
    assert row["status"] == "running"          # claim_queued 把它推成 running
    assert "SEQ0" in row["brief"]


def test_row_task_owner_is_the_workflow_owner(disp, tmp_path):
    """闸门⑥ 判的是 owner 的队列，不是别人的。"""
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    row = _first_row(wf)
    assert row["owner_id"] == uid


def test_row_task_created_event_names_the_workflow(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path, name="抗体筛选")
    disp._workflow_run_pass()
    row = _first_row(wf)
    evs = store.list_events(row["id"])
    created = [e for e in evs if e["kind"] == "created"]
    assert len(created) == 1
    assert created[0]["actor"] == "system"
    assert "抗体筛选" in created[0]["message"] and "1" in created[0]["message"]


def test_brief_of_row_n_excludes_other_rows(disp, tmp_path):
    """§6.9 上下文独立：**第 N 行的 brief 里不含第 N-1 行的值**。

    这是「任务之间上下文相互独立」那句话唯一能变成测试的形状。天然成立
    （每行独立 task、独立 work 目录），但必须钉死——批量场景里一行漏进
    上一行的序列，整个后续批次的结果都是错的，而且**不报错**。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    disp._workflow_run_pass()
    r1 = _first_row(wf, 1)
    assert "SEQ1" in r1["brief"]
    assert "SEQ0" not in r1["brief"], "第 2 行的 brief 里混进了第 1 行的序列"


def test_serial_second_row_waits(disp, tmp_path):
    uid = _user()
    _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    assert disp._workflow_run_pass() == []      # 同一 tick 不放第二行


def test_gate_5_holds_even_when_the_row_is_already_archived(disp, tmp_path):
    """⑤ 与② 会互相遮蔽：未归档的行② 就挡住了，**看不出⑤ 在不在**。

    能把二者区分开的只有「已归档但还没进终态」的行——闸门⑤ 故意不过滤
    workflow_archived 正是为它准备的（加了过滤，已归档非终态的行会既过⑤又不过
    ②③，同一个工作流两行同时推进）。少了这条围栏，⑤ 被整个删掉、或被加上
    `workflow_archived = 0` 过滤，上面那些用例都照样全绿。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="running", workflow_archived=1)
    assert disp._workflow_run_pass() == [], "闸门⑤ 被② 遮蔽掉了"
    assert store.list_workflow_rows(wf["id"], 1) == []


def test_gate_1_is_rechecked_on_the_workflow_dict_itself(disp, tmp_path):
    """闸门① 有**两道**：取数（list_running_workflows 只给 running）与
    :meth:`_workflow_admit_one` 里的再判一次。

    后者是纵深防御，也正因为冗余，只有直接调内部方法才钉得住它——从 pass 那一层
    看，两道少任何一道结果都一样，全绿的测试证明不了它还在。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path)
    store.update_workflow(wf["id"], status="paused")
    assert disp._workflow_admit_one(store.get_workflow(wf["id"])) is None
    assert disp._spawned == []


def test_next_row_after_previous_is_archived(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done")
    assert disp._workflow_run_pass() == [], "归档前不许放下一行"
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 1
    got = disp._workflow_run_pass()
    assert len(got) == 1
    assert store.list_workflow_rows(wf["id"], 1)


def test_packaging_blocks_archiving(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="packaging")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 0
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 1


# --- 闸门顺序（规格 6.4）----------------------------------------------------

def test_gate_4_precedes_gate_2(disp, tmp_path):
    """重跑的行同时满足②和④。④不排在前面 → 这条永远停在「已排队」。

    这条用例单独存在的理由：整理代码顺序的人不会想到去跑重跑用例。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    store.update_task(r0["id"], status="queued", workflow_archived=0)   # 管理员重跑
    disp._running.clear()      # 模型上管理员重跑时，上一次那个进程早就退出了
    got = disp._workflow_run_pass()
    assert len(got) == 1, "重跑的行没有被认领——④ 被②挡住了"
    assert store.get_task(r0["id"])["status"] == "running"
    assert store.get_workflow(wf["id"])["cursor"] == 1, "重跑不该再推游标"


def test_regular_tasks_go_first(disp, tmp_path):
    """顺序层让路：常规任务和工作流行任务同时有空槽时，先给常规的。"""
    uid = _user()
    wf = _workflow(uid, tmp_path)
    store.create_task("常规", "b", owner_id=uid)
    admitted = disp.tick()
    assert store.list_workflow_rows(wf["id"], 0) == []
    assert len(admitted) == 1


def test_gate_6_waits_for_owner_regular_tasks(disp, tmp_path):
    uid = _user()
    _workflow(uid, tmp_path)
    store.create_task("A 的常规任务", "b", owner_id=uid)
    assert disp._workflow_run_pass() == []


def test_gate_6_ignores_other_users_regular_tasks(disp, tmp_path):
    """owner_id 分错的话，A 的工作流会被 B 的任务挡住。"""
    a, b = _user("a"), _user("b")
    _workflow(a, tmp_path)
    store.create_task("B 的常规任务", "b", owner_id=b)
    assert len(disp._workflow_run_pass()) == 1


def test_gate_6_ignores_the_same_users_other_workflow_rows(disp, tmp_path):
    """闸门⑥ 只看**常规**任务。同一个人另一个工作流的行任务不该挡住这个。"""
    a = _user("a")
    wf_a = _workflow(a, tmp_path, name="A", wid=9101)
    wf_b = _workflow(a, tmp_path, name="B", wid=9102)
    other = store.create_task("B 的第 0 行", "b", owner_id=a,
                              workflow_id=wf_b["id"], workflow_row=0)
    store.update_task(other["id"], status="running")   # B 正在跑，不进 _running
    assert len(disp._workflow_run_pass()) == 1, "同用户的另一个工作流行任务挡住了闸门⑥"
    assert disp._spawned == [_first_row(wf_a)["id"]]
    assert store.get_workflow(wf_b["id"])["cursor"] == 0, "B 自己那一行是闸门⑤ 的事"


def test_concurrency_limit_is_enforced(disp, tmp_path):
    """_spawn 本身没有限流检查，全靠这条闸门。"""
    a, b = _user("a"), _user("b")
    disp._running[999] = object()
    disp.limit = 1
    _workflow(a, tmp_path, name="A", wid=9201)
    _workflow(b, tmp_path, name="B", wid=9202)
    assert disp._workflow_run_pass() == []


def test_paused_workflow_admits_nothing(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    store.update_workflow(wf["id"], status="paused")
    # 闸门① 的取数侧也单独钉一下：它与 _workflow_admit_one 里的再判是冗余的，
    # 只断言 pass 不放行的话，两道里少任何一道都看不出来。
    assert store.list_running_workflows() == [], "闸门① 的取数侧没滤掉 paused"
    assert disp._workflow_run_pass() == []


def test_finished_when_cursor_exhausted(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=1)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    disp._workflow_run_pass()
    assert store.get_workflow(wf["id"])["status"] == "finished"


# --- 归档（规格 7.2）--------------------------------------------------------

def test_archive_marks_every_row_individually(disp, tmp_path):
    """一批两行都终态 → 两行都要置位，不许只处理第一行。"""
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    disp._workflow_run_pass()
    r1 = _first_row(wf, 1)
    store.update_task(r1["id"], status="done")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 1
    assert _archived(r1["id"]) == 1


def test_locked_result_file_leaves_row_retryable(disp, tmp_path, monkeypatch):
    """文件被 Excel 占住 → **不置位**、留待重试。

    置位就等于宣告「这一行完事了」，而结果根本不在表里：待归档行的取数带
    ``AND workflow_archived = 0``，置位之后没有任何代码会再碰它，writeback_pending
    永远挂着没人清，用户看到的却是「跑完了」。
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    monkeypatch.setattr(wfmod, "write_back",
                        lambda *a, **k: (_ for _ in ()).throw(
                            PermissionError(13, "被占用")))
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)

    assert _archived(r0["id"]) == 0, "被占时置了位——这一行将永远不会被重试"
    assert store.get_workflow(wf["id"])["writeback_pending"] == 1
    notes = [e for e in store.list_events(r0["id"]) if e["kind"] == "note"]
    assert notes, "用户看不到「关掉 Excel 后会自动重试」这条提示"
    assert "Excel" in notes[-1]["message"]


def test_locked_row_is_written_back_on_the_next_archive_pass(disp, tmp_path,
                                                            monkeypatch):
    """解锁后由**下一次归档顺手补写**——不是定时器重试。

    只测「被占时不置位」不够：那证明了它不丢，但也可能永远不补。少这一条的
    时候，实现可以是「永远标 pending、永远不写」而全部测试通过。
    """
    from openpyxl import load_workbook
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    real = wfmod.write_back
    state = {"locked": True}

    def flaky(*a, **k):
        if state["locked"]:
            raise PermissionError(13, "被占用")
        return real(*a, **k)

    monkeypatch.setattr(wfmod, "write_back", flaky)
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 0

    state["locked"] = False
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)

    assert _archived(r0["id"]) == 1
    assert store.get_workflow(wf["id"])["writeback_pending"] == 0
    result = Path(store.get_workflow(wf["id"])["result_path"])
    ws = load_workbook(result)["S1"]
    assert ws.cell(row=2, column=4).value == "done"
    assert ws.cell(row=2, column=5).value == "ok"


def test_source_shortened_marks_row_archived_with_error_event(disp, tmp_path):
    """源表被改短 → 重试一万次也等不到，必须置位，否则闸门② 整批死锁。"""
    from openpyxl import Workbook
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")
    # 把源表改成只剩表头：第 0 个数据行不存在了 → _sheet_row_number 抛 Invalid
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "序列", "项目", "状态", "说明"])
    wb.save(store.get_workflow(wf["id"])["source_path"])

    disp._workflow_archive_pass()
    assert disp._archive_wait(30)

    assert _archived(r0["id"]) == 1, "数据损坏不置位的话闸门② 会永远等这一行"
    errs = [e for e in store.list_events(r0["id"]) if e["kind"] == "error"]
    assert errs, "失败原因没有落到用户可见的时间线上"
    assert store.workflow_has_unarchived(wf["id"]) is False, \
        "闸门② 的谓词还为真，整批就死在这一行上"


def test_gate_2_blocks_next_row_while_unarchived(disp, tmp_path, monkeypatch):
    """反向围栏：闸门② 在「有未归档的行」时确实不放行。

    钉的是**卡住是暂时的**：上一条钉住「被占时不置位」，这条钉住「不置位之后
    这一批真的停住、下一次归档成功之后又真的放行」。两条合起来才是完整契约；
    少任何一条，实现都可以是「永远不放行」或「永远放行」而全绿。
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    real = wfmod.write_back
    state = {"locked": True}

    def flaky(*a, **k):
        if state["locked"]:
            raise PermissionError(13, "被占用")
        return real(*a, **k)

    monkeypatch.setattr(wfmod, "write_back", flaky)
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert disp._workflow_run_pass() == [], "结果还没落表就放下一行了"
    assert store.get_workflow(wf["id"])["cursor"] == 1

    state["locked"] = False
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 1
    assert len(disp._workflow_run_pass()) == 1, "补写成功后闸门② 应当解除"


def test_archive_pass_exception_does_not_escape(disp, tmp_path):
    """一次异常不能越过 tick 的中线，否则所有常规任务停在排队中。"""
    uid = _user()
    _workflow(uid, tmp_path)
    disp._workflow_run_pass()

    def boom():
        raise RuntimeError("整批写挂了")
    disp._workflow_archive_pass = boom
    # tick 不该炸
    assert isinstance(disp.tick(), list)


def test_one_broken_row_does_not_stop_the_batch(disp, tmp_path, monkeypatch):
    """第 1 行的 write_back 抛异常：**下一轮它自己会再试，整批不会被永久停住**。

    Major-2 之后同一工作流的归档是串行的，所以后面那些行要等前面这行收工。
    这里因此写成三段：抛异常那一轮收不到任何一行 → 异常不再抛之后第 1 行归档 →
    再下一轮第 2 行归档。**延迟不是丢失**：不归档的行取数带
    ``AND workflow_archived = 0``，闸门② 也会把整批停住，所以它们一定还会被重试。

    （代价写在明面上：同一工作流里某一行**永久**失败时，它后面那些行也会一直
    排队。这可以接受，因为闸门② 早就把整批停住了；而让它们并发写同一张结果表
    就是 Major-2 那种静默丢更新。）
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")
    r1 = store.create_task("补的行", "b", owner_id=uid,
                           workflow_id=wf["id"], workflow_row=1)
    store.update_task(r1["id"], status="done", summary="ok2")

    real = wfmod.write_back
    state = {"broken": True}

    def boom_first(*a, **k):
        if state["broken"] and a[4][0][0] == 0:      # jobs = [(row_index, col, value)]
            raise RuntimeError("openpyxl 崩了")
        return real(*a, **k)

    monkeypatch.setattr(wfmod, "write_back", boom_first)
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 0, "抛异常的行被误判成归档完成了"
    assert disp._archive_thread is not None and disp._archive_thread.is_alive(), \
        "归档线程被一个异常带走了"

    state["broken"] = False
    disp._workflow_archive_pass()                    # 异常不再抛：第 1 行收工
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 1, "下一轮没重试，整批被永久停住了"
    disp._workflow_archive_pass()                    # 第 2 行在它后面收工
    assert disp._archive_wait(30)
    assert _archived(r1["id"]) == 1, "一行失败把整批带走了"


# --- 归档不阻塞调度主路径 ----------------------------------------------------

def test_tick_does_not_wait_for_write_back(disp, tmp_path, monkeypatch):
    """POLL_INTERVAL 是 1 秒，而宽表单次 write_back 2.2 秒——tick 绝不能等它。

    这条不是性能断言而是**结构断言**：write_back 被事件门卡住不放行，tick 只要
    同步做归档就必然超时返回。
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    entered = threading.Event()
    release = threading.Event()
    real = wfmod.write_back

    def slow(*a, **k):
        entered.set()
        if not release.wait(10):
            raise AssertionError("tick 在等一次完整的 write_back")
        return real(*a, **k)

    monkeypatch.setattr(wfmod, "write_back", slow)
    try:
        t0 = time.time()
        disp.tick()
        elapsed = time.time() - t0
        assert entered.wait(5), "归档线程根本没开工"
        assert elapsed < 3.0, f"tick 花了 {elapsed:.2f}s，归档还在主路径上"
        # 归档还没完成 → 这一行仍未归档 → 闸门② 仍不放行
        assert _archived(r0["id"]) == 0
        assert disp._workflow_run_pass() == []
    finally:
        release.set()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 1


def test_row_in_flight_is_not_enqueued_twice(disp, tmp_path, monkeypatch):
    """竞态：tick 刚把行交给归档线程、还没置位就又扫到一次。

    ``workflow_archived = 0`` 这个谓词在这里**兜不住**：那一列要等整表写完才
    置位，中间有两秒。这两秒里每次 tick 都会把同一行重新捞出来——所以必须靠
    在途集合（或者别的等价手段）去重，否则同一个文件被同时写两次。
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    entered = threading.Event()
    release = threading.Event()
    real = wfmod.write_back
    calls = []

    def slow(*a, **k):
        calls.append(1)
        entered.set()
        release.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(wfmod, "write_back", slow)
    try:
        first = disp._workflow_archive_pass()
        assert entered.wait(5)
        second = disp._workflow_archive_pass()
        third = disp._workflow_archive_pass()
        assert first == [r0["id"]]
        assert second == [] and third == [], "在途的行被重复排队了"
    finally:
        release.set()
    assert disp._archive_wait(30)
    assert len(calls) == 1, "同一个结果文件被同时写了两次"


# --- I-1：文件长期被占时的告警风暴与恢复 -------------------------------------
#
# 上游裁定「被占时不置 workflow_archived」是为了**不静默丢数据**。本轮给这条
# 路径加去重（压掉重复的 note），最容易犯的错就是把重试一起压掉——于是原来那个
# bug 原样装回来：闸门② 永远等这一行，用户看到的却是「跑完了」。下面两条必须
# **成对**存在：一条挡风暴，一条挡「为了挡风暴而不重试」。


def test_repeated_locked_ticks_do_not_flood_the_timeline(disp, tmp_path, monkeypatch):
    """文件一直被占 → 告警**至多一两条**，但**每一轮都真的重试**。

    两个方向都得钉，缺一个都有实现能钻过去：
    - 只钉「不刷屏」：实现可以是「被占过一次就再也不试」，照样全绿——而那
      正是「静默丢数据」：结果表留空、不报错。
    - 只钉「一直重试」：实现可以是每轮记一条一模一样的 note，照样全绿——
      用户吃顿饭回来时间线上多出上万条噪声（实测 59 条/分）。
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    calls = {"n": 0}

    def locked(*a, **k):
        calls["n"] += 1
        raise PermissionError(13, "被占用")

    monkeypatch.setattr(wfmod, "write_back", locked)
    for _ in range(12):
        disp._workflow_archive_pass()
        assert disp._archive_wait(30)

    # 方向一：重试没被顺手压掉
    assert calls["n"] >= 12, \
        f"12 轮只试了 {calls['n']} 次——去重把重试一起去掉了"
    assert _archived(r0["id"]) == 0, "被占时置了位——这一行将永远不会被重试"
    assert store.get_workflow(wf["id"])["writeback_pending"] == 1
    # 方向二：告警没变成风暴
    notes = [e for e in store.list_events(r0["id"]) if e["kind"] == "note"]
    assert notes, "用户一条提示都收不到"
    assert len(notes) <= 2, f"12 轮被占刷出 {len(notes)} 条 note：{notes}"
    assert "Excel" in notes[-1]["message"]


def test_locked_row_recovers_after_a_lock_storm(disp, tmp_path, monkeypatch):
    """挡风暴**不许**顺带挡掉重试：解锁后再来一轮，真的补写成功。

    与上一条成对。少这一条，实现可以是「一旦被占就再也不碰这一行」，
    而上一条会全绿——闸门② 永远等它，用户看到的是「跑完了但结果表是空的」。
    """
    from openpyxl import load_workbook
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    real = wfmod.write_back
    state = {"locked": True}

    def flaky(*a, **k):
        if state["locked"]:
            raise PermissionError(13, "被占用")
        return real(*a, **k)

    monkeypatch.setattr(wfmod, "write_back", flaky)
    for _ in range(12):
        disp._workflow_archive_pass()
        assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 0, "还没解锁就归档了"

    state["locked"] = False
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)

    assert _archived(r0["id"]) == 1, "解锁之后没有自动补写——数据丢了"
    assert store.get_workflow(wf["id"])["writeback_pending"] == 0
    result = Path(store.get_workflow(wf["id"])["result_path"])
    ws = load_workbook(result)["S1"]
    assert ws.cell(row=2, column=4).value == "done"
    assert ws.cell(row=2, column=5).value == "ok"


# --- I-2：挂起的 write_back 不能拖住别的行 ---------------------------------


def test_archive_pool_has_at_least_two_workers(disp, tmp_path):
    """归档池**至少两条**线程（闸门② 等的就是这个队列里的行）。

    单线程下一条挂起的 write_back 就是整个平台静默停摆：线程活着、队列不报错、
    闸门② 永远等那一行。这是结构要求，不是性能优化，所以钉死「≥2」。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    disp._workflow_archive_pass()
    assert disp._archive_wait(30)

    alive = [t for t in disp._archive_threads if t.is_alive()]
    assert len(alive) >= 2, f"归档池只有 {len(alive)} 条线程"
    assert len({t.name for t in alive}) >= 2, "线程同名，出问题时看不出是哪条"


def test_one_hung_write_back_does_not_block_other_rows(disp, tmp_path, monkeypatch):
    """I-2 的核心围栏：一条 write_back 挂起，**别的批次照样被归档**。

    单线程池下这里必然失败：那一行排在一条线程上并挂着，别的批次的行
    永远轮不到，闸门② 于是卡住那个工作流而界面上看不出任何异常。

    **「别的批次」必须是另一个工作流**（Major-2 之后）：同一个工作流的行共享
    一张结果表，并发写就是静默丢更新（见下面那条串行围栏），所以它们本来就该
    排队。ARCHIVE_WORKERS=2 的意义从一开始就是「别让一个工作流把平台停住」。
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path, name="A", wid=9001)
    other = _workflow(uid, tmp_path, name="B", wid=9002)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")
    # A 的第 2 行：与 r0 同一张结果表，**必须**排在 r0 后面（Major-2）
    same_wf_next = store.create_task("A 的第 2 行", "b", owner_id=uid,
                                     workflow_id=wf["id"], workflow_row=1)
    store.update_task(same_wf_next["id"], status="done", summary="ok1")
    r_other = _first_row(other)
    store.update_task(r_other["id"], status="done", summary="别的批次")

    entered = threading.Event()
    release = threading.Event()
    real = wfmod.write_back

    def hang_first(*a, **k):
        # 按**结果表**判是哪一个工作流：两个工作流的第一行 row_index 都是 0，
        # 只看 row_index 会把两个批次一起挂住，那这条就测不到跨批次并发了。
        if Path(a[1]).parent.name == "9001" and a[4][0][0] == 0:
            entered.set()
            release.wait(30)
            raise PermissionError(13, "一直挂着")
        return real(*a, **k)

    monkeypatch.setattr(wfmod, "write_back", hang_first)
    try:
        disp._workflow_archive_pass()
        assert entered.wait(5), "归档线程根本没开工"
        deadline = time.time() + 15
        while time.time() < deadline and not _archived(r_other["id"]):
            time.sleep(0.05)
        # 夹具自证：上面那圈是「别的批次被归档了」，不是「挂起提前结束了」
        assert entered.is_set() and not release.is_set(), \
            "夹具失效：挂起提前结束，这条断言测不到想测的东西"
        assert _archived(r_other["id"]) == 1, \
            "一个工作流挂住的 write_back 把另一个工作流也拖住了"
        assert _archived(r0["id"]) == 0, "挂着的那行被误判成归档完成了"
        assert _archived(same_wf_next["id"]) == 0, \
            "同一工作流的第二行趁第一行挂着就写了进去——那正是 Major-2 的丢更新"
    finally:
        release.set()
    assert disp._archive_wait(30)


def test_same_workflow_archives_never_overlap(disp, tmp_path, monkeypatch):
    """Major-2 的围栏：同一工作流任意时刻**只有一条 write_back 在途**。

    丢更新的形状：两条 write_back 各自 load 同一张结果表、各改一格、各自整表
    save。后 save 的那份内容是它**在自己 load 之后**才有的，中间隔着另一条写——
    于是先写进去的那一格被整片盖掉，而**两行都会 workflow_archived=1**，归档
    pass 再也不会回头：不报错、不重试，用户下载到的结果表少一行值。复审实测
    8 次里漏 1 次。

    **怎么造出「两条同时在途」而不用多进程碰运气**：探针在 write_back 内部按
    dest（即工作流的结果表）计在场人数，第一个进来的人**开着门等**——
    并发版本下第二个线程必然挤得进来（两个 job 都已入队、两条线程都已起来），
    人数记到 2；串行版本下第二个线程压根到不了这里，门白开后放行。判据是
    **在场人数**，不是耗时，所以不需要靠「跑得够快才撞上」。
    """
    from openpyxl import load_workbook

    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok0")
    r1 = store.create_task("补的行", "b", owner_id=uid,
                           workflow_id=wf["id"], workflow_row=1)
    store.update_task(r1["id"], status="done", summary="ok1")

    inside: dict[str, int] = {}
    overlap: list[str] = []
    written: list[int] = []
    guard = threading.Lock()
    real = wfmod.write_back

    def probe(source, dest, sheet, header_row, jobs):
        key = str(dest)
        with guard:
            inside[key] = inside.get(key, 0) + 1
            n = inside[key]
            if n > 1:
                overlap.append(f"{key}:{jobs[0][0]}")
            written.append(int(jobs[0][0]))
        try:
            if n == 1:
                time.sleep(1.2)            # 给同工作流的第二行一个进门的机会
        finally:
            with guard:
                inside[key] -= 1
        return real(source, dest, sheet, header_row, jobs)

    monkeypatch.setattr(wfmod, "write_back", probe)
    # 串行版本下第二行要等下一轮 pass 才排得上，所以像生产那样反复驱动。
    deadline = time.time() + 40
    while time.time() < deadline:
        disp._workflow_archive_pass()
        assert disp._archive_wait(30)
        if _archived(r0["id"]) == 1 and _archived(r1["id"]) == 1:
            break
        time.sleep(0.05)

    assert overlap == [], (
        "同一个工作流有两条 write_back 同时在途：后写的拿它更早读到的旧内容盖上去，"
        f"先写的那一格永久丢失且永不重试。撞在一起的是：{overlap}")
    # 夹具自证：探针真的跑过两次、真的各写一行、真的开过门等人。
    assert sorted(written) == [0, 1], f"夹具没走到两次写：{written}"
    # 串行不等于不写：两行的值都必须在结果表里。
    ws = load_workbook(store.get_workflow(wf["id"])["result_path"])["S1"]
    assert ws.cell(row=2, column=5).value == "ok0"
    assert ws.cell(row=3, column=5).value == "ok1"


def test_different_workflows_still_archive_concurrently(disp, tmp_path,
                                                        monkeypatch):
    """分段加锁不许退化成一把全局锁：两个工作流必须能**同时**归档。

    Major-2 是按 workflow_id 分段加锁，不是全局串行——全局锁会把 500 行的批次
    排成一条十几分钟的队，ARCHIVE_WORKERS 的下限 2 也就白设了。
    围法用 barrier：两条 write_back 各自走到 barrier 一起放行。全局锁下第二条
    永远到不了 barrier，超时抛 BrokenBarrierError，围栏就红。
    """
    import workflow as wfmod
    uid = _user()
    a = _workflow(uid, tmp_path, name="A", wid=9001)
    b = _workflow(uid, tmp_path, name="B", wid=9002)
    disp._workflow_run_pass()
    ra, rb = _first_row(a), _first_row(b)
    store.update_task(ra["id"], status="done", summary="ok-a")
    store.update_task(rb["id"], status="done", summary="ok-b")

    gate = threading.Barrier(2, timeout=20)
    real = wfmod.write_back

    def rendezvous(*a_, **k):
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            raise RuntimeError("两个工作流没有同时归档：归档被串成单线程了")
        return real(*a_, **k)

    monkeypatch.setattr(wfmod, "write_back", rendezvous)
    disp._workflow_archive_pass()
    assert disp._archive_wait(30), "归档队列没排空"

    assert _archived(ra["id"]) == 1, "A 的行没归档"
    assert _archived(rb["id"]) == 1, "B 的行没归档——被另一个工作流串住了"


def test_archive_stuck_threshold_default_is_thirty_seconds(disp):
    """出厂阈值必须是 30 秒，且调度器实例真的取了这个值。

    上一条为了不等 30 秒，把阈值改成 0.2——于是**模块里那个常量本身没有任何
    测试碰过**：把它从 30 改成 10^9（「看门狗永不触发」）全套照样绿。变异实测
    的幸存者就是这条。围的是**出厂策略值**，所以这里得单独钉一下：它既要明显
    长于一次正常 write_back（宽表实测 2.2 秒，误报比不报更坏），又要短到用户
    不会等到放弃（分钟级）。
    """
    assert dispatcher.ARCHIVE_WORKERS >= 2, "归档池出厂大小掉到 2 以下了"
    assert dispatcher.ARCHIVE_STUCK_SECONDS == 30.0
    assert disp._archive_stuck_seconds == dispatcher.ARCHIVE_STUCK_SECONDS, \
        "实例没取模块常量——看门狗阈值会各走各的"
    # 误报比不报更坏：正常一次 write_back 是 2.2 秒，阈值必须远大于它
    assert dispatcher.ARCHIVE_STUCK_SECONDS >= 10.0


def test_stuck_row_gets_a_user_visible_warning(disp, tmp_path, monkeypatch):
    """看门狗：挂起超过阈值的行要有一句**用户能照着做**的话，且只说一次。

    阈值调成 0.2 秒，免得测试真等 30 秒——围的是「告警有没有发生、发生几次」，
    不是那个 30 的具体取值。不重排是刻意的：重排等于同一行可能被写两次。
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    disp._archive_stuck_seconds = 0.2
    entered = threading.Event()
    release = threading.Event()

    def hang(*a, **k):
        entered.set()
        release.wait(20)
        raise PermissionError(13, "一直挂着")

    monkeypatch.setattr(wfmod, "write_back", hang)
    try:
        disp._workflow_archive_pass()
        assert entered.wait(5), "归档线程根本没开工"
        time.sleep(0.3)
        # 看门狗挂在归档巡检上（tick 每轮都会走一遍），不另起线程
        for _ in range(5):
            disp._workflow_archive_pass()
            time.sleep(0.1)
        notes = [e for e in store.list_events(r0["id"]) if e["kind"] == "note"]
        stuck = [n for n in notes if "卡住" in n["message"]]
        assert stuck, f"挂起的行没有告警，用户只看到「停在第 1 行」：{notes}"
        assert len(stuck) == 1, f"看门狗自己刷屏了 {len(stuck)} 条"
        assert ("Excel" in stuck[0]["message"]
                or "网络盘" in stuck[0]["message"]), \
            "告警没告诉用户接下来该做什么"
    finally:
        release.set()
    assert disp._archive_wait(30)


# --- I-4：OSError 那半边也要有围栏 -----------------------------------------


def test_disk_full_leaves_row_retryable_with_a_note(disp, tmp_path, monkeypatch):
    """磁盘满（errno 28）→ **同样不置位**，而且同样挂上 pending 与提示。

    这一侧此前**完全没被钉住**：把 ``, OSError`` 从 except 里删掉，31 条照样
    全绿。而磁盘满、设备未就绪、网络盘断链走的都是 OSError——行还是留待重试
    （对的），可 writeback_pending 不挂、那条「关掉占用它的程序」的提示也没有，
    用户在时间线上看不到任何异常。
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    monkeypatch.setattr(wfmod, "write_back",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError(28, "No space left on device")))
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)

    assert _archived(r0["id"]) == 0, "磁盘满却置了位——这一行永远不会被重试"
    assert store.get_workflow(wf["id"])["writeback_pending"] == 1, \
        "OSError 侧没挂上 writeback_pending"
    notes = [e for e in store.list_events(r0["id"]) if e["kind"] == "note"]
    assert notes, "磁盘满时用户看不到任何提示"


# --- 常规队列排除工作流任务（规格 6.1）-------------------------------------

def test_queued_tasks_excludes_workflow_rows(disp, tmp_path):
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()                       # 行任务变成 running
    store.update_task(_first_row(wf)["id"], status="queued")   # 人工退回队列
    got = [int(r["id"]) for r in disp._queued_tasks()]
    assert _first_row(wf)["id"] not in got


# --- 回写的目标表一致性（规格 7.1）------------------------------------------

def _two_sheet_book(tmp_path):
    from openpyxl import Workbook
    src = Path(tmp_path) / "two.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "状态"])
    ws.append(["ID0", ""])
    other = wb.create_sheet("S2")
    other.append(["编号", "状态"])
    other.append(["ID0", ""])
    wb.save(src)
    return src


def test_write_back_rejects_a_sheet_the_row_numbers_come_from_nowhere(tmp_path):
    """行号按第一张表算、值写进第二张表 = 静默写错行。必须报错。"""
    import shutil
    import workflow as wfmod
    src = _two_sheet_book(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    with pytest.raises(store.Invalid) as ei:
        wfmod.write_back(src, dest, "S2", 1,
                         [(0, wfmod.OutputCol(1, "status"), "done")])
    assert "S2" in str(ei.value) and "S1" in str(ei.value)


def test_write_back_accepts_the_first_sheet(tmp_path):
    """配套围栏：传对表**不许**报错——否则上面那条会被「一律抛错」蒙混过去。"""
    import shutil
    import workflow as wfmod
    src = _two_sheet_book(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    wfmod.write_back(src, dest, "S1", 1,
                     [(0, wfmod.OutputCol(1, "status"), "done")])
    from openpyxl import load_workbook
    assert load_workbook(dest)["S1"].cell(row=2, column=2).value == "done"


def test_archive_uses_the_sheet_the_row_numbers_came_from(disp, tmp_path):
    """工作流上存的 sheet 若是别的表名，归档**照样成功**——因为调度器传的是
    ``view.sheet``（第一张表），不是用户填的那个。"""
    uid = _user()
    wf = _workflow(uid, tmp_path)
    store.update_workflow(wf["id"], sheet="并不存在的表")
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 1
    from openpyxl import load_workbook
    result = Path(store.get_workflow(wf["id"])["result_path"])
    assert load_workbook(result)["S1"].cell(row=2, column=4).value == "done"


def test_result_path_defaults_next_to_the_source(disp, tmp_path):
    """没配 result_path 时落到 source.xlsx 旁边，而不是变成一个叫 "." 的路径。"""
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    result = store.get_workflow(wf["id"])["result_path"]
    assert result and Path(result).name == "result.xlsx"
    assert Path(result).exists()


# --- C-1：result.xlsx 必须累积（规格 7.1「当前进度」）------------------------
#
# 上面 4 处 result.xlsx 断言**每处只归档一行**，天然看不见累积：缺陷版本下
# 「只有最后归档的那一行有值」与正确版本下「每一行都有值」在这 4 条里表现
# 完全一样。所以这里补一条走完整调度器路径、**三行都跑到终态并归档**的用例，
# 再打开 result.xlsx 逐行核对——这正是规格 §10 点名要的形状。


def _run_and_archive_all(disp, wf, count):
    """放行 count 行、每行跑到终态并归档——完全走调度器那条路。

    每行之间 ``_running.clear()``：夹具的假进程 poll() 永远返回 None，于是
    「并发上限」那道闸（``len(self.running) >= self.limit``，夹具里是 2）会
    在第三行挡住我们。真机上不会这样——上一行的进程早就退出了、我们把那一行
    置成 done 就是在说它跑完了。
    """
    for i in range(count):
        disp._running.clear()
        assert len(disp._workflow_run_pass()) == 1, f"第 {i + 1} 行没放行"
        row = _first_row(wf, i)
        store.update_task(row["id"], status="done", summary=f"ok{i}")
        disp._workflow_archive_pass()
        assert disp._archive_wait(30)
    return [_first_row(wf, i) for i in range(count)]


def test_result_xlsx_accumulates_every_archived_row(disp, tmp_path):
    """C-1 的调度器层围栏：三行全部 succeeded、全部归档 → 表里三行都有值。

    缺陷形态：``write_back`` 无条件从 source 重载，而唯一生产调用点每次只传
    **一行** jobs，于是每归档一行就把前面写进去的结果整片擦掉。500 行的批次
    结果表里只剩一格有值，``workflow_archived`` 全是 1，不报错、不卡住，
    界面还显示「已完成」——静默丢数据，且没有任何测试看得见。

    断言必须**逐行**核对。只断「最后一行有值」会被缺陷版本蒙混过去
    （最后一行恰好是缺陷下也会对的那一格）。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    rows = _run_and_archive_all(disp, wf, 3)
    assert [_archived(r["id"]) for r in rows] == [1, 1, 1], \
        "三行都跑完都归档了，有一行没置位——不是本用例要查的那个缺陷"

    from openpyxl import load_workbook
    result = Path(store.get_workflow(wf["id"])["result_path"])
    assert result.is_file(), "result.xlsx 压根没生成"
    ws = load_workbook(result)["S1"]
    assert [ws.cell(row=2 + i, column=4).value for i in range(3)] == \
        ["done", "done", "done"], "历史行的结果被后来的归档静默抹掉了"
    assert [ws.cell(row=2 + i, column=5).value for i in range(3)] == \
        ["ok0", "ok1", "ok2"], "结果说明同样只有最后一行有值"


def test_result_xlsx_survives_a_retry_of_the_first_row(disp, tmp_path):
    """重跑第 0 行 → 表里既有第 0 行的新值，也**仍然有**第 1、2 行的旧值。

    规格 §10 点名要「重跑要重新归档」；而「重跑不许把别的行弄丢」是同一个
    累积性质在另一条路径上的表现——管理员重跑是批量场景里最常发生的动作。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    rows = _run_and_archive_all(disp, wf, 3)
    store.update_task(rows[0]["id"], status="queued", workflow_archived=0)
    disp._running.clear()
    store.update_task(rows[0]["id"], status="running")
    store.update_task(rows[0]["id"], status="done", summary="重跑后的新值")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)

    from openpyxl import load_workbook
    ws = load_workbook(Path(store.get_workflow(wf["id"])["result_path"]))["S1"]
    assert ws.cell(row=2, column=5).value == "重跑后的新值"
    assert ws.cell(row=3, column=5).value == "ok1", "重跑第 0 行把第 1 行抹掉了"
    assert ws.cell(row=4, column=5).value == "ok2", "重跑第 0 行把第 2 行抹掉了"


# --- M-1：中止之后自己收敛到终态，被中止的那一行照样落表 ---------------------
#
# 中止 = status 置 paused + abort_requested=1 + 杀掉在跑的行。三个副作用叠成
# 一条死路：① finished 只在闸门③ 产生，而闸门① 只放行 running 的工作流，
# 于是**没有任何代码能推到终态**；② 归档 pass 只扫 running，被中止的那一行
# （终态、未归档）永远排在取数范围之外；③ 界面把下载入口按
# status === "finished" 判断，永久隐藏。用户点完中止，已算出的部分结果
# 既不在表里、也下载不到，唯一出路是再点一次「继续」。


def _abort(disp, wf):
    """按 abort 路由的真实语义中止（app.py 的 control_workflow + _abort_live_rows）。

    ``_running.clear()`` 是必须的：中止会把在跑的行置成终态并由持有它的
    dispatcher 在下一次 ``_reap`` 里杀掉。夹具的 ``_FakeProc`` 没有 pid，
    真去 ``kill_tree`` 会 AttributeError——清掉 ``_running`` 就是「这一行的
    进程已经被杀掉并收尾」这件事在测试里的写法。
    """
    store.update_workflow(wf["id"], status="paused", abort_requested=1)
    for row in store.list_workflow_rows(wf["id"]):
        if row["status"] not in db.TERMINAL_STATUSES:
            store.set_status(row["id"], "failed", actor="human",
                             message="工作流中止")
    disp._running.clear()


def _settle_after_abort(disp, ticks=5):
    """连跑若干轮 tick，**不带任何用户操作**——收敛必须是它自己做到的。"""
    for _ in range(ticks):
        disp.tick()
        disp._archive_wait(30)


def test_aborted_workflow_converges_to_a_terminal_state_on_its_own(disp, tmp_path):
    """中止之后连跑 5 轮 tick → 状态自己到终态。

    workflows.status 的 CHECK 只有 finished 一个终态（规格 6.6 明确不新增
    aborted 值，代价是 SQLite 改 CHECK 要重建表），所以「中止后的终态」
    只能是它。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    disp._workflow_run_pass()
    _abort(disp, wf)
    _settle_after_abort(disp)

    got = store.get_workflow(wf["id"])
    assert got["status"] == "finished", (
        f"中止之后 5 轮 tick 仍然是 {got['status']}——必须用户再点一次继续")
    assert got["abort_requested"] == 1, "收敛不该顺手清掉中止标记"
    # 复用 finished 不会误导：cursor 如实显示跑了几行，abort_requested 也还在。
    assert got["cursor"] < got["total_rows"], \
        "中止后 cursor 应当停在中止那一刻，不该假装整批跑完了"


def test_aborted_row_is_still_archived(disp, tmp_path):
    """被中止的那一行**照样归档**：结果进表，而且归档 pass 看得见 paused。"""
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    _abort(disp, wf)
    _settle_after_abort(disp)

    assert _archived(r0["id"]) == 1, (
        "被中止的那一行没有归档——归档 pass 只扫 running，paused 被漏掉了")
    result = store.get_workflow(wf["id"])["result_path"]
    assert result and Path(result).is_file(), "被中止的这一行连结果表都没生成"
    from openpyxl import load_workbook
    assert load_workbook(Path(result))["S1"].cell(row=2, column=4).value == "failed"


def test_aborted_workflow_admits_nothing_even_after_converging(disp, tmp_path):
    """收敛到终态**不许**反过来放行新行：中止是终态，不是「暂停后自动继续」。"""
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    disp._workflow_run_pass()
    _abort(disp, wf)
    _settle_after_abort(disp)
    assert store.get_workflow(wf["id"])["status"] == "finished"
    assert disp._workflow_run_pass() == [], "中止之后居然又放行了新行"
    assert store.list_workflow_rows(wf["id"], 1) == []


def test_paused_workflow_still_archives_its_finished_rows(disp, tmp_path):
    """**普通暂停**（abort_requested=0）也不该让已跑完的行烂在库里。

    归档与「能不能放行下一行」是两件事：暂停之后没有任何行在跑，但已经跑完
    的那几行仍然要落表——规格 7.1 的「随时能下载当前进度」在暂停态同样成立。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")
    store.update_workflow(wf["id"], status="paused")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert _archived(r0["id"]) == 1, "暂停态下已完成的行没有归档"


def test_download_entry_is_open_whenever_a_result_file_exists(disp, tmp_path):
    """下载入口的判据是「有没有可下的结果表」，不是 ``status == 'finished'``。

    M-1 死路的后半段就在这里。**关键是要在 status 还不是 finished 的那一刻
    断言**：刚放行完一行、归档落表、用户点了暂停或中止的那一刻，工作流是
    ``paused`` 而结果表已经在磁盘上了——按 finished 判的话入口被藏起来，
    用户连已经算出来的部分结果都拿不到。用「中止并收敛之后」来断言不行：
    收敛之后 status 恰好就是 finished，两种判据给出同一个答案，围栏变恒真。
    """
    import app as svc
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert Path(store.get_workflow(wf["id"])["result_path"]).is_file()

    # **此刻 status 还是 running**：按 status === "finished" 判的话入口是关的。
    view = svc._wf_view(store.get_workflow(wf["id"]))
    assert view["status"] != "finished", "夹具失效：这一行恰好已经跑完整批了"
    assert view["result_ready"] is True, (
        "结果表已经生成、也已经归档，下载入口却还是关着的——"
        "判据被写成了 status === 'finished'")

    # 中止并收敛之后同样要能下（收敛复用的是 finished，不该把它排除在外）。
    _abort(disp, wf)
    _settle_after_abort(disp)
    after = svc._wf_view(store.get_workflow(wf["id"]))
    assert after["status"] == "finished"
    assert after["result_ready"] is True

    # 配套：还没有结果表时判据必须是 false，否则入口指向一个必然 404 的链接。
    fresh = _workflow(uid, tmp_path, name="B", wid=9099)
    assert svc._wf_view(store.get_workflow(fresh["id"]))["result_ready"] is False, \
        "还没有结果表却说可以下载"


def test_workflow_page_shows_the_download_entry_by_result_readiness():
    """前端那一半：``#wf-download`` 的显隐读 ``result_ready``，**不读 status**。

    服务端算对了、前端照样按 status 判，那条死路原封不动地留在界面上——
    而真机上点得到的只有前端这条。两边必须各钉一次。
    """
    js = (Path(__file__).resolve().parent.parent / "web" / "workflow.js").read_text(
        encoding="utf-8")
    line = [ln.strip() for ln in js.splitlines()
            if "$(\"#wf-download\").hidden" in ln]
    assert line, "找不到 #wf-download 的显隐语句——下载入口的判据被删了？"
    assert "result_ready" in line[0], (
        f"下载入口按 {line[0]!r} 判——按 status 判的话中止之后入口被永久藏起来")
    assert "finished" not in line[0]


def test_workflow_page_has_a_writeback_checkbox_wired_to_save():
    """前端那一半：勾选框存在、并且真的随「保存映射」发出去。

    一个不发出去的勾选框比没有更糟：用户以为自己关掉了敏感序列的整表复制，
    实际磁盘上照样躺着一份。
    """
    import re
    root = Path(__file__).resolve().parent.parent / "web"
    html = (root / "workflow.html").read_text(encoding="utf-8")
    assert 'id="wf-writeback"' in html, "页面上没有回写开关的勾选框"
    js = (root / "workflow.js").read_text(encoding="utf-8")
    body = re.search(r"async function saveMapping\(\)\s*\{(.*?)\n\}", js, re.S)
    assert body, "找不到 saveMapping"
    assert "writeback" in body.group(1) and "$(\"#wf-writeback\")" in body.group(1), (
        "保存映射的请求体里没有带上回写开关——勾选框是个装饰")
    # 回显：打开详情时把库里的值读回勾选框
    assert re.search(r'\$\("#wf-writeback"\)\.checked\s*=', js), \
        "打开详情时没有回显回写开关，用户看不到当前设置"


# --- M-2：跳过的那一行不许被跑，结果列写「已跳过」---------------------------


def test_skip_before_admit_does_not_run_that_row(disp, tmp_path):
    """跳过先于放行：那一行**不得被建任务、不得烧一次计算**。

    「跳过」的用途就是「这行序列是坏的，别烧 GPU」，而放行侧按 cursor 建任务
    的那条路原来整个文件都不读 workflow_skips——最该跳的场景（坏行还没放行）
    恰好是唯一会失效的场景，坏序列会被真的提交一次计算。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    assert store.get_workflow(wf["id"])["cursor"] == 0
    store.skip_workflow_row(wf["id"], 0, "序列不对")

    disp._workflow_run_pass()
    assert disp._spawned == [], "被跳过的第 0 行照样被放出去跑了"
    rows0 = store.list_workflow_rows(wf["id"], 0)
    assert [r["status"] for r in rows0] == ["failed"], (
        "被跳过的行必须是终态，否则闸门② 会一直等它，整批卡住")


def test_skipped_row_is_written_as_skipped_in_the_result_table(disp, tmp_path):
    """规格 §10 原话：那一行的结果列写「已跳过」，而且该行是终态。

    这里不能写 status（failed 读起来像「跑失败了」，而用户的本意是「我故意
    没跑」），所以钉的是字面上的「已跳过」三个字。
    """
    from openpyxl import load_workbook
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    store.skip_workflow_row(wf["id"], 0, "序列不对")
    disp._workflow_run_pass()
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)

    r0 = store.list_workflow_rows(wf["id"], 0)[0]
    assert _archived(r0["id"]) == 1
    ws = load_workbook(Path(store.get_workflow(wf["id"])["result_path"]))["S1"]
    assert ws.cell(row=2, column=4).value == "已跳过", (
        f"结果列写的是 {ws.cell(row=2, column=4).value!r}，规格要求「已跳过」")
    assert ws.cell(row=2, column=5).value == "用户跳过：序列不对", (
        "跳过原因没有落进结果说明列，用户在表里看不到这一行为什么没跑")


def test_next_row_is_admitted_normally_after_a_skip(disp, tmp_path):
    """规格 §10：跳过一行后，**下一行照常放行**（否则整批在一行上死锁）。"""
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    store.skip_workflow_row(wf["id"], 0, "序列不对")

    disp._workflow_run_pass()          # 补出第 0 行的终态任务
    disp._workflow_archive_pass()
    assert disp._archive_wait(30)
    assert len(disp._workflow_run_pass()) == 1, "跳过之后下一行没放行"
    assert store.list_workflow_rows(wf["id"], 1), "第 1 行没有被放行"
    assert store.get_workflow(wf["id"])["cursor"] == 2


def test_skip_after_admit_is_not_re_run(disp, tmp_path):
    """放行后跳过（跳过晚于放行）：那一行被置终态，**不许再跑一遍**。

    闸门④ 是「管理员重跑」的唯一落点，而重跑把行退回 queued——那里不看
    workflow_skips 的话，被用户跳过的行会被重排拉起来再烧一次。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    disp._running.clear()
    spawned_before = list(disp._spawned)      # 第一次放行确实跑过它，取差集
    store.skip_workflow_row(wf["id"], 0, "序列不对")
    store.set_status(r0["id"], "failed", actor="human", message="用户跳过")
    store.update_task(r0["id"], status="queued")     # 管理员重跑
    assert disp._workflow_run_pass() == [], "被跳过的行被重排拉起来又跑了一遍"
    assert disp._spawned == spawned_before, "被跳过的行又被跑了一遍"
    assert store.get_task(r0["id"])["status"] == "failed", \
        "跳过的那一行应当留在终态上"


def test_skip_is_scoped_to_one_workflow(disp, tmp_path):
    """跳过是**按行**的，不许波及另一个工作流的第 0 行。"""
    uid = _user()
    a = _workflow(uid, tmp_path, name="A", wid=9101)
    b = _workflow(uid, tmp_path, name="B", wid=9102)
    store.skip_workflow_row(a["id"], 0, "A 的第 0 行坏了")
    disp._workflow_run_pass()
    disp._archive_wait(30)
    assert store.list_workflow_rows(b["id"], 0), "B 的第 0 行被 A 的跳过牵连了"
    assert store.list_workflow_skips(b["id"]) == set()


# --- M-4：回写开关（规格 2 决策表 / 7.1 / 9.1）--------------------------------


def test_writeback_off_archives_every_row_but_writes_no_xlsx(disp, tmp_path):
    """关掉开关：``workflow_archived`` 全为 1，**且没有**生成 result.xlsx。

    「照样归档」这一半最容易踩：闸门② 等的是「这一行不用再写了」，跳过置位
    的话开关一关整批就永远停在那一行，界面显示「运行中」但一行都不动。
    """
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=2)
    store.update_workflow(wf["id"], writeback=0)
    rows = _run_and_archive_all(disp, wf, 2)
    assert [_archived(r["id"]) for r in rows] == [1, 1], (
        "关掉回写就不归档——闸门② 会永远等这两行，整批死锁")
    assert store.get_workflow(wf["id"])["result_path"] == "", (
        "关掉回写却仍然写了 result.xlsx：用户要的就是磁盘上不留整表副本")
    assert not list(Path(store.get_workflow(wf["id"])["source_path"]).parent
                    .glob("result.xlsx"))
    # 闸门② 真的解除了：cursor 照常推进到 2，批次能一路跑到终态。
    assert store.get_workflow(wf["id"])["cursor"] == 2


def test_writeback_on_writes_every_row_and_they_all_survive(disp, tmp_path):
    """打开开关：同样跑完 → 有 result.xlsx 且**每行都有值**（顺手覆盖 C-1）。"""
    from openpyxl import load_workbook
    uid = _user()
    wf = _workflow(uid, tmp_path, rows=3)
    assert store.get_workflow(wf["id"])["writeback"] == 1
    _run_and_archive_all(disp, wf, 3)
    result = store.get_workflow(wf["id"])["result_path"]
    assert result and Path(result).is_file()
    ws = load_workbook(Path(result))["S1"]
    assert [ws.cell(row=2 + i, column=4).value for i in range(3)] == \
        ["done", "done", "done"]


# --- F-1：夹具收尾不许放行没收完的活 ------------------------------------------


def test_shutdown_waits_for_the_archive_to_finish_before_returning(disp, tmp_path,
                                                                  monkeypatch):
    """围栏：**归档还在途就收尾，收尾必须等它做完**，做完之后一条线程都不剩。

    复审已用 threading.Event 确定性复现过原缺陷（`_archive_stop` 用时 2.31 秒就
    返回，线程仍活着；下一个测试的一号行任务被静默改成 ``workflow_archived=1``）。
    这里造的是同一个形状，**不用多进程、也不靠 sleep 碰运气**：

    - write_back 里用一个 Event 把归档线程按在原地，而**只有本用例会去 set 它**——
      「归档没做完」这个状态是夹具自己造出来的，与机器快慢无关；
    - 把 ``poll_interval`` 调成负数：``_archive_stop`` 的 join 预算
      ``poll_interval * 3 + 2.0`` 于是变负，等于**一秒都不等**。「等」与
      「不等」因此在毫秒级上分出胜负，不必真的耗掉那 5 秒。而正确的收尾靠
      :func:`_shutdown_archive` 自己那个真等待循环，不依赖这个被调小的预算，
      所以下面两条断言仍然是实的。

    判据是**收尾返回之后的状态**：没有归档线程存活，且那一行确实被归档完成了
    （等它做完，而不是把它扔下）。
    """
    import workflow as wfmod
    uid = _user()
    wf = _workflow(uid, tmp_path)
    disp._workflow_run_pass()
    r0 = _first_row(wf)
    store.update_task(r0["id"], status="done", summary="ok")

    entered = threading.Event()
    release = threading.Event()
    real = wfmod.write_back

    def blocked(*a, **k):
        entered.set()
        release.wait(30)
        return real(*a, **k)

    monkeypatch.setattr(wfmod, "write_back", blocked)
    disp._workflow_archive_pass()
    assert entered.wait(5), "归档线程根本没开工，这条用例测不到想测的东西"
    assert _archived(r0["id"]) == 0

    disp.poll_interval = -0.7          # join 预算 → -0.1：_archive_stop 不再等
    stopper = threading.Thread(target=_shutdown_archive, args=(disp,),
                               name="wf-teardown", daemon=True)
    stopper.start()
    # 归档还被 Event 按着，收尾此刻**不可能**返回。返回了就说明它把没做完的活
    # 扔下了——那条线程会活过夹具，去连下一个测试的库。
    # （判据用 is_alive()，不是 join 的返回值：Python 3 的 Thread.join 恒返回 None。）
    stopper.join(0.5)
    assert stopper.is_alive(), "收尾没等归档做完就返回了"
    assert _archived(r0["id"]) == 0, "归档还没放行，这一行就已经被置位了"

    release.set()
    stopper.join(30)
    assert not stopper.is_alive(), "收尾之后归档还没停"
    assert not [t.name for t in threading.enumerate()
                if t.name.startswith("wf-archive-") and t.is_alive()], \
        "收尾返回之后还有归档线程活着——它会去改下一个测试的库"
    assert _archived(r0["id"]) == 1, "收尾把没收完的活丢掉了（得等它做完）"

