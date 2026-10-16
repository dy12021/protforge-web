"""跨进程中止测试（dispatcher._reap / _abort_signal）。

**防的回归**：用户中止跑在 public app 那个进程里，只能靠数据库把意图传给调度器。
早年 :meth:`Dispatcher._reap` 靠 ``status == "failed"`` 猜——而 ``failed`` 是笼统终态，
打包失败、agent 异常、磁盘满都会写它。猜错的后果不是"少杀一个进程"，而是**原因文案
张冠李戴**：用户明明点的不是中止，详情页却写着"任务已被中止"；反过来，将来新增
``cancelled`` 之类表示中止的终态，这道猜法又整个漏掉。

判据改成"进终态那条 status 事件的 actor 是不是 human"，见 :func:`dispatcher._abort_signal`。

**不用 mock 进程**：起真的 Python 子进程（它自己再起一个孙进程持续写心跳），用
``poll()`` 与心跳证明进程真被杀了 / 真没被杀。只断言状态字段是恒真的——无论进程死活，
库里那个 ``failed`` 都长那样。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import auth
import db
import dispatcher
import store

# 假 agent：起一个孙进程持续写心跳（验"杀的是整棵树"），自己阻塞在 stdin 上。
FAKE_AGENT = r'''
import os, subprocess, sys

HB = os.environ.get("FAKE_AGENT_HB", "")
if HB:
    env = dict(os.environ, FAKE_AGENT_MODE="heartbeat")
    # 孙进程自己拿一份 stdout/stderr（DEVNULL），不继承 agent 的那两条管道：继承之后，
    # "只杀本体不杀树"这个回归会让孙进程一直活着并攥住 pytest 的 stdout 管道，
    # 那条用例变成**卡死**而不是失败——围栏要的是响亮地红，不是挂住。
    subprocess.Popen([sys.executable, "-c",
                      "import os,time\n"
                      "hb=os.environ['FAKE_AGENT_HB']\n"
                      "while True:\n"
                      "    open(hb,'a').write('x')\n"
                      "    time.sleep(0.1)\n"],
                     env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

sys.stderr.write("fake agent ready\n")
sys.stderr.flush()
for line in sys.stdin:          # 一直活到 stdin 被关掉
    pass
'''

WAIT = 20.0
POLL = 0.05

_BUILT: list = []


@pytest.fixture(autouse=True)
def _reap_agents():
    """收尸：断言失败也不能把进程留在后台跑。"""
    yield
    for d in list(_BUILT):
        for ap in list(d.running.values()):
            try:
                dispatcher.kill_tree(ap.proc)
                ap.proc.wait(timeout=5)
            except Exception:                     # noqa: BLE001
                try:
                    ap.proc.kill()
                except OSError:
                    pass
    _BUILT.clear()


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


@pytest.fixture
def disp(fake_agent, monkeypatch):
    d = dispatcher.Dispatcher(
        db,
        max_concurrency=2,
        node_exe=sys.executable,
        agent_script=str(fake_agent),
        gpu_probe=lambda: 2,
    )
    _BUILT.append(d)
    return d


def wait_for(pred, timeout=WAIT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(POLL)
    return pred()


def hb_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# --- 两个"别的进程"写入库里的那几行 ------------------------------------------------
#
# 原样抄 app.py 的端点：用户中止跑在 public app 那个进程里，调度器碰不到它的内存，
# 唯一信道就是数据库。测试也必须走这条信道——直接调 disp.abort() 是测不到跨进程的。

def abort_from_public_app(task_id: int) -> None:
    """抄 ``POST /api/tasks/{id}/abort``（server/app.py:371）。"""
    store.set_status(task_id, "failed", actor="human", message="用户中止了任务")
    store.update_task(task_id, error="用户中止了任务")


def fail_from_packaging(task_id: int, reason: str) -> None:
    """抄 ``app._fail_task``（server/app.py:500）：打包/收尾失败，**不是**中止。"""
    store.update_task(task_id, error=reason)
    store.set_status(task_id, "failed", actor="system", message=reason)
    store.add_event(task_id, "error", "system", reason)


def started(disp, task, hb):
    """接单并等孙进程的心跳起来，返回进程句柄。"""
    assert task["id"] in disp.tick(), "接单失败"
    ap = disp.running[task["id"]]
    assert store.get_task(task["id"], is_admin=True)["status"] == "running"
    assert wait_for(lambda: hb_size(hb) > 0), "孙进程没起来，树杀无从验证"
    return ap


# --- 正向：中止必须真的杀进程 --------------------------------------------------------

def test_cross_process_abort_kills_the_real_process_tree(disp, make_task, tmp_path,
                                                         monkeypatch):
    """防的回归：用户点了中止，调度器没看见，计算接着跑完一轮 GPU。

    断言的是**真进程**：``poll()`` 出退出码、孙进程心跳停。只查库里的 failed 是恒真的。
    """
    hb = tmp_path / "hb.txt"
    monkeypatch.setenv("FAKE_AGENT_HB", str(hb))
    task = make_task()
    ap = started(disp, task, hb)

    abort_from_public_app(task["id"])      # 另一个进程写库，调度器毫不知情
    disp.tick()                            # 下一轮收尾才认出来

    assert wait_for(lambda: ap.proc.poll() is not None), "中止后计算进程还活着"
    before = hb_size(hb)
    time.sleep(1.0)
    assert hb_size(hb) == before, "孙进程还在写心跳，只杀了 node 本体没杀树"

    row = store.get_task(task["id"], is_admin=True)
    assert row["status"] == "failed"
    # 原因文案得是人自己按下中止时的那句，不能被调度器的收尾改写成别的
    assert row["error"] == "用户中止了任务"
    assert any("已杀掉进程树" in e["message"] for e in store.list_events(task["id"]))


# --- 反向：非中止的失败不许被当成中止（缺陷 B 的护栏）--------------------------------

def test_non_abort_failure_is_not_treated_as_user_abort(disp, make_task, tmp_path,
                                                        monkeypatch):
    """防的回归：把别的失败误判成用户中止（缺陷 B 本体）。

    打包失败也是 ``failed``。早年 ``_reap`` 见 failed 就当中止，于是 ``_settle`` 走
    中止那条分支，写出"中止：已杀掉进程树"——用户点都没点过中止，详情页却说中止了，
    而且一个正在收尾的进程被提前杀掉。
    """
    hb = tmp_path / "hb.txt"
    monkeypatch.setenv("FAKE_AGENT_HB", str(hb))
    task = make_task()
    ap = started(disp, task, hb)

    fail_from_packaging(task["id"], "打包失败：磁盘空间不足")     # 系统写的失败
    disp.tick()

    row = store.get_task(task["id"], is_admin=True)
    assert row["error"] == "打包失败：磁盘空间不足", "失败原因被改写成中止文案"
    killed_notes = [e["message"] for e in store.list_events(task["id"])
                    if "已杀掉进程树" in e["message"] or "中止" in e["message"]]
    assert not killed_notes, f"非中止的失败被写成了中止：{killed_notes}"
    # 非中止的失败不该由 _reap 去杀进程：等它自己退出再按 exit code 收尾
    assert ap.proc.poll() is None, "仅凭 failed 就把进程杀了，中止判据还是笼统的"


def test_admin_status_event_is_not_an_abort_marker(disp, make_task, tmp_path, monkeypatch):
    """防的回归：'人写的' 被理解成 '带 human actor 的'。

    actor 是四个枚举值之一，除了中止还有别的事会写到人。将来多一条
    ``store.set_status(..., actor="human")`` 的路径时，中止判据必须仍然问的是
    **"进终态这条"**，而不是"历史上有没有人写过"。
    """
    hb = tmp_path / "hb.txt"
    monkeypatch.setenv("FAKE_AGENT_HB", str(hb))
    task = make_task()
    ap = started(disp, task, hb)

    # 人写了别的事（上传/改名之类），但进 failed 的是系统
    store.add_event(task["id"], "edited", "human", "用户改了标题")
    fail_from_packaging(task["id"], "计算进程异常结束（退出码 3）")

    disp.tick()

    assert ap.proc.poll() is None, "人改过标题就被当成用户中止了"
    assert store.get_task(task["id"], is_admin=True)["error"] == "计算进程异常结束（退出码 3）"


# --- 向后兼容与前向兼容 ---------------------------------------------------------------

def test_legacy_failed_task_with_abort_marker_is_still_recognised(disp, make_task,
                                                                  tmp_path, monkeypatch):
    """防的回归：判据一换，老任务的中止就认不出来了（向后兼容）。

    老任务的时间线形状是：人写的**进终态 status 事件**，后面还压着别的非 status 事件
    ——早年调度器收尾会补一条 "中止：已杀掉进程树" 的 log 事件，收尾器还会补 error。
    判据必须是"倒着找**最后一条进终态的 status 事件**"，而不是"最后一条事件"或
    "时间线上有人写过 human"。写错成后者的话，老任务升级后会把进程留在后台继续烧。
    """
    hb = tmp_path / "hb.txt"
    monkeypatch.setenv("FAKE_AGENT_HB", str(hb))
    task = make_task()
    ap = started(disp, task, hb)

    abort_from_public_app(task["id"])          # 人写的进终态事件
    # 后面压着非 status 事件：老服务收尾留下的痕迹
    store.add_event(task["id"], "log", "admin", f"中止：已杀掉进程树 pid={ap.pid}")
    store.add_event(task["id"], "error", "system", "用户中止了任务")

    assert dispatcher._abort_signal(task["id"], "failed") == "用户中止了任务"

    disp.tick()

    assert wait_for(lambda: ap.proc.poll() is not None), "带中止标记的老任务没被收尾"


def test_abort_signal_recognises_a_future_terminal_status(make_task, monkeypatch):
    """防的回归：判据把 failed 写死，将来新增 cancelled 之类的终态就漏掉中止。

    这是 :func:`dispatcher._abort_signal` 承诺的"不写死 failed"，直接对着
    db.TERMINAL_STATUSES 打一个新终态验。终态由 db 定，判据只问"最后那条进终态的
    事件是谁写的"，所以扩表时这里不用改。
    """
    task = make_task()
    task_id = task["id"]
    store.add_event(task_id, "status", "system", "接单", from_status="queued",
                    to_status="running")
    # 假想的将来：新增 cancelled 终态，中止端点照旧用 actor=human
    store.add_event(task_id, "status", "human", "用户中止了任务", from_status="running",
                    to_status="cancelled")
    monkeypatch.setattr(db, "TERMINAL_STATUSES", ("done", "failed", "cancelled"))

    assert dispatcher._abort_signal(task_id, "cancelled") == "用户中止了任务"

    # 对照组：同一个终态但由系统写入 → 不是中止
    task2 = make_task()
    store.add_event(task2["id"], "status", "system", "打包失败", from_status="running",
                    to_status="cancelled")
    assert dispatcher._abort_signal(task2["id"], "cancelled") is None


def test_abort_signal_returns_none_for_non_terminal_or_unmarked(make_task):
    """防的回归：判据越界，把还在跑的任务 / 没有进终态事件的任务当成中止。

    还在 running 的任务哪怕有人写过事件也不是中止（人改标题、上传文件都会留 human
    事件）；进终态却查不到事件的（老库、手工改库）宁可当成"不是中止"，也不能猜。
    """
    task = make_task()
    task_id = task["id"]
    store.add_event(task_id, "note", "human", "用户中止了任务")      # 只是条 note

    assert dispatcher._abort_signal(task_id, "running") is None
    assert dispatcher._abort_signal(task_id, "failed") is None

    store.set_status(task_id, "failed", actor="system", message="计算进程异常结束")
    assert dispatcher._abort_signal(task_id, "failed") is None


def test_abort_falls_back_to_readable_reason_when_marker_has_no_text(make_task):
    """防的回归：中止事件没有文案时，失败原因写成空串，详情页显示不出理由。"""
    task = make_task()
    store.add_event(task["id"], "status", "human", "", from_status="running",
                    to_status="failed")

    assert dispatcher._abort_signal(task["id"], "failed") == ""
    assert dispatcher.ABORT_FALLBACK_REASON == "用户中止了任务"
