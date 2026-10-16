"""调度器测试（dispatcher.py）。

**不跑真的 pi agent**——它重且慢，跑一次几小时。这里用一个 tmp_path 里的假
agent（Python 写的 JSON echo 进程）顶替，它把调度器的四条契约都走了一遍：

1. 四个环境变量确实注进去了，且 **token 不在 argv 里**；
2. stdin 写 JSON 命令 → stdout 回一行 JSON 响应（RPC 往返）；
3. stdout 的事件流能被 ``stream()`` 读到；
4. 进程退出后按 ``store.has_finished`` 分流成 packaging / failed。

中止那条用**真的进程树**：假 agent 自己再起一个孙进程持续写心跳文件，
``abort()`` 之后心跳必须停——停不了就说明只杀了直接子进程。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import auth
import db
import dispatcher
import store

# 假 agent：stdin 收一行 JSON 命令、stdout 回一行 JSON 响应。
# 它读四个环境变量证明注入生效，再按 FAKE_AGENT_MODE 决定演哪一出。
FAKE_AGENT = r'''
import json, os, subprocess, sys, time

MODE = os.environ.get("FAKE_AGENT_MODE", "echo")
HB = os.environ.get("FAKE_AGENT_HB", "")


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def hello():
    emit({
        "type": "event",
        "event": "hello",
        "task_id": os.environ.get("PROTFORGE_TASK_ID", ""),
        "internal": os.environ.get("PROTFORGE_WEB_INTERNAL", ""),
        "work_dir": os.environ.get("PROTFORGE_WORK_DIR", ""),
        "argv": sys.argv,
        # 只报名字与长度，不把令牌本身写进日志
        "env_keys": sorted(k for k in os.environ if k.startswith("PROTFORGE_")),
        "token_len": len(os.environ.get("PROTFORGE_AGENT_TOKEN", "")),
    })


if MODE == "heartbeat":
    # 孙进程：只写心跳，用来验"杀的是整棵树"
    while True:
        with open(HB, "a") as f:
            f.write("x")
        time.sleep(0.1)

if MODE == "tree":
    # 孙进程：必须改的是 FAKE_AGENT_MODE 本身。写成 MODE= 会让孙进程读到继承来的
    # "tree"，于是每个孙进程再起一个——那是 fork 炸弹，不是进程树。
    # stdin 必须给 DEVNULL：实测在 Windows 上继承父进程那条 stdin 管道会让孙进程
    # 起来就卡住（心跳一个字都不写，也不报错）。反正它只写文件，不读 RPC 输入。
    env = dict(os.environ, FAKE_AGENT_MODE="heartbeat")
    subprocess.Popen([sys.executable, os.path.abspath(__file__)], env=env,
                     stdin=subprocess.DEVNULL)

hello()

if MODE == "exit_clean":
    sys.exit(0)

if MODE == "exit_abnormal":
    sys.stderr.write("fake agent: 炸了\n")
    sys.stderr.flush()
    os._exit(3)

# echo：一直服务到 stdin 关闭
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        cmd = json.loads(line)
    except ValueError:
        emit({"type": "response", "id": None, "ok": False, "error": "非法 JSON"})
        continue
    emit({"type": "response", "id": cmd.get("id"), "ok": True,
          "result": {"echo": cmd, "task_id": os.environ.get("PROTFORGE_TASK_ID", "")}})
'''

WAIT = 15.0          # 给进程留的等待上限，CI 慢的时候不至于假失败
POLL = 0.05


def wait_for(pred, timeout=WAIT):
    """轮询到条件成立。返回是否成立，失败时断言方自己给上下文。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(POLL)
    return pred()


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


#: 本模块起过的 Dispatcher。每个测试跑完统一收尸（见 _reap_agents）。
_BUILT: list = []


