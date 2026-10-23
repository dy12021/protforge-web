"""验收机制与续跑（2026-10-23）。

两件事，各有各的坑：

**验收**：算完 ≠ 交付完成。平台原来照单全收——模型交上来什么就打包、
标绿、归档。现在打完包进「待验收」，人认了才算完成；平台能机械判定的
硬条件自动判不通过（但人可以推翻）。

**续跑**：同一个任务接着干，**保留上一轮的产物**，新一轮的提示词里带上
失败原因 / 上一轮结论 / 上一轮产出清单，再加用户这一轮的要求。

围栏清单：

- ``test_packaged_task_waits_for_acceptance``      打完包进「待验收」，不算完成
- ``test_zero_byte_artifacts_are_rejected``         产物全是 0 字节 → 不通过
- ``test_empty_summary_is_rejected``                没有结论 → 不通过
- ``test_declared_artifacts_not_found_is_rejected`` 模型自报的产物找不到 → 不通过
- ``test_declared_artifacts_found_is_accepted``     自报的产物能找到 → 正常待验收
- ``test_accept_is_idempotent``                     验收幂等
- ``test_reject_records_the_note``                  打回记下意见
- ``test_continue_carries_failure_reason``          失败的接着跑 → 带上失败原因
- ``test_continue_carries_previous_result``         完成的补要求 → 带上上一轮结论与产物
- ``test_continue_keeps_artifacts_and_clears_package``  产物留着、package_path 清掉
- ``test_continue_clears_finished_marker``          收口标记要清（否则第二轮 finish 422）
- ``test_continue_resets_workflow_archive_flag``    归档标记归零（结果表那一格要能被覆盖）
- ``test_pending_row_does_not_block_the_batch``     待验收的行不挡后续行
- ``test_continue_endpoint_requires_owner``         越权 404
- ``test_workflow_batch_accept``                   工作流批量验收
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as svc
import auth
import db
import review
import store

XLSX_MIME = ("application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet")


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def client():
    return TestClient(svc.create_public_app())


def _token(username, role="user"):
    if auth.get_user(username) is None:
        auth.create_user(username, "pw-for-tests", role=role)
    return auth.issue_token(int(auth.get_user(username)["id"]))


def _headers(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture()
def alice():
    return _token("alice")


@pytest.fixture()
def bob():
    return _token("bob")


@pytest.fixture()
def root():
    return _token("root", role="admin")


def _done_task(owner_id, *, summary="算完了，结论在这里", size=128,
               name="result.pdb", declared=None, error="", status="done"):
    """造一个「算完了」的任务：带一个产物、有结论。

    直接落库而不是走打包：打包要真起子进程压 zip，而这一层要验的是
    「打包之后发生了什么」，不是打包本身。
    """
    task = store.create_task("第 1 行", "原始需求", owner_id=owner_id,
                             actor="human")
    tid = int(task["id"])
    store.artifacts.add(tid, f"work/{tid}/{name}", name, size=size)
    fields = {"summary": summary, "package_path": f"packages/task_{tid}.zip"}
    if error:
        fields["error"] = error
    store.update_task(tid, **fields)
    if declared is not None:
        store.update_task(tid, declared=json.dumps(declared, ensure_ascii=False))
    if status != "queued":
        store.set_status(tid, status, actor="system", message="算完了")
    return store.get_task(tid, is_admin=True)


# ---------------------------------------------------------------- 验收


def test_packaged_task_waits_for_acceptance(alice):
    """打完包的任务进「待验收」，**不算交付完成**。

    这是整个功能的根：算完不等于交付完。
    """
    task = _done_task(_uid("alice"))
    updated = review.mark_pending(task["id"])
    assert updated["acceptance"] == db.ACCEPTANCE_PENDING
    assert store.is_accepted(updated) is False, "待验收的任务不能算交付完成"
    assert store.awaiting_acceptance(updated) is True


def test_zero_byte_artifacts_are_rejected(alice):
    """产物全是 0 字节的空文件：有文件名，没有内容——那不叫交上来了。"""
    task = _done_task(_uid("alice"), size=0)
    updated = review.mark_pending(task["id"])
    assert updated["acceptance"] == db.ACCEPTANCE_REJECTED
    assert store.is_accepted(updated) is False
    msgs = " ".join(e["message"] for e in store.list_events(task["id"]))
    assert "0 字节" in msgs, msgs


def test_empty_summary_is_rejected(alice):
    """没有结论就无从验收。"""
    task = _done_task(_uid("alice"), summary="")
    updated = review.mark_pending(task["id"])
    assert updated["acceptance"] == db.ACCEPTANCE_REJECTED
    msgs = " ".join(e["message"] for e in store.list_events(task["id"]))
    assert "没有给出结论" in msgs, msgs


def test_declared_artifacts_not_found_is_rejected(alice):
    """模型自报的产物在产物清单里一个都找不到 → 不通过。

    判据用**basename** 比对：模型可能写 ``outputs/result.pdb``，
    而盘上登记的名字是 ``result.pdb``——按全串比会对不上，
    于是明明交了产物却被判「一个都没找到」。
    """
    task = _done_task(_uid("alice"), declared=["outputs/nowhere.pdb"])
    updated = review.mark_pending(task["id"])
    assert updated["acceptance"] == db.ACCEPTANCE_REJECTED
    msgs = " ".join(e["message"] for e in store.list_events(task["id"]))
    assert "一个都没找到" in msgs, msgs


def test_declared_artifacts_found_is_accepted(alice):
    """自报的产物能找到（basename 对上）→ 正常进待验收。"""
    task = _done_task(_uid("alice"), name="result.pdb",
                      declared=["outputs/result.pdb"])
    updated = review.mark_pending(task["id"])
    assert updated["acceptance"] == db.ACCEPTANCE_PENDING, \
        review.hard_check_failures(updated)


def test_accept_is_idempotent(alice):
    """验收通过；再点一次不报错（幂等）。"""
    task = _done_task(_uid("alice"))
    review.mark_pending(task["id"])
    out = review.accept(task["id"])
    assert out["acceptance"] == db.ACCEPTANCE_ACCEPTED
    assert store.is_accepted(out) is True
    out2 = review.accept(task["id"])
    assert out2["acceptance"] == db.ACCEPTANCE_ACCEPTED


def test_auto_reject_can_be_overridden(alice):
    """自动判不通过**不是终局**：人可以再点「验收通过」放过去。"""
    task = _done_task(_uid("alice"), size=0)
    review.mark_pending(task["id"])
    out = review.accept(task["id"])
    assert out["acceptance"] == db.ACCEPTANCE_ACCEPTED


def test_reject_records_the_note(alice):
    """打回：记下人说的「哪里不行」。"""
    task = _done_task(_uid("alice"))
    review.mark_pending(task["id"])
    out = review.reject(task["id"], "对接姿势不对，换个种子")
    assert out["acceptance"] == db.ACCEPTANCE_REJECTED
    msgs = " ".join(e["message"] for e in store.list_events(task["id"]))
    assert "对接姿势不对" in msgs, msgs


# ---------------------------------------------------------------- 续跑


def test_continue_carries_failure_reason(alice):
    """失败的接着跑 → 新一轮的提示词里要带上失败原因。

    不带的话模型不知道上一轮为什么挂的，只能从头猜。
    """
    task = _done_task(_uid("alice"), status="failed", error="采样时 OOM 了")
    out = review.continue_task(task["id"], "改成小一点的体系再跑")
    assert out["status"] == "queued"
    followup = store.get_followup(task["id"])
    assert "采样时 OOM 了" in followup, followup
    assert "改成小一点的体系再跑" in followup, followup


def test_continue_carries_previous_result(alice):
    """完成的补要求 → 带上上一轮的结论与产出清单。"""
    task = _done_task(_uid("alice"), summary="RMSD 1.8 埃，可用")
    out = review.continue_task(task["id"], "再跑 10 个构象取平均")
    assert out["status"] == "queued"
    followup = store.get_followup(task["id"])
    assert "RMSD 1.8" in followup, followup
    assert "result.pdb" in followup, "上一轮产出了哪些文件要告诉模型"
    assert "再跑 10 个构象取平均" in followup


def test_continue_keeps_artifacts_and_clears_package(alice):
    """**产物留着**（模型要接着改它们），但 package_path 要清。

    package_path 不清的话打包扫描会看见它非空就跳过，
    新一轮的产物永远打不进包——上一轮的包会把这一轮的结果盖住。
    """
    task = _done_task(_uid("alice"))
    review.mark_pending(task["id"])
    out = review.continue_task(task["id"], "再改一版")
    assert out["package_path"] == "", "package_path 必须清掉，否则新一轮打不进包"
    arts = store.artifacts.list(task["id"])
    assert len(arts) == 1, "上一轮的产物要保留，模型要接着改"
    assert out["acceptance"] == db.ACCEPTANCE_NONE, "新一轮还没算完"


def test_continue_clears_finished_marker(alice):
    """收口标记要清：不清的话第二轮的 finish 会被 422 拒掉。"""
    task = _done_task(_uid("alice"))
    store.record_finished(task["id"], actor="agent")
    assert store.has_finished(task["id"]) is True
    review.continue_task(task["id"], "再改一版")
    assert store.has_finished(task["id"]) is False


def test_continue_resets_workflow_archive_flag(alice):
    """归档标记归零：结果表那一格要能被新一轮覆盖。"""
    wf = store.create_workflow(_uid("alice"), "批次", "查 {{seq}}")
    task = _done_task(_uid("alice"))
    store.update_task(task["id"], workflow_id=int(wf["id"]), workflow_row=0,
                      workflow_archived=1)
    out = review.continue_task(task["id"], "再改一版")
    assert out["workflow_archived"] == 0


def test_continue_requires_a_note(alice):
    """空的要求没有意义——那是「重跑」，不是「续跑」。"""
    task = _done_task(_uid("alice"))
    with pytest.raises(review.ReviewError):
        review.continue_task(task["id"], "   ")


def test_continue_refuses_live_task(alice):
    """还在跑的任务不用续跑。"""
    task = store.create_task("在跑", "x", owner_id=_uid("alice"), actor="human")
    with pytest.raises(review.ReviewError):
        review.continue_task(int(task["id"]), "再改一版")


def test_pending_row_does_not_block_the_batch(alice):
    """**待验收的行不挡后续行**（用户 2026-10-23 定的）。

    闸门②看的是「有没有未归档的行」。待验收的行 status 仍是 done，
    所以它照样进归档 pass——批次照跑照归档，验收状态逐行记录。
    """
    wf = store.create_workflow(_uid("alice"), "批次", "查 {{seq}}")
    wid = int(wf["id"])
    row = store.create_task("第 1 行", "x", owner_id=_uid("alice"),
                            actor="system", workflow_id=wid, workflow_row=0)
    tid = int(row["id"])
    store.artifacts.add(tid, f"work/{tid}/r.pdb", "r.pdb", size=10)
    store.update_task(tid, summary="算完了")
    store.set_status(tid, "done", actor="system", message="ok")
    review.mark_pending(tid)

    unarchived = store.workflow_unarchived_terminal_rows(wid)
    assert [int(r["id"]) for r in unarchived] == [tid], \
        "待验收的行必须照样进归档 pass，否则整批卡住"


# ---------------------------------------------------------------- HTTP 层


def test_accept_endpoint(alice, client):
    """端点：验收通过。"""
    task = _done_task(_uid("alice"))
    review.mark_pending(task["id"])
    r = client.post(f"/api/tasks/{task['id']}/accept",
                    headers=_headers(alice))
    assert r.status_code == 200, r.text
    assert r.json()["task"]["acceptance"] == db.ACCEPTANCE_ACCEPTED


def test_reject_endpoint_reruns(alice, client):
    """打回默认顺手续跑（打回是「这一版不行，再来」）。"""
    task = _done_task(_uid("alice"))
    review.mark_pending(task["id"])
    r = client.post(f"/api/tasks/{task['id']}/reject", headers=_headers(alice),
                    json={"note": "换个种子"})
    assert r.status_code == 200, r.text
    body = r.json()["task"]
    assert body["status"] == "queued"
    assert "换个种子" in store.get_followup(task["id"])


def test_continue_endpoint(alice, client):
    """续跑端点。"""
    task = _done_task(_uid("alice"))
    r = client.post(f"/api/tasks/{task['id']}/continue",
                    headers=_headers(alice), json={"note": "再改一版"})
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "queued"


def test_continue_endpoint_requires_owner(alice, bob, client):
    """越权一律 404（不承认这个 id 存在）。"""
    task = _done_task(_uid("alice"))
    r = client.post(f"/api/tasks/{task['id']}/continue", headers=_headers(bob),
                    json={"note": "我来改"})
    assert r.status_code == 404, r.text


def test_accept_endpoint_rejects_unfinished(alice, client):
    """还在跑的任务验收不了。"""
    task = store.create_task("在跑", "x", owner_id=_uid("alice"), actor="human")
    r = client.post(f"/api/tasks/{task['id']}/accept", headers=_headers(alice))
    assert r.status_code == 422, r.text


def test_workflow_batch_accept(alice, client):
    """工作流批量验收：一次把还没人认的行都认了。

    61 行那种批次逐行点 61 次是不现实的，所以要有「一批都认了」。
    """
    wf = store.create_workflow(_uid("alice"), "批次", "查 {{seq}}")
    wid = int(wf["id"])
    tids = []
    for i in range(3):
        row = store.create_task(f"第 {i + 1} 行", "x", owner_id=_uid("alice"),
                                actor="system", workflow_id=wid, workflow_row=i)
        tid = int(row["id"])
        store.artifacts.add(tid, f"work/{tid}/r.pdb", "r.pdb", size=10)
        store.update_task(tid, summary="算完了")
        store.set_status(tid, "done", actor="system", message="ok")
        review.mark_pending(tid)
        tids.append(tid)

    r = client.post(f"/api/workflows/{wid}/accept", headers=_headers(alice),
                    json={})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3
    for tid in tids:
        assert store.get_task(tid, is_admin=True)["acceptance"] \
            == db.ACCEPTANCE_ACCEPTED
    assert store.workflow_rows_awaiting_acceptance(wid) == []


def test_workflow_batch_accept_specific_rows(alice, client):
    """给了 rows 就只认那几行。"""
    wf = store.create_workflow(_uid("alice"), "批次", "查 {{seq}}")
    wid = int(wf["id"])
    for i in range(2):
        row = store.create_task(f"第 {i + 1} 行", "x", owner_id=_uid("alice"),
                                actor="system", workflow_id=wid, workflow_row=i)
        tid = int(row["id"])
        store.artifacts.add(tid, f"work/{tid}/r.pdb", "r.pdb", size=10)
        store.update_task(tid, summary="算完了")
        store.set_status(tid, "done", actor="system", message="ok")
        review.mark_pending(tid)

    r = client.post(f"/api/workflows/{wid}/accept", headers=_headers(alice),
                    json={"rows": [0]})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    assert len(store.workflow_rows_awaiting_acceptance(wid)) == 1


def test_admin_panel_can_accept_a_task(root, alice):
    """值班台代替用户验收。

    值班台的任务表里挂着「待验收」芯片，不给个能点的入口就成了只读摆设。
    """
    import app_admin

    task = _done_task(_uid("alice"))
    review.mark_pending(task["id"])
    panel = TestClient(app_admin.create_admin_app())
    r = panel.post(f"/api/admin/tasks/{task['id']}/accept",
                   headers=_headers(root))
    assert r.status_code == 200, r.text
    assert r.json()["acceptance"] == db.ACCEPTANCE_ACCEPTED


def test_admin_panel_can_accept_workflow_rows(root, alice):
    """值班台也能整批验收。"""
    import app_admin

    wf = store.create_workflow(_uid("alice"), "批次", "查 {{seq}}")
    wid = int(wf["id"])
    row = store.create_task("第 1 行", "x", owner_id=_uid("alice"),
                            actor="system", workflow_id=wid, workflow_row=0)
    tid = int(row["id"])
    store.artifacts.add(tid, f"work/{tid}/r.pdb", "r.pdb", size=10)
    store.update_task(tid, summary="算完了")
    store.set_status(tid, "done", actor="system", message="ok")
    review.mark_pending(tid)

    panel = TestClient(app_admin.create_admin_app())
    r = panel.post(f"/api/admin/workflows/{wid}/accept", headers=_headers(root),
                   json={})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    assert store.get_task(tid, is_admin=True)["acceptance"] \
        == db.ACCEPTANCE_ACCEPTED


# ---------------------------------------------------------------- 接线


def test_packaging_success_marks_the_task_awaiting_review(alice, tmp_path,
                                                          monkeypatch):
    """**打包成功之后进「待验收」**——这是整条链的接头。

    接线点错一个地方，整个验收机制就是空转的：任务照样标绿、照样归档，
    界面上永远看不到「待验收」。所以这条直接走真的打包路径（真起子进程
    压 zip），不 mock。
    """
    import time

    import artifacts

    monkeypatch.setenv("PROTFORGE_DATA", str(tmp_path / "pf"))
    root = tmp_path / "pf" / "outputs"
    root.mkdir(parents=True)

    task = store.create_task("设计一个结合蛋白", "要能表达的小分子结合蛋白",
                             owner_id=_uid("alice"), actor="human")
    tid = int(task["id"])
    job = root / f"t{tid}_design"
    job.mkdir(parents=True)
    (job / "part0.pdb").write_text("ATOM      1  N   MET A   1\n",
                                   encoding="utf-8")
    artifacts.discover(task_id=tid, started_at=None, ended_at=None)
    store.update_task(tid, summary="算完了")
    store.set_status(tid, "packaging", actor="system", message="收口")

    svc._package_one(tid)

    out = store.get_task(tid, is_admin=True)
    assert out["status"] == "done", "打包成功就该是 done"
    assert out["acceptance"] == db.ACCEPTANCE_PENDING, \
        "打包成功后必须进「待验收」，否则验收机制整条空转"
    assert store.is_accepted(out) is False, "待验收不算交付完成"


FAKE_AGENT = """
import sys
sys.stdin.read()      # 阻塞在 stdin 上，等调度器收它
"""


def test_followup_reaches_the_brief(tmp_path, alice):
    """**补充要求必须真的进到模型的提示词里。**

    模型读的是工作目录里的 ``brief.md``（agent 靠
    ``PROTFORGE_WEB_BRIEF_PATH`` 找到它）。续跑写的那段话如果没进这个文件，
    续跑就只是「重新跑了一遍原来那个需求」——界面上看着在续跑，模型那边
    什么都不知道。
    """
    import sys

    import dispatcher
    import guards

    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")

    task = store.create_task("续跑", "原始需求", owner_id=_uid("alice"),
                             actor="human")
    tid = int(task["id"])
    store.append_followup(tid, "### 本次补充要求\n换个对接种子再跑")

    work = guards.regions(tid)[guards.WORK]
    disp = dispatcher.Dispatcher(
        db, db.data_root(), node_exe=sys.executable,
        agent_script=str(script), gpu_probe=lambda: 2, max_concurrency=1)
    try:
        disp._spawn(tid, "原始需求", work)
        brief = (work / "brief.md").read_text(encoding="utf-8")
    finally:
        disp.stop()

    assert "原始需求" in brief, brief
    assert "换个对接种子再跑" in brief, \
        "补充要求没进 brief.md —— 续跑就白续了：" + brief


# ---------------------------------------------------------------- 迁移


def test_legacy_db_gets_the_review_columns(tmp_path, monkeypatch):
    """**线上那个库是本次改动之前建好的**——新列必须靠迁移补上去。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，所以新库有这三列
    不等于旧库有。这一条手写一份旧 schema 的库来跑迁移。
    """
    import sqlite3

    legacy = tmp_path / "legacy.db"
    c = sqlite3.connect(legacy)
    try:
        c.executescript(
            "CREATE TABLE users ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,"
            " password_hash TEXT NOT NULL, salt TEXT NOT NULL,"
            " role TEXT NOT NULL DEFAULT 'user', disabled INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL, CHECK (role IN ('user','admin')));"
            "CREATE TABLE tasks ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " title TEXT NOT NULL, brief TEXT NOT NULL DEFAULT '',"
            " status TEXT NOT NULL DEFAULT 'queued',"
            " step TEXT NOT NULL DEFAULT '',"
            " owner_id INTEGER NOT NULL, priority INTEGER NOT NULL DEFAULT 0,"
            " uploads_dir TEXT NOT NULL DEFAULT '', output_dir TEXT NOT NULL DEFAULT '',"
            " package_path TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',"
            " error TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, done_at TEXT,"
            " CHECK (status IN ('queued','running','packaging','done','failed')));")
        c.commit()
    finally:
        c.close()

    monkeypatch.setenv("PROTFORGE_WEB_DB", str(legacy))
    db.init_db()
    db.init_db()                     # 幂等：调两次不许炸
    c = sqlite3.connect(legacy)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    finally:
        c.close()
    assert {"acceptance", "declared", "followup"} <= cols, \
        "旧库没有补上验收那三列"


# ---------------------------------------------------------------- 工具


def _uid(username):
    return int(auth.get_user(username)["id"])
