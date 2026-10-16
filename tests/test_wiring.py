"""接线测试（T13）。

这里测的全是「模块都写好了但没人调用」那一类缺陷。它们有个共同点：
**每个模块自己的测试都是绿的**，缺陷只在接起来之后才显形，而且是静默的——
不报错、不抛异常，只是某件事从来没发生过。单模块测试抓不到它们。

钉死的九条：

1. 令牌表只有一张——调度器签的令牌，内部 app 得认（曾因两张表各说各话，
   agent 每一步回调都 401）。
2. ``claim_queued`` 是原子的：抢第二次抢不到，且如实报告当前状态。
3. 跨进程中止——public app 把状态写成 failed 之后，调度器会去杀掉在跑的进程。
4. ``_mark_failed`` 不覆盖已有的 failed（用户中止的话不能被改成"异常结束"）。
5. 打包器真的会把 packaging 推成 done，并写回 package_path。
6. 产物没确认时**诚实地失败**，而不是悄悄打成空包。
7. 用户面板前端确实被发出来了。
8. ``finish`` 认 ``artifacts`` 字段（pydantic 对未声明的键是静默丢弃）。
9. 初始管理员按环境变量建，且绝不覆盖已有的同名用户。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

import artifacts
import auth
import db
import dispatcher
import store
import app as svc

# 真实起 uvicorn 的两个夹具在 test_app.py 里，这里直接复用，不另写一份
from test_app import internal, public          # noqa: F401


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def owner():
    return auth.create_user("owner1", "password-123", role="user")


@pytest.fixture
def agent_task(owner):
    """造一个任务，返回 id。"""
    return store.create_task(title="分析 1ubq", brief="算一下表面暴露",
                             owner_id=owner["id"])["id"]


def _task(owner, title="t"):
    return store.create_task(title=title, brief="b", owner_id=owner["id"])["id"]


def _outputs(monkeypatch, tmp_path):
    """把 ProtForge 的数据根指到临时目录。

    走 **PROTFORGE_DATA 环境变量**而不是 monkeypatch artifacts.outputs_root：
    打包前 artifacts.package 还会过一遍 guards.resolve(path, tid)，
    outputs 只认 PROTFORGE_DATA 底下。只改 outputs_root 的话，守卫会正确地
    把这个伪造的路径判成越界——测试挂了，但那正是守卫在干活。
    """
    root = tmp_path / "protforge-data"
    (root / "outputs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROTFORGE_DATA", str(root))
    return root / "outputs"


def _job(root: Path, name: str, files: int = 2) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (d / f"part{i}.txt").write_text(f"内容 {i}", encoding="utf-8")
    return d


# ---------------------------------------------------------------- 1. 令牌表

def test_dispatcher_issued_token_is_accepted_by_internal_app(internal, agent_task):
    """**这条是整个令牌接线的命门。**

    调度器签发、内部 app 校验，两边必须读同一张表。曾经 app.py 自己抄了一张，
    于是调度器签的令牌到了内部 app 一律查不到——而每一步都"正常失败"，
    只表现为 agent 的回调持续 401，没有任何报错指向真正的原因。
    """
    token = dispatcher.issue_agent_token(agent_task)
    r = internal.post(f"/api/internal/agent/{agent_task}/step",
                      json={"text": "第一步"},
                      headers={"X-Agent-Token": token})
    assert r.status_code == 200, r.text
    assert r.json()["step"] == "第一步"


def test_agent_token_still_rejects_other_task(internal, agent_task, owner):
    """单一化之后，作用域保证不能丢：task A 的令牌打 task B 必须 403。"""
    other = store.create_task(title="别的", brief="别的", owner_id=owner["id"])["id"]
    token = dispatcher.issue_agent_token(agent_task)
    r = internal.post(f"/api/internal/agent/{other}/step",
                      json={"text": "越权"}, headers={"X-Agent-Token": token})
    assert r.status_code == 403, r.text


def test_agent_token_task_reports_binding(internal, agent_task):
    token = dispatcher.issue_agent_token(agent_task)
    assert dispatcher.agent_token_task(token) == agent_task
    assert dispatcher.agent_token_task("没这回事") is None


# ---------------------------------------------------------------- 2. 原子抢占

def test_claim_queued_is_atomic(agent_task):
    assert store.claim_queued(agent_task) == "queued"
    # 第二次抢不到，且如实报告它现在是什么——不是"queued"，也不是瞎编一个
    assert store.claim_queued(agent_task) == "running"


def test_claim_queued_reports_failed_after_user_abort(agent_task):
    """用户中止与调度器接单撞车时，抢占必须抢不到。"""
    store.set_status(agent_task, "failed", actor="human", message="用户中止了任务")
    assert store.claim_queued(agent_task) == "failed"


def test_claim_queued_writes_startup_event(agent_task):
    store.claim_queued(agent_task)
    msgs = [(e["kind"], e["from_status"], e["to_status"])
            for e in store.list_events(agent_task)]
    assert ("status", "queued", "running") in msgs


# ---------------------------------------------------------------- 3+4. 跨进程中止

FAKE_AGENT = """import sys, time
sys.stdout.write('{"kind":"note","message":"起来了"}\\n')
sys.stdout.flush()
while True:
    time.sleep(0.05)