@pytest.fixture(autouse=True)
def _reap_agents():
    """每个测试跑完，把本模块起的假 agent 全杀掉。

    不这么做的话，**一次断言失败就会漏一个进程**：假 agent 阻塞在 stdin 上，
    pytest 收工不管它，跑几轮就攒下一堆 python 在后台跑。tree 模式更糟——
    孙进程会变成没人认领的孤儿。
    """
    yield
    for d in list(_BUILT):
        for ap in list(d.running.values()):
            try:
                dispatcher.kill_tree(ap.proc)      # 先杀树
                ap.proc.wait(timeout=5)
            except Exception:                     # noqa: BLE001 - 收尸失败不该让测试红
                try:
                    ap.proc.kill()
                except OSError:
                    pass
    _BUILT.clear()


@pytest.fixture
def make_disp():
    """统一构造入口：默认用假 agent + Python 解释器当 node，并登记以便收尸。"""
    def _make(script, **kw):
        kw.setdefault("max_concurrency", 2)
        kw.setdefault("gpu_probe", lambda: 2)    # 不依赖本机有没有 nvidia-smi
        kw.setdefault("node_exe", sys.executable)
        kw.setdefault("agent_script", str(script))
        d = dispatcher.Dispatcher(**kw)
        _BUILT.append(d)
        return d
    return _make


@pytest.fixture
def disp(fake_agent, monkeypatch):
    monkeypatch.delenv("FAKE_AGENT_MODE", raising=False)
    # 计划里的签名就是 Dispatcher(db, data_root)，这里按位置传，钉住它别改坏
    d = dispatcher.Dispatcher(
        db,
        max_concurrency=2,
        node_exe=sys.executable,
        agent_script=str(fake_agent),
        gpu_probe=lambda: 2,     # 探测固定 2，测试不依赖本机有没有 nvidia-smi
    )
    _BUILT.append(d)
    return d


# --- 注入：token 走环境变量，绝不进 argv ---------------------------------------------

def test_spawn_injects_token_via_env_not_argv(disp, tmp_path):
    """计划里的原样断言，另加一条 argv 长度断言：argv 里不许有别的东西。"""
    ap = disp._spawn(1, "brief", tmp_path / "work")

    assert "PROTFORGE_AGENT_TOKEN" in ap.env
    assert not any("PROTFORGE_AGENT_TOKEN" in str(a) for a in ap.argv)
    assert ap.argv == [sys.executable, ap.argv[1]]
    assert len(ap.argv) == 2

    disp.abort(1)


def test_spawn_env_carries_internal_port_task_and_work_dir(disp, tmp_path):
    work = tmp_path / "work" / "7"
    ap = disp._spawn(7, "brief", work)

    assert ap.env["PROTFORGE_WEB_INTERNAL"] == dispatcher.DEFAULT_INTERNAL_BASE
    assert ap.env["PROTFORGE_WEB_INTERNAL"] == "http://127.0.0.1:8475"
    assert ap.env["PROTFORGE_TASK_ID"] == "7"
    assert ap.env["PROTFORGE_WORK_DIR"] == str(work)
    assert ap.token and ap.token in ap.env["PROTFORGE_AGENT_TOKEN"]
    assert work.is_dir()                       # 调度器负责建工作区

    disp.abort(7)


def test_spawned_agent_receives_env_over_the_pipe(disp):
    """env 不只是写进了 dict：子进程自己也能读到。"""
    ap = disp._spawn(3, "brief", disp.data_root / "work" / "3")
    ev = next(disp.stream(3, timeout=WAIT))

    assert ev["task_id"] == "3"
    assert ev["internal"] == "http://127.0.0.1:8475"
    assert "PROTFORGE_AGENT_TOKEN" in ev["env_keys"]
    assert ev["token_len"] == len(ap.token)
    assert not any("PROTFORGE_AGENT_TOKEN" in str(a) for a in ev["argv"])

    disp.abort(3)


# --- agent_token：一次性且与 task_id 绑定 --------------------------------------------

