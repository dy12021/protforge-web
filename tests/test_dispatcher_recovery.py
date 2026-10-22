"""调度器重启恢复测试（dispatcher.recover / 运行态记录）。

**防的回归**：``self._running`` 是纯内存表，进程一起就没了。上一条命接走的任务在
库里还是 ``running``，却再没有任何东西跟踪它——用户看到"永远在计算中"，而那些 node
agent 成了孤儿继续烧 GPU。所以 ``start()`` 必须先做一次恢复：验身份、杀整棵进程树、
落成带中文原因的终态。

这些测试**不跑真的 pi agent**，也不 mock 进程：起真的 Python 子进程（它自己再起一个
孙进程持续写心跳），用「心跳停没停」证明杀的是整棵树，用 ``poll()`` 证明进程真没了。
只断言最终状态是恒真的——库里那个 ``running`` 无论进程死活都长那样，所以每条测试都
同时盯住**恢复路径确实被调用过**（spy + last_recovery）与**真进程的生死**。
"""

from __future__ import annotations

import json
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

# 假 agent：起一个孙进程持续写心跳（用来验"杀的是整棵树"），自己阻塞在 stdin 上。
# 孙进程必须改 FAKE_AGENT_MODE 本身——写成 MODE= 会让它读到继承来的同名变量，
# 于是每个孙进程再起一个，那是 fork 炸弹不是进程树。
FAKE_AGENT = r'''
import os, subprocess, sys, time

HB = os.environ.get("FAKE_AGENT_HB", "")
if HB:
    env = dict(os.environ, FAKE_AGENT_MODE="heartbeat")
    # 孙进程**必须自己拿一份 stdout/stderr**（DEVNULL），不能继承 agent 的那两条管道：
    # 继承之后，"只杀本体不杀树"这个回归（去掉 /T）会让孙进程一直活着，把 pytest 进程
    # 的 stdout 管道攥在手里不放——那样这条用例是**卡死**而不是失败，围栏就废了。
    # 它只写心跳文件，不往 stdout 写任何东西，DEVNULL 不会丢掉任何可观测信号。
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
    """每个测试跑完把起过的进程全杀掉，不留孤儿。

    不这么做的话一次断言失败就会漏一个进程：假 agent 阻塞在 stdin 上，pytest 收工
    不管它，跑几轮就攒下一堆 python 在后台跑。
    """
    yield
    for d in list(_BUILT):
        for ap in list(d.running.values()):
            try:
                dispatcher.kill_tree(ap.proc)
                ap.proc.wait(timeout=5)
            except Exception:                     # noqa: BLE001 - 收尸失败不该让测试红
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
    # USERNAME_RE 要求 3-32 位，别用 "u1"
    return auth.create_user("user01", "password-123")["id"]


@pytest.fixture
def make_task(owner_id):
    def _make(title="t", brief="分析 1ubq"):
        return store.create_task(title, brief, owner_id)
    return _make


@pytest.fixture
def make_disp(fake_agent, monkeypatch):
    """构造一个调度器实例。每个实例都是一条独立的"命"——用来模拟服务重启。"""
    monkeypatch.delenv("FAKE_AGENT_MODE", raising=False)

    def _make(**kw):
        kw.setdefault("max_concurrency", 2)
        kw.setdefault("gpu_probe", lambda: 2)    # 测试不依赖本机有没有 nvidia-smi
        kw.setdefault("node_exe", sys.executable)
        kw.setdefault("agent_script", str(fake_agent))
        d = dispatcher.Dispatcher(**kw)
        _BUILT.append(d)
        return d
    return _make


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


def ticked_and_gone(d, task_id) -> bool:
    """扫一轮再问"这个任务还在不在跑"。必须先 tick 再判断，否则会短路。"""
    d.tick()
    return task_id not in d.running


def spawn_bystander():
    """起一个与调度器无关的长驻进程（代表"用户自己开的程序"）。"""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                            stdin=subprocess.DEVNULL, **kwargs)


# --- 恢复路径 ------------------------------------------------------------------------

def test_start_invokes_recovery_before_polling(make_disp, monkeypatch):
    """防的回归：恢复被漏接在 start() 上（或接在起线程之后）。

    只断言「start 之后任务被收尾」不够——那条终态也可能被别的路径写出来。所以这里
    直接盯 ``recover`` 有没有被 start 调用，并核对调用发生在后台线程起跑之前：
    顺序反了的话，新任务会先占满并发，恢复被排在后面。
    """
    d = make_disp()
    seen = []
    orig = dispatcher.Dispatcher.recover

    def spy(self):
        seen.append({"running": dict(self.running), "thread": self._thread})
        return orig(self)

    monkeypatch.setattr(dispatcher.Dispatcher, "recover", spy)
    try:
        d.start()
    finally:
        d.stop()

    assert seen, "start() 没有调用 recover()，重启后遗留任务永远没人收"
    assert seen[0]["thread"] is None, "恢复必须在起后台线程之前跑"


def test_recover_settles_stale_running_task_and_kills_orphan_tree(
        make_disp, make_task, tmp_path, monkeypatch):
    """防的回归（缺陷 A 本体）：重启后 running 任务卡死 + 孤儿空烧 GPU。

    一条完整的链路：上一条命真的派出去一个带孙进程的 agent（库里 running、pid 活着、
    心跳在跳）→ 构造**新实例**当重启（``_running`` 是空的，跟真实重启一样看不见那个
    pid）→ 恢复 → 断言真进程真死了、孙进程心跳真停了、状态落成 failed 且带中文原因、
    运行态记录被销掉。
    """
    hb = tmp_path / "hb.txt"
    monkeypatch.setenv("FAKE_AGENT_HB", str(hb))

    d1 = make_disp()
    task = make_task()
    assert task["id"] in d1.tick(), "接单失败，后面就没得测了"
    ap = d1.running[task["id"]]
    assert store.get_task(task["id"], is_admin=True)["status"] == "running"
    assert wait_for(lambda: hb_size(hb) > 0), "孙进程没起来，树杀无从验证"

    record = dispatcher._record_path(d1.run_dir, task["id"])
    assert record.is_file(), "派活后没落运行态记录，重启就没有凭据可查"
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert rec["pid"] == ap.pid and rec["run_id"]
    assert rec["start_epoch"], "记录里没有进程创建时刻，重启就无法区分 PID 复用"

    # --- 服务重启：全新实例，看不见那个 pid ---
    d2 = make_disp()
    assert task["id"] not in d2.running, "新实例不该继承内存表"

    calls = []
    orig = dispatcher.Dispatcher._recover_one

    def spy(self, task_id, record_, summary):
        calls.append(task_id)
        return orig(self, task_id, record_, summary)

    monkeypatch.setattr(dispatcher.Dispatcher, "_recover_one", spy)
    summary = d2.recover()

    # 1) 恢复路径确实被调用过（不是"状态碰巧对"）
    assert calls == [task["id"]], f"恢复路径没被调用：{calls}"
    assert summary is d2.last_recovery
    assert task["id"] in summary["settled"]
    assert task["id"] in summary["killed"]

    # 2) 真进程真死了（不是只改了库）
    assert wait_for(lambda: ap.proc.poll() is not None), "残留进程还活着，还在烧 GPU"

    # 3) 杀的是整棵树：孙进程心跳必须停
    #    **必须轮询等它真的停，不能只睡一秒就断言。** kill_tree 杀整棵树要
    #    一点时间，机器一忙一秒就不够——于是这条在全量跑里偶发误红（单独跑
    #    从来不红）。判据是「心跳停止增长」，不是「一秒钟后没增长」。
    deadline = time.time() + 15.0
    last = hb_size(hb)
    quiet = 0.0
    while time.time() < deadline:
        time.sleep(0.3)
        now = hb_size(hb)
        if now == last:
            quiet += 0.3
            if quiet >= 1.2:
                break
        else:
            quiet = 0.0
            last = now
    assert hb_size(hb) == last, "孙进程还在写心跳，只杀了 node 本体没杀树"

    # 4) 状态是带中文原因的可解释终态，不是静默改状态
    row = store.get_task(task["id"], is_admin=True)
    assert row["status"] == "failed"
    assert "服务重启" in row["error"] and "计算进程已中断" in row["error"]
    assert str(ap.pid) in row["error"], "原因里应点名那个 pid，便于对账"
    assert any("服务重启" in e["message"] for e in store.list_events(task["id"]))

    # 5) 记录销掉：下一次启动不该再追一个早退出的 pid
    assert not record.exists()


def test_recover_leaves_pid_reused_by_unrelated_program_alone(make_disp, make_task,
                                                             monkeypatch):
    """防的回归：PID 复用被误杀。

    pid 会被系统很快复用，一个睡了几小时的孤儿号很可能已经被用户自己开的程序占着。
    恢复时若只看 pid 就 taskkill，等于替用户杀掉无关程序。造法：库里 running 任务的
    记录指向一个**活着但不是我们起的**进程，且它的创建时刻对不上记录。
    """
    bystander = spawn_bystander()
    try:
        d = make_disp()
        task = make_task()
        store.set_status(task["id"], "running", actor="system", message="派出去算")

        fp = dispatcher._proc_fingerprint(bystander.pid)
        assert fp is not None, "探不到旁证进程的指纹，测不了 PID 复用这道闸"
        d.run_dir.mkdir(parents=True, exist_ok=True)
        dispatcher._record_path(d.run_dir, task["id"]).write_text(json.dumps({
            "task_id": task["id"],
            "run_id": "run-reused",
            "pid": bystander.pid,
            "argv": [sys.executable, "run.mjs"],
            "node_exe": sys.executable,
            "image": fp[0],
            # 伪造一个早得多的创建时刻：等价于"这个号早被别的进程用过一轮"
            "start_epoch": (fp[1] or time.time()) - 3600.0,
        }, ensure_ascii=False), encoding="utf-8")

        summary = d.recover()

        assert task["id"] in summary["settled"], "任务本身仍要收尾，别让它卡在 running"
        assert task["id"] not in summary["killed"], "身份对不上却动手杀了"
        assert bystander.poll() is None, "PID 被复用后误杀了无关程序"
        row = store.get_task(task["id"], is_admin=True)
        assert row["status"] == "failed"
        assert "服务重启" in row["error"]
    finally:
        try:
            bystander.kill()
            bystander.wait(timeout=5)
        except OSError:
            pass


def test_recover_skips_when_another_instance_holds_the_claim(make_disp, make_task,
                                                             tmp_path, monkeypatch):
    """防的回归：两个调度器实例同时启动，收尾同一个 pid、各杀一次各写一条。

    认领锁（``O_CREAT|O_EXCL``）就是干这个的，与 ``store.claim_queued`` 那句原子
    UPDATE 同源。这里造"另一个实例正在收尾"：把锁文件先占住，再跑恢复。
    """
    hb = tmp_path / "hb.txt"
    monkeypatch.setenv("FAKE_AGENT_HB", str(hb))
    d1 = make_disp()
    task = make_task()
    d1.tick()
    ap = d1.running[task["id"]]

    rec = json.loads(dispatcher._record_path(d1.run_dir, task["id"])
                     .read_text(encoding="utf-8"))

    d2 = make_disp()
    d2.run_dir.mkdir(parents=True, exist_ok=True)
    claim = dispatcher._claim_path(d2.run_dir, task["id"], rec["run_id"])
    claim.write_text("99999", encoding="utf-8")        # 别的实例抢到了

    summary = d2.recover()

    assert task["id"] in summary["left_alone"], "抢不到认领锁就该让开"
    assert task["id"] not in summary["settled"]
    assert ap.proc.poll() is None, "让开了却还是把别人的 pid 杀了"
    assert store.get_task(task["id"], is_admin=True)["status"] == "running"
    assert dispatcher._record_path(d1.run_dir, task["id"]).is_file()


def test_recover_does_not_overwrite_task_already_settled(make_disp, make_task, tmp_path,
                                                        monkeypatch):
    """防的回归：恢复盖掉别人的结论。

    遗留记录不等于"现在还在 running"：用户中止、打包失败、上一条命自己收尾，都会
    让库里先变成终态。恢复必须只在**库里仍是 running** 时才动手，且不许覆盖已有
    error 文案——用户在详情页看到的应该是中止/失败自己的话。
    """
    hb = tmp_path / "hb.txt"
    monkeypatch.setenv("FAKE_AGENT_HB", str(hb))
    d1 = make_disp()
    task = make_task()
    d1.tick()
    ap = d1.running[task["id"]]

    # 模拟 public app 的中止端点：库里已是 failed
    store.set_status(task["id"], "failed", actor="human", message="用户中止了任务")
    store.update_task(task["id"], error="用户中止了任务")

    d2 = make_disp()
    summary = d2.recover()

    assert task["id"] in summary["left_alone"]
    assert task["id"] not in summary["killed"], "任务已终态却还去杀它的进程"
    row = store.get_task(task["id"], is_admin=True)
    assert row["error"] == "用户中止了任务", "恢复覆盖了中止的原因文案"
    assert ap.proc.poll() is None, "进程不该由恢复来杀，交给还在跟踪它的那个实例"


def test_recover_survives_broken_record_and_db_unreachable(make_disp, make_task,
                                                           tmp_path, monkeypatch):
    """防的回归：善后把服务启动搞挂。

    记录认不出来、库读不到、taskkill 失败——都只记日志。平台起不来的代价比多一个
    孤儿大得多：用户至少还能进来看任务列表、重新提交。
    """
    hb = tmp_path / "hb.txt"
    monkeypatch.setenv("FAKE_AGENT_HB", str(hb))
    d1 = make_disp()
    task = make_task()
    d1.tick()
    assert dispatcher._record_path(d1.run_dir, task["id"]).is_file()

    # 再掺两份认不出来的记录：它们必须被跳过而不是抛异常
    (d1.run_dir / "not-json.json").write_text("{这不是 JSON", encoding="utf-8")
    (d1.run_dir / "999999.json").write_text('{"task_id": "不是数字"}', encoding="utf-8")

    real_db_status = dispatcher._db_status

    def boom(task_id):
        raise RuntimeError("库不可达")

    # 换实例 = 换一条命：这个实例的 running 表是空的，才轮到恢复来收
    d = make_disp()
    monkeypatch.setattr(dispatcher, "_db_status", boom)
    summary = d.recover()          # 不能抛

    assert summary["candidates"] == 1, "只有那条真记录算候选，坏的两份该被跳过"
    assert task["id"] in summary["errors"], "库不可达那条应记账而不是静默"
    assert task["id"] not in summary["settled"]

    # 坏记录也不能让启动失败
    monkeypatch.setattr(dispatcher, "_db_status", real_db_status)
    d2 = make_disp()
    try:
        d2.start()
        assert d2._thread is not None and d2._thread.is_alive()
    finally:
        d2.stop()


def test_run_record_removed_when_agent_exits_normally(make_disp, make_task, tmp_path,
                                                     monkeypatch):
    """防的回归：收尾不销记录，下次启动去追一个早就退出的 pid。

    pid 早就被系统回收并复用，甚至同一个号又被派了出去——追它等于杀无辜进程或砸掉
    新任务。所以"收尾"必须删记录，这是恢复能安全成立的前提。
    """
    d = make_disp()
    task = make_task()
    d.tick()
    ap = d.running[task["id"]]
    record = dispatcher._record_path(d.run_dir, task["id"])
    assert record.is_file()

    store.record_finished(task["id"])           # agent 收口
    ap.proc.stdin.close()                       # 让它自己退出
    assert wait_for(lambda: ticked_and_gone(d, task["id"]))
    assert not record.exists(), "进程收尾后运行态记录还在，下次启动会去追一个死 pid"
    assert store.get_task(task["id"], is_admin=True)["status"] == "packaging"


def test_run_record_absent_before_any_dispatch(make_disp):
    """防的回归：恢复在没有遗留记录**也没有 running 行**时乱动。

    库里没有 running、磁盘没有记录 → 恢复必须是零成本的空转，不能凭空造事件。
    """
    d = make_disp()
    summary = d.recover()
    assert summary == {"candidates": 0, "settled": [], "killed": [],
                       "left_alone": [], "errors": [], "no_record": 0}
    assert not d.run_dir.exists() or not list(d.run_dir.glob("*.json"))


# ------------------------------------- 库里 running、磁盘没有记录（补扫那一路）


def test_running_without_record_is_settled_not_left_stuck(make_disp, make_task):
    """库里 running + 磁盘无记录 = 永久卡死，必须被补扫收尾。

    这一路原先完全没有：`recover()` 只遍历 `run/<id>.json`，而 pid 只存在那儿，
    于是没有记录的任务被整轮漏掉——用户永远看着"计算中"。
    两个真实来源：派活途中在 claim_queued 与写记录之间崩了；
    以及本次修复**之前**派出去的老任务（实测本机数据盘上就有四条）。

    这一路**不杀任何进程**：没有记录就没有 pid，猜 pid 去杀，撞上 PID 复用
    就是在杀无关程序。所以只收状态，并把"杀不了"如实写进原因里。
    """
    d = make_disp()
    task = make_task()
    store.set_status(task["id"], "running", actor="system")
    assert not dispatcher._record_path(d.run_dir, task["id"]).exists()

    summary = d.recover()

    after = store.get_task(task["id"], is_admin=True)
    assert after["status"] == "failed", "无记录的任务被留在 running 上，永久卡死"
    assert summary["no_record"] == 1, summary
    assert summary["killed"] == [], "没有 pid 就不该声称杀过任何东西"
    assert "运行记录" in (after["error"] or ""), after["error"]
    assert "无法确认计算进程" in (after["error"] or ""), \
        "杀不了 pid 这件事要如实告诉用户，不能让人以为进程被清了"
    # 不能凭空说成崩溃——那是一条不存在的事故
    texts = [e["message"] for e in store.list_events(task["id"])]
    assert not any("异常结束" in t for t in texts), texts


def test_recordless_sweep_leaves_untouched_tasks_alone(make_disp, make_task):
    """补扫只动 running；queued / done / failed 一律不碰。"""
    d = make_disp()
    queued = make_task()
    done = make_task()
    store.set_status(done["id"], "running", actor="system")
    store.set_status(done["id"], "done", actor="system")

    summary = d.recover()

    assert store.get_task(queued["id"], is_admin=True)["status"] == "queued"
    assert store.get_task(done["id"], is_admin=True)["status"] == "done"
    assert summary["no_record"] == 0, summary


def test_recordless_sweep_releases_its_claim(make_disp, make_task):
    """补扫抢到的认领锁必须被还回去。

    这条来自真机跑：起一次服务收掉四条卡住的 running 任务之后，run/ 里留下
    四个 `*-no-record.claim` 没人删（run/ 不参与 janitor 清扫）。对这一路来说
    残留不是"垃圾文件"那么轻——run_id 是固定哨兵，锁在就意味着**这个 task_id
    永远进不了补扫**。同一个任务第二次以同样方式卡住时就再也扫不掉了，
    正是这个功能要根治的症状换了个形式回来。
    """
    d = make_disp()
    task = make_task()
    store.set_status(task["id"], "running", actor="system")

    d.recover()

    claim = dispatcher._claim_path(d.run_dir, task["id"], dispatcher._NO_RECORD)
    assert not claim.exists(), f"认领锁没还回去：{claim.name}"


def test_recordless_task_can_be_swept_twice(make_disp, make_task):
    """同一条无记录任务连扫两次都能收尾——残锁挡不住第二次。

    这是上一条的行为后果：管理员重跑后再次卡住、库里又出现 running + 无记录时，
    补扫必须还能再收一次。
    """
    d = make_disp()
    task = make_task()
    store.set_status(task["id"], "running", actor="system")
    first = d.recover()
    assert first["no_record"] == 1, first

    # 模拟管理员重跑：failed -> queued -> running，磁盘上依旧没有记录
    store.set_status(task["id"], "queued", actor="admin", message="重跑")
    store.set_status(task["id"], "running", actor="system")

    second = d.recover()

    assert second["no_record"] == 1, (
        f"第二次补扫被上一次的残锁挡住了：{second}")
    assert store.get_task(task["id"], is_admin=True)["status"] == "failed"


def test_recordless_sweep_yields_when_another_instance_holds_the_claim(
        make_disp, make_task):
    """两个实例同时启动，只有一个能收尾那条无记录任务。"""
    d1, d2 = make_disp(), make_disp()
    task = make_task()
    store.set_status(task["id"], "running", actor="system")
    assert dispatcher._claim(d1.run_dir, task["id"], dispatcher._NO_RECORD)

    summary = d2.recover()

    assert summary["no_record"] == 0, summary
    assert summary["left_alone"] == [task["id"]], summary
    assert store.get_task(task["id"], is_admin=True)["status"] == "running"