"""


@pytest.fixture
def running_agent(tmp_path, agent_task, monkeypatch):
    """真起一个假 agent 进程并交给调度器管。

    **不调 disp.start()**：后台线程会先抢一步 _admit，把这个还在排队的任务
    又接走一遍，于是 self.running 里登记的是另一个进程，夹具里握着的 ap
    反而是没人管的野进程——测出来"中止没生效"，其实是夹具自己造了两个。
    这里手动 tick，链路才是确定的。
    """
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    disp = dispatcher.Dispatcher(
        db, db.data_root(), node_exe=sys.executable, agent_script=script,
        gpu_probe=lambda: 0, max_concurrency=1)
    try:
        ap = disp._spawn(agent_task, "算一下", tmp_path / "work")
        yield disp, ap
    finally:
        disp.stop()


def test_abort_from_another_process_kills_the_agent(running_agent, agent_task):
    """public app 在**另一个进程**里中止任务，它改得到数据库、改不到进程。

    调度器必须靠库里的状态把这件事传导过来，否则用户点了中止之后计算还会
    接着跑完——白烧一轮 GPU，而界面上还写着"已中止"。
    """
    disp, ap = running_agent
    assert ap.proc.poll() is None

    # 模拟 public app 做的事：只改数据库
    store.set_status(agent_task, "failed", actor="human", message="用户中止了任务")

    disp.tick()

    assert ap.proc.poll() is not None, "进程还活着：中止没有传导过来"


def test_settle_does_not_overwrite_user_abort_reason(running_agent, agent_task):
    """用户点的是中止，最后看到的必须是中止，不能变成"计算进程异常结束"。"""
    disp, ap = running_agent
    store.set_status(agent_task, "failed", actor="human", message="用户中止了任务")
    store.update_task(agent_task, error="用户中止了任务")

    disp.tick()

    task = store.get_task(agent_task, is_admin=True)
    assert task["status"] == "failed"
    assert task["error"] == "用户中止了任务", task["error"]
    # 进程确实被杀这件事仍然要留在时间线上
    texts = [e["message"] for e in store.list_events(agent_task)]
    assert any("已杀掉进程树" in t for t in texts), texts


# ---------------------------------------------------------------- 5+6. 打包器

def test_packager_closes_packaging_task(owner, monkeypatch, tmp_path):
    """停在 packaging 的任务会被真的打包并收口——这一段原先没有任何人调用。"""
    tid = _task(owner)
    root = _outputs(monkeypatch, tmp_path)
    _job(root, f"t{tid}_freesasa")
    store.set_status(tid, "packaging", actor="system", message="开始打包")

    handled = svc._pack_scan_once()

    assert tid in handled
    task = store.get_task(tid, is_admin=True)
    assert task["status"] == "done", task
    assert task["package_path"]
    with zipfile.ZipFile(task["package_path"]) as z:
        assert any("freesasa" in n for n in z.namelist())


def test_packager_refuses_to_ship_unverified_artifacts(owner, monkeypatch, tmp_path):
    """时间窗兜底发现的产物 verified=0，**不能自动放行**。

    自动确认等于把"先显示、再确认"这道闸门拆了。它该诚实地失败，并且把
    下一步该谁做什么说清楚。
    """
    tid = _task(owner)
    root = _outputs(monkeypatch, tmp_path)
    _job(root, "rand_name_no_prefix")            # 不守 t{id}_ 约定 → 走时间窗
    store.set_status(tid, "packaging", actor="system", message="开始打包")

    svc._pack_scan_once()

    task = store.get_task(tid, is_admin=True)
    assert task["status"] == "failed", task
    assert "尚未确认" in (task["error"] or ""), task["error"]
    assert not task["package_path"], "没确认的产物被打包了"


def test_packager_fails_loudly_when_nothing_found(owner, monkeypatch, tmp_path):
    tid = _task(owner)
    _outputs(monkeypatch, tmp_path)
    store.set_status(tid, "packaging", actor="system", message="开始打包")

    svc._pack_scan_once()

    task = store.get_task(tid, is_admin=True)
    assert task["status"] == "failed"
    assert "没有找到任何产物" in (task["error"] or "")


def test_packager_skips_already_packaged(owner, monkeypatch, tmp_path):
    """已有 package_path 的任务不能再打一遍——那会用新包覆盖掉旧包。"""
    tid = _task(owner)
    root = _outputs(monkeypatch, tmp_path)
    _job(root, f"t{tid}_x")
    store.set_status(tid, "packaging", actor="system", message="开始打包")
    store.update_task(tid, package_path=str(db.data_root() / "packages" / "old.zip"))

    assert svc._pack_scan_once() == []


# ---------------------------------------------------------------- 7. 重新打包

def test_repack_needs_admin(internal, owner):
    auth.create_user("plain", "password-123", role="user")
    tok = auth.issue_token(auth.get_user("plain")["id"])
    tid = _task(owner)
    store.set_status(tid, "failed", actor="system", message="产物未确认")

    r = internal.post(f"/api/internal/admin/repack/{tid}",
                      headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403, r.text


def test_repack_pushes_failed_task_back_to_packaging(internal, owner):
    admin = auth.create_user("boss", "password-123", role="admin")
    tok = auth.issue_token(admin["id"])
    tid = _task(owner)
    store.set_status(tid, "failed", actor="system", message="产物未确认")
    store.update_task(tid, error="产物已找到但尚未确认打包")

    r = internal.post(f"/api/internal/admin/repack/{tid}",
                      headers={"Authorization": f"Bearer {tok}"})

    assert r.status_code == 200, r.text
    task = store.get_task(tid, is_admin=True)
    assert task["status"] == "packaging"
    assert not task["error"], "重新排队了还留着旧错误，界面上会误导"


def test_repack_refuses_to_clobber_a_done_task(internal, owner):
    admin = auth.create_user("boss", "password-123", role="admin")
    tok = auth.issue_token(admin["id"])
    tid = _task(owner)
    store.set_status(tid, "done", actor="system", message="完成")

    r = internal.post(f"/api/internal/admin/repack/{tid}",
                      headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------- 8. 前端

def test_user_panel_is_served(public):
    """8473 首页曾经是 404——前端写好了却没人发出来。"""
    r = public.get("/")
    assert r.status_code == 200, r.status_code
    assert "<html" in r.text.lower()


def test_api_routes_still_win_over_the_static_mount(public):
    """挂载顺序错了会把接口全吃成静态文件，症状是"登录接口 404"。"""
    r = public.post("/api/auth/login", json={"username": "x", "password": "y"})
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------- 9. finish.artifacts

def test_finish_accepts_artifacts_field(internal, agent_task):
    """pydantic 对未声明的键是**静默丢弃**的：agent 以为报了、后端当没收到。"""
    token = dispatcher.issue_agent_token(agent_task)
    r = internal.post(f"/api/internal/agent/{agent_task}/finish",
                      json={"summary": "算完了", "artifacts": ["a.pdb", "b.txt"]},
                      headers={"X-Agent-Token": token})
    assert r.status_code == 200, r.text
    msgs = [e["message"] for e in store.list_events(agent_task)]
    assert any("自报产物" in m and "a.pdb" in m for m in msgs), msgs


def test_finish_without_artifacts_still_works(internal, agent_task):
    token = dispatcher.issue_agent_token(agent_task)
    r = internal.post(f"/api/internal/agent/{agent_task}/finish",
                      json={"summary": "算完了"}, headers={"X-Agent-Token": token})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 10. 初始管理员

def test_bootstrap_admin_from_env(monkeypatch):
    monkeypatch.setenv("PROTFORGE_ADMIN_USER", "boss")
    monkeypatch.setenv("PROTFORGE_ADMIN_PASSWORD", "password-123")

    made = svc.bootstrap_admin()

    assert made and made["role"] == "admin"
    assert auth.authenticate("boss", "password-123") is not None


def test_bootstrap_admin_never_overwrites(monkeypatch):
    auth.create_user("boss", "password-123", role="user")
    monkeypatch.setenv("PROTFORGE_ADMIN_USER", "boss")
    monkeypatch.setenv("PROTFORGE_ADMIN_PASSWORD", "另一个口令-456")

    assert svc.bootstrap_admin() is None
    # 原口令还得管用：覆盖掉的话等于用环境变量把现有人踢出系统
    assert auth.authenticate("boss", "password-123") is not None


def test_bootstrap_admin_noop_without_env(monkeypatch):
    monkeypatch.delenv("PROTFORGE_ADMIN_USER", raising=False)
    monkeypatch.delenv("PROTFORGE_ADMIN_PASSWORD", raising=False)
    assert svc.bootstrap_admin() is None
    assert auth.get_user("") is None