def test_agent_token_is_bound_to_task_id_and_revoked_on_exit(disp, tmp_path, make_task, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_clean")
    t = make_task()
    ap = disp._spawn(t["id"], "brief", tmp_path / "work")

    assert dispatcher.verify_agent_token(t["id"], ap.token)
    assert not dispatcher.verify_agent_token(t["id"] + 1, ap.token)   # 不能碰别的任务
    assert not dispatcher.verify_agent_token(t["id"], "wrong-token")
    assert not dispatcher.verify_agent_token(t["id"], "")

    ap.proc.wait(timeout=WAIT)
    disp.tick()                                 # 收尾 → 令牌作废
    assert not dispatcher.verify_agent_token(t["id"], ap.token)


def test_agent_token_dies_with_the_token_store(disp, tmp_path, make_task):
    """令牌只活在内存表里：换个表（等价于进程重启）就查不到。

    同一进程内两个 Dispatcher **共用**同一张表（模块级单例），所以这里拿新表来
    比，而不是拿新 Dispatcher——后者会给人"每个实例一张表"的错觉。
    """
    t = make_task()
    ap = disp._spawn(t["id"], "brief", tmp_path / "work")
    assert dispatcher.verify_agent_token(t["id"], ap.token)

    rebooted = dispatcher._TokenStore()
    assert not rebooted.verify(t["id"], ap.token)

    disp.abort(t["id"])


# --- RPC 与事件流 --------------------------------------------------------------------

def test_rpc_roundtrip_returns_matching_response(disp):
    ap = disp._spawn(11, "brief", disp.data_root / "work" / "11")
    next(disp.stream(11, timeout=WAIT))          # 等 agent 起来

    resp = disp.rpc(11, {"op": "steer", "text": "改用 AlphaFold3"}, timeout=WAIT)

    assert resp["ok"] is True
    assert resp["result"]["echo"]["op"] == "steer"
    assert resp["result"]["echo"]["text"] == "改用 AlphaFold3"
    # 信封里的 task_id 是 JSON 数字（env 里那个才是字符串，agent 侧别混着用）
    assert resp["result"]["echo"]["task_id"] == 11
    assert ap.last_rpc_id in str(resp["id"])      # 响应 id 对得上发出的那条

    disp.abort(11)


def test_rpc_to_dead_agent_raises(disp, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_clean")
    ap = disp._spawn(12, "brief", disp.data_root / "work" / "12")
    ap.proc.wait(timeout=WAIT)

    with pytest.raises(dispatcher.AgentGone):
        disp.rpc(12, {"op": "steer", "text": "还活着吗"}, timeout=3)

    disp.tick()


def test_stream_yields_events_then_stops_at_exit(disp, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_clean")
    ap = disp._spawn(13, "brief", disp.data_root / "work" / "13")

    events = list(disp.stream(13, timeout=WAIT))       # 进程退出后生成器自己收尾
    ap.proc.wait(timeout=WAIT)

    assert [e["event"] for e in events] == ["hello"]
    # 事件留在缓冲里给 SSE 重连用（不是消费即删），但 stdout 必须读到 EOF，
    # 否则 stream() 会永远挂着、管理员侧 SSE 连不上。
    assert wait_for(lambda: ap.eof, timeout=WAIT)
    assert wait_for(lambda: not ap.reader.is_alive(), timeout=WAIT)
    # 游标语义：带上 seq 重连不会重复推
    assert list(disp.stream(13, after=events[-1]["seq"], timeout=1)) == []
    assert [e["seq"] for e in disp.stream(13, timeout=1)] == [events[-1]["seq"]]


def test_stream_unknown_task_raises(disp):
    with pytest.raises(KeyError):
        list(disp.stream(999, timeout=1))


# --- 收尾分流：核心正确性 --------------------------------------------------------------

def test_finished_process_transitions_to_packaging(disp, monkeypatch, make_task):
    """agent 调过 /finish（store.record_finished）后退出 → 正常收口，进 packaging。"""
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_clean")
    t = make_task()

    assert disp.tick() == [t["id"]]             # 接走排队任务
    ap = disp.running[t["id"]]
    ap.proc.wait(timeout=WAIT)
    store.record_finished(t["id"])              # 内部 API 收到 finish 回调时写的
    disp.tick()                                 # 第二轮：收尾

    task = store.get_task(t["id"])
    assert task["status"] == "packaging"
    assert task["error"] == ""


def test_process_exit_without_finished_mark_is_failed(disp, monkeypatch, make_task):
    """没有 finished 标记就退出 → 判异常，进 failed，且不留半截 packaging。"""
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_abnormal")
    t = make_task()

    disp.tick()
    ap = disp.running[t["id"]]
    ap.proc.wait(timeout=WAIT)
    disp.tick()

    task = store.get_task(t["id"])
    assert task["status"] == "failed"
    assert task["error"] != ""
    assert "3" in task["error"] or "退出" in task["error"]

    kinds = [(e["kind"], e["to_status"]) for e in store.list_events(t["id"])]
    assert ("status", "failed") in kinds
    assert ("status", "packaging") not in kinds
    # 非零退出码要能在事件里查到，否则管理员看到 failed 也不知道为什么
    assert any(e["kind"] in ("error", "log") for e in store.list_events(t["id"]))


def test_settle_is_idempotent(disp, monkeypatch, make_task):
    """收尾跑两次不能插两条事件，也不能把 failed 又翻回 packaging。"""
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_abnormal")
    t = make_task()
    disp.tick()
    disp.running[t["id"]].proc.wait(timeout=WAIT)
    disp.tick()
    first = len(store.list_events(t["id"]))

    disp.tick()
    disp.tick()

    assert len(store.list_events(t["id"])) == first
    assert store.get_task(t["id"])["status"] == "failed"


# --- 中止：必须杀整棵进程树 ------------------------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="Windows 专属：验 taskkill /T 杀进程树")
def test_kill_tree_invokes_taskkill_with_T_and_F(disp, monkeypatch, tmp_path, make_task):
    """只验**发了什么命令**：taskkill 被打了桩，所以这一条里进程是真没被杀干净的，
    收尾靠 monkeypatch 撤销后由 _reap_agents 兜底。
    """
    seen = {}
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).lower().startswith("taskkill"):
            seen["cmd"] = list(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(dispatcher.subprocess, "run", fake_run)
    t = make_task()
    ap = disp._spawn(t["id"], "brief", tmp_path / "work")
    try:
        disp.abort(t["id"])

        assert seen.get("cmd"), "必须走 taskkill，terminate() 杀不掉 docker/wsl 孙进程"
        assert "/T" in seen["cmd"] and "/F" in seen["cmd"]
        assert str(ap.pid) in seen["cmd"]
    finally:
        monkeypatch.undo()                        # 桩撤了，这才能真杀
        dispatcher.kill_tree(ap.proc)
        ap.proc.wait(timeout=WAIT)


@pytest.mark.skipif(os.name != "nt", reason="真进程树测试，跨平台语义不同")
def test_abort_kills_grandchild_too(disp, monkeypatch, tmp_path, make_task):
    """假 agent 起的孙进程（心跳）也必须死——这是 taskkill /T 的意义。"""
    monkeypatch.setenv("FAKE_AGENT_MODE", "tree")
    hb = tmp_path / "heartbeat"
    monkeypatch.setenv("FAKE_AGENT_HB", str(hb))
    t = make_task()

    disp.tick()
    ap = disp.running[t["id"]]
    assert wait_for(lambda: hb.exists() and hb.stat().st_size > 0), "孙进程没起来"

    assert disp.abort(t["id"]) is True
    assert ap.proc.wait(timeout=WAIT) is not None

    time.sleep(0.6)                              # 让心跳涨一涨做基线
    base = hb.stat().st_size if hb.exists() else 0
    time.sleep(1.5)
    assert (hb.stat().st_size if hb.exists() else 0) == base, "孙进程还活着：进程树没杀干净"

    assert store.get_task(t["id"])["status"] == "failed"
    assert disp.running == {}


def test_abort_queued_task_cancels_without_spawning(disp, make_task):
    t = make_task()
    assert disp.abort(t["id"]) is True           # 还在排队，没进程可杀

    assert store.get_task(t["id"])["status"] == "failed"
    disp.tick()
    assert disp.running == {}                    # 不会再被接走


def test_abort_unknown_task_returns_false(disp):
    assert disp.abort(4242) is False


# --- 并发上限 = GPU 数 -----------------------------------------------------------------

def test_concurrency_limit_follows_gpu_probe(fake_agent, make_disp):
    d = make_disp(fake_agent, gpu_probe=lambda: 4, max_concurrency=None)
    assert d.limit == 4


def test_concurrency_limit_falls_back_when_probe_fails(fake_agent, make_disp):
    """nvidia-smi 不存在/坏了不能起不来：退回默认值 1。"""
    d = make_disp(fake_agent, gpu_probe=lambda: 0, max_concurrency=None)
    assert d.limit == dispatcher.DEFAULT_MAX_CONCURRENCY == 1


def test_manual_limit_only_ever_lowers(fake_agent, make_disp):
    """手动配置只能下调：配大了按探测值收，避免加卡后忘了改配置就超卖。"""
    down = make_disp(fake_agent, gpu_probe=lambda: 4, max_concurrency=2)
    assert down.limit == 2
    too_big = make_disp(fake_agent, gpu_probe=lambda: 2, max_concurrency=8)
    assert too_big.limit == 2


def test_tick_admits_up_to_limit_and_keeps_the_rest_queued(fake_agent, make_disp, monkeypatch, owner_id):
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit_clean")
    d = make_disp(fake_agent, gpu_probe=lambda: 2, max_concurrency=2)
    ids = [store.create_task(f"t{i}", "brief", owner_id)["id"] for i in range(4)]

    admitted = d.tick()
    try:
        assert admitted == ids[:2]               # 先进先出，接 2 个
        assert len(d.running) == 2
        assert store.get_task(ids[2])["status"] == "queued"
        assert store.get_task(ids[0])["status"] == "running"
    finally:
        d.stop()


def test_spawn_failure_marks_task_failed(disp, tmp_path, make_task, monkeypatch):
    """起不来（脚本不存在）不能把任务永远卡在 queued。"""
    t = make_task()

    def boom(task_id, brief, work_dir):
        raise FileNotFoundError("node 不存在")

    monkeypatch.setattr(disp, "_spawn", boom)
    disp.tick()

    task = store.get_task(t["id"])
    assert task["status"] == "failed"
    assert task["error"] != ""


# --- SSE 交接：写完事件必须叫醒总线 ----------------------------------------------------

def test_settle_wakes_sse_bus(disp, monkeypatch, make_task):
    """Task 6 的交接项：调度器写完事件要调 sse.notify()，否则这条事件要等 3 秒轮询。"""
    calls = []
    monkeypatch.setattr(dispatcher.sse, "notify", lambda: calls.append(1))
    t = make_task()

    disp.tick()                      # 接单：set_status(running)
    assert calls, "接单后没叫醒总线"

    calls.clear()
    ap = disp.running[t["id"]]
    disp.abort(t["id"])              # 收尾：_mark_failed
    assert calls, "收尾后没叫醒总线"
    assert ap.proc.poll() is not None


# --- 生命周期 -------------------------------------------------------------------------

def test_start_pumps_queue_in_background(disp, make_task):
    t = make_task()
    disp.start()
    try:
        assert wait_for(lambda: t["id"] in disp.running), "后台线程没接走排队任务"
    finally:
        disp.stop()


def test_stop_kills_running_agents_and_revokes_tokens(disp, make_task):
    t = make_task()
    disp.tick()
    ap = disp.running[t["id"]]
    assert ap.token

    disp.stop()

    assert disp.running == {}
    assert ap.proc.poll() is not None
    assert not dispatcher.verify_agent_token(t["id"], ap.token)


def test_stop_is_safe_without_start(fake_agent, make_disp):
    d = make_disp(fake_agent)
    d.stop()
    d.stop()
