"""状态面板的后端（server/status_api.py）。

三条规矩，每条都要有能红的围栏：

1. **系统指标与全局速度只对管理员账号可见。** 常规用户调 /api/status/system
   必须 403 —— 不是「拿到了再由前端藏起来」。
2. **常规用户只看到自己的任务。** 别人的任务数、用时，一个都不能漏出去。
3. **单行用时只算真正在跑的那一段。** 工作流是先把整批行任务建出来再串行跑
   的，用 created_at 当起点会把「排队等前面的行」也算进去（实测某批 61 行的
   表：单行中位 8.4 小时，其中大半是在排队）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import auth
import db
import store
import app as svc

PWD = "password-123"


@pytest.fixture
def owner():
    return auth.create_user("owner1", PWD, role="user")


@pytest.fixture
def other():
    return auth.create_user("other1", PWD, role="user")


@pytest.fixture
def admin():
    return auth.create_user("boss", PWD, role="admin")


@pytest.fixture
def env():
    with TestClient(svc.create_public_app()) as c:
        yield c


def _bearer(user):
    return {"Authorization": f"Bearer {auth.issue_token(user['id'])}"}


def _make_task(owner_id, title="t", brief="b"):
    return store.create_task(title, brief, owner_id)


# ------------------------------------------------------------- 可见性

def test_a_regular_user_sees_only_their_own_tasks(env, owner, other):
    _make_task(owner["id"], "a1")
    _make_task(owner["id"], "a2")
    _make_task(other["id"], "b1")

    r = env.get("/api/status/overview", headers=_bearer(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "user"
    assert body["tasks"]["total"] == 2, "别人的任务数漏出去了：%r" % body["tasks"]


def test_an_admin_sees_everything(env, owner, other, admin):
    _make_task(owner["id"], "a1")
    _make_task(other["id"], "b1")
    # **必须有一个已完成的任务**：全局吞吐是拿完成时间跟 24 小时前的
    # cutoff 比的，而 cutoff 与库里的时间戳必须同为 naive —— 混了会
    # TypeError，真机上直接 500。
    done = _make_task(owner["id"], "a2")
    store.set_status(done["id"], "done", actor="system", message="ok")

    r = env.get("/api/status/overview", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "admin"
    assert body["tasks"]["total"] == 3
    assert "throughput_per_hour" in body, "管理员看不到全局吞吐"
    assert body["throughput_per_hour"] >= 0


def test_a_regular_user_does_not_get_the_global_throughput(env, owner):
    r = env.get("/api/status/overview", headers=_bearer(owner))
    body = r.json()
    assert "throughput_per_hour" not in body, \
        "常规用户拿到了全局速度：%r" % body


def test_system_metrics_are_forbidden_for_regular_users(env, owner):
    r = env.get("/api/status/system", headers=_bearer(owner))
    assert r.status_code == 403, "常规用户拿到了系统指标：%r" % r.text


def test_an_admin_can_read_the_system_metrics(env, admin):
    r = env.get("/api/status/system", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("cpu", "memory", "gpu", "disk"):
        assert key in body, "少了 %s：%r" % (key, body)
    assert "percent" in body["cpu"]
    assert "percent" in body["memory"]
    # GPU 可能没有 —— 那时 available=False，不是缺字段
    assert "available" in body["gpu"]


def test_the_system_endpoint_needs_a_login(env):
    r = env.get("/api/status/system")
    assert r.status_code in (401, 422), r.text


# ------------------------------------------------------------- 用时口径

def test_queueing_time_is_not_counted_as_runtime(env, owner):
    """created_at 是「任务被建出来」，不是「它开始跑」。

    工作流把整批行任务先建出来再串行跑，用 created_at 当起点的话，
    单行用时里会塞进「等前面那些行跑完」的时间——实测偏差能到几倍。
    """
    t = _make_task(owner["id"], "慢启动")
    time.sleep(1.0)                      # 这一秒是「排队」
    store.set_status(t["id"], "running", actor="system", message="开始")
    store.set_status(t["id"], "done", actor="system", message="完成")

    r = env.get("/api/status/overview", headers=_bearer(owner))
    body = r.json()
    assert body["duration"]["count"] == 1
    secs = body["duration"]["avg_seconds"]
    # 界限 0.8 秒：实现正确时这段只有几毫秒（两次 set_status 之间），
    # 实现错了（拿 created_at 当起点）会是 1 秒以上。**不要收到 0.2 秒**——
    # 那不是抓得更紧，是把「机器正忙」也判成失败。
    assert secs < 0.8, \
        "排队时间被算进单行用时了（%s 秒）：它该从认领那一刻起算" % secs


def test_duration_block_is_empty_when_nothing_finished(env, owner):
    _make_task(owner["id"], "还在跑")
    r = env.get("/api/status/overview", headers=_bearer(owner))
    body = r.json()
    assert body["duration"]["count"] == 0
    assert body["duration"]["avg_seconds"] == 0.0
    assert body["success_rate"] == 0.0


def test_success_rate_counts_done_over_terminal(env, owner):
    a = _make_task(owner["id"], "a")
    b = _make_task(owner["id"], "b")
    store.set_status(a["id"], "done", actor="system", message="ok")
    store.set_status(b["id"], "failed", actor="system", message="炸了")
    r = env.get("/api/status/overview", headers=_bearer(owner))
    body = r.json()
    assert body["success_rate"] == 0.5
    assert body["tasks"]["done"] == 1
    assert body["tasks"]["failed"] == 1


# ------------------------------------------------------------- 单工作流

def test_workflow_status_only_counts_that_workflow(env, owner):
    wf = store.create_workflow(owner["id"], "批次A", "做 {x}")
    other_wf = store.create_workflow(owner["id"], "批次B", "做 {y}")
    assert wf["id"] != other_wf["id"]

    # 批次 A 的行：一行做完，一行失败
    r1 = store.create_task("A-1", "x", owner["id"], workflow_id=wf["id"],
                            workflow_row=0)
    r2 = store.create_task("A-2", "y", owner["id"], workflow_id=wf["id"],
                            workflow_row=1)
    store.set_status(r1["id"], "done", actor="system", message="ok")
    store.set_status(r2["id"], "failed", actor="system", message="炸了")
    # 批次 B 的一行（不该被算进 A）
    r3 = store.create_task("B-1", "z", owner["id"], workflow_id=other_wf["id"],
                            workflow_row=0)
    store.set_status(r3["id"], "done", actor="system", message="ok")

    r = env.get(f"/api/status/workflow/{wf['id']}", headers=_bearer(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow_id"] == wf["id"]
    assert body["rows"]["done"] == 1
    assert body["rows"]["failed"] == 1
    assert body["rows"]["done"] + body["rows"]["failed"] == 2, \
        "把别的工作流的行算进来了：%r" % body["rows"]


def test_workflow_status_of_someone_elses_workflow_is_404(env, owner, other):
    wf = store.create_workflow(other["id"], "别人的", "做 {x}")
    r = env.get(f"/api/status/workflow/{wf['id']}", headers=_bearer(owner))
    assert r.status_code == 404, r.text


def test_workflow_status_of_a_missing_workflow_is_404(env, owner):
    r = env.get("/api/status/workflow/999999", headers=_bearer(owner))
    assert r.status_code == 404, r.text


# ------------------------------------------------------------- 降级

def test_gpu_probe_failure_degrades_instead_of_raising(monkeypatch):
    """这台机器可能没有 GPU，也可能 nvidia-smi 卡住。不能因此 500。"""
    import status_api

    def _boom(*a, **kw):
        raise OSError("no nvidia-smi")

    monkeypatch.setattr(status_api.subprocess, "run", _boom)
    gpu = status_api._gpu()
    assert gpu["available"] is False
    assert gpu["util_percent"] == 0


def test_cpu_probe_returns_a_sane_number():
    import status_api
    pct = status_api._cpu_percent()
    assert 0.0 <= pct <= 100.0
