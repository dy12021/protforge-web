"""「重跑」的第二轮收口必须能被接受。

背景（review 高危③）
--------------------
``record_finished`` 每任务至多一条（``WHERE NOT EXISTS``），而管理员点重跑时原先
只清 done_at/step/error/package_path/summary，**没清这条标记**——而且全仓没有任何
代码能清它。于是：

1. 第二轮 agent 调 ``finish`` 被 422 拒掉（``app.py`` 现在拿 has_finished 当重复
   收口的闸门）；
2. 调度器 ``_settle`` 读到的是**第一轮**的旧标记，把第二轮任何退出（包括直接崩溃）
   都判成"正常收口"进 packaging，最后打出一份与本次运行无关的旧结果包。

用户看到的现象是"任务 done，但结果不对"——比明确失败更难查。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import db
import store
import app as svc

# admin/ 不在 conftest 的 sys.path 里（conftest 是冻结的，只加了 server/），
# 而重跑是管理员面板的动作，这一层必须真走到面板代码才算测到。
ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

import app_admin      # noqa: E402

PWD = "password-123"


@pytest.fixture
def owner():
    return auth.create_user("owner1", PWD, role="user")


@pytest.fixture
def admin():
    return auth.create_user("boss", PWD, role="admin")


@pytest.fixture
def env():
    internal = svc.create_internal_app()
    panel = app_admin.create_admin_app()
    with TestClient(internal) as ci, TestClient(panel) as cp:
        yield ci, cp


def _bearer(user):
    return {"Authorization": f"Bearer {auth.issue_token(user['id'])}"}


# ---------------------------------------------------------------- 数据层

def test_clear_finished_removes_only_the_marker(owner):
    tid = store.create_task(title="t", brief="b", owner_id=owner["id"])["id"]
    store.add_event(tid, "step", "agent", "在算")
    store.record_finished(tid, actor="agent")

    assert store.has_finished(tid)
    store.clear_finished(tid)

    assert not store.has_finished(tid)
    # 时间线是给人看的历史，重跑不该抹掉它
    assert any(e["kind"] == "step" for e in store.list_events(tid))


def test_clear_finished_is_safe_when_absent(owner):
    tid = store.create_task(title="t", brief="b", owner_id=owner["id"])["id"]
    store.clear_finished(tid)          # 不该抛
    assert not store.has_finished(tid)


def test_record_finished_is_idempotent(owner):
    tid = store.create_task(title="t", brief="b", owner_id=owner["id"])["id"]
    store.record_finished(tid, actor="agent")
    store.record_finished(tid, actor="agent")
    n = sum(1 for e in store.list_events(tid) if e["kind"] == "finished")
    assert n == 1


# ---------------------------------------------------------------- 重跑整条链

def test_retry_clears_the_finished_marker(env, owner, admin):
    ci, cp = env
    tid = store.create_task(title="t", brief="b", owner_id=owner["id"])["id"]
    store.set_status(tid, "running", actor="system")
    store.record_finished(tid, actor="agent")
    store.set_status(tid, "done", actor="system", message="第一轮完成")

    r = cp.post(f"/api/admin/tasks/{tid}/retry", headers=_bearer(admin))

    assert r.status_code == 200, r.text
    assert store.get_task(tid, is_admin=True)["status"] == "queued"
    assert not store.has_finished(tid), "重跑没清收口标记，第二轮 finish 必被 422"


def test_second_run_finish_is_accepted_after_retry(env, owner, admin):
    """完整闭环：第一轮收口 -> 重跑 -> 第二轮 finish 必须 200，且状态不倒退。"""
    ci, cp = env
    tid = store.create_task(title="t", brief="b", owner_id=owner["id"])["id"]
    store.set_status(tid, "running", actor="system")
    tok1 = svc.issue_agent_token(tid)
    r1 = ci.post(f"/api/internal/agent/{tid}/finish", json={"summary": "第一轮"},
                 headers={"X-Agent-Token": tok1})
    assert r1.status_code == 200, r1.text
    store.set_status(tid, "done", actor="system", message="第一轮完成")

    assert cp.post(f"/api/admin/tasks/{tid}/retry", headers=_bearer(admin)).status_code == 200

    store.set_status(tid, "running", actor="system")
    tok2 = svc.issue_agent_token(tid)
    r2 = ci.post(f"/api/internal/agent/{tid}/finish", json={"summary": "第二轮"},
                 headers={"X-Agent-Token": tok2})

    assert r2.status_code == 200, f"第二轮 finish 被拒：{r2.text}"
    assert store.has_finished(tid)
    assert store.get_task(tid, is_admin=True)["summary"] == "第二轮"


def test_retry_refuses_a_running_task(env, owner, admin):
    ci, cp = env
    tid = store.create_task(title="t", brief="b", owner_id=owner["id"])["id"]
    store.set_status(tid, "running", actor="system")

    r = cp.post(f"/api/admin/tasks/{tid}/retry", headers=_bearer(admin))

    assert r.status_code == 422, r.text
    assert not store.has_finished(tid), "被拒的重跑不该动收口标记"
