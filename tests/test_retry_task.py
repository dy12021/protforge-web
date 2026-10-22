"""用户端「重跑单个任务」（含工作流行任务）。

管理面板早就有「重跑」按钮，但用户面板没有：工作流展开后的行列表里只有
「下载产物包」，跑砸了的一行只能看着干等。dispatcher 那边其实早认得出
「重跑产生的既有行任务」（_spawn_workflow_row 的 existing_task_id），
缺的只是用户能点的那个入口。
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

PWD = "password-123"


@pytest.fixture
def owner():
    return auth.create_user("owner1", PWD, role="user")


@pytest.fixture
def other():
    return auth.create_user("other1", PWD, role="user")


@pytest.fixture
def env():
    with TestClient(svc.create_public_app()) as c:
        yield c


def _bearer(user):
    return {"Authorization": f"Bearer {auth.issue_token(user['id'])}"}


def _make(owner_id, title="t", brief="b"):
    return store.create_task(title, brief, owner_id)


def _finished(tid, status="done"):
    store.set_status(tid, status, actor="system", message="收工")
    store.record_finished(tid, actor="agent")
    return store.get_task(tid, is_admin=True)


def test_owner_can_retry_a_finished_task(env, owner):
    t = _make(owner["id"])
    _finished(t["id"])
    r = env.post(f"/api/tasks/{t['id']}/retry", headers=_bearer(owner))
    assert r.status_code == 200, r.text
    after = store.get_task(t["id"], is_admin=True)
    assert after["status"] == "queued"
    assert after["error"] == ""
    assert after["summary"] == ""
    assert after["done_at"] is None
    assert store.has_finished(t["id"]) is False, "收口标记没清 → 第二轮 finish 会被 422 拒掉"


def test_cannot_retry_someone_elses_task(env, owner, other):
    t = _make(owner["id"])
    _finished(t["id"])
    r = env.post(f"/api/tasks/{t['id']}/retry", headers=_bearer(other))
    assert r.status_code == 404, r.text


def test_cannot_retry_a_running_task(env, owner):
    t = _make(owner["id"])
    store.set_status(t["id"], "running", actor="system", message="在算")
    r = env.post(f"/api/tasks/{t['id']}/retry", headers=_bearer(owner))
    assert r.status_code == 422, r.text


def test_retry_clears_the_archive_flag(env, owner):
    """归档标记的语义是「本行结果已写回表」。不清的话重跑的新结果写不回去。"""
    t = _make(owner["id"])
    _finished(t["id"])
    store.update_task(t["id"], workflow_archived=1)
    env.post(f"/api/tasks/{t['id']}/retry", headers=_bearer(owner))
    assert store.get_task(t["id"], is_admin=True)["workflow_archived"] == 0


def test_retrying_a_workflow_row_reuses_the_same_task(env, owner):
    """工作流行任务重跑是**同一个任务 id** 重来，不是新建一行。

    调度器认领重跑时走 _spawn_workflow_row(..., existing_task_id)，
    所以库里不该冒出第二个任务——否则结果表那格会被两行抢。
    """
    wf = store.create_workflow(owner["id"], "批次", "做 {x}")
    t = store.create_task("行 1", "x", owner["id"],
                          workflow_id=int(wf["id"]), workflow_row=0)
    _finished(t["id"], status="failed")
    before = store.list_tasks(is_admin=True, limit=500)
    r = env.post(f"/api/tasks/{t['id']}/retry", headers=_bearer(owner))
    assert r.status_code == 200, r.text
    after = store.list_tasks(is_admin=True, limit=500)
    assert len(after) == len(before), "重跑新建了一个任务"
    row = store.get_task(t["id"], is_admin=True)
    assert row["status"] == "queued"
    assert row["workflow_row"] == 0, "行号变了 → 结果表那格会写错位置"
