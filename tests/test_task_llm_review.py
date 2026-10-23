"""机器审核（LLM 开新上下文专门审核，2026-10-23）。

这一层要钉住的是**几条容易悄悄错掉的约定**：

**审核方式三级**：任务 > 工作流 > 全局默认，空串 = 继承。
优先级散开就会「为什么这行是人工审的」答不上来。

**审核进程的身份**：独立工作目录、干净上下文、**只读**被审任务的产物、
**拿不到任何令牌**。它不是管理员，也不是某个任务的计算进程。

**结论的效力**：判通过 → 直接改成「验收通过」；判不通过 → 改成「验收没通过」；
**没跑成 → 一律不改**（机器挂了不该把任务算成不通过，也不该算成通过）。

围栏清单：

- ``test_review_mode_resolution_order``      三级优先级
- ``test_empty_review_mode_means_inherit``   空串 = 继承
- ``test_invalid_review_mode_is_rejected``   非法值被拒
- ``test_global_default_is_human``            没设过时是人工 + 条件判定
- ``test_sweep_only_picks_pending_llm_tasks`` 巡检只挑「该机器审且没审过」的
- ``test_brief_carries_requirement_and_artifacts``  brief 带着需求与产物
- ``test_review_agent_gets_no_token``         审核进程拿不到令牌
- ``test_review_agent_cannot_write_artifacts`` 只读根挡住了写入
- ``test_pass_marks_accepted``               判通过 → 验收通过
- ``test_fail_marks_rejected``               判不通过 → 验收没通过
- ``test_broken_review_leaves_acceptance_alone``  审核没跑成 → 不动验收状态
- ``test_manual_trigger_queues_review``       手动触发 = 切机器审 + 清结论 + 回到待验收
- ``test_detail_exposes_mode_and_llm_verdict`` 详情信封给出方式与机器结论
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as svc
import auth
import db
import store
import task_reviewer

#: 值班台（admin/app_admin.py）在 admin/ 目录下，server/ 由 conftest 加上。
#: **本文件要自己加**：否则单跑这个文件时 import app_admin 会 ModuleNotFoundError
#: ——而它只在跟别的测试文件一起跑时恰好能用（别的文件加过 sys.path）。
ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))


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


def _uid(username):
    return int(auth.get_user(username)["id"])


@pytest.fixture()
def alice():
    return _token("alice")


@pytest.fixture()
def root():
    return _token("root", role="admin")


def _done_task(owner_id, *, summary="算完了，结论在这里", size=128,
               name="result.pdb", brief="原始需求：算一下这个蛋白的表面面积"):
    """造一个「算完了、待验收」的任务。"""
    task = store.create_task("第 1 行", brief, owner_id=owner_id, actor="human")
    tid = int(task["id"])
    # **绝对路径**：相对路径会让测试往仓库根目录落文件（work/<id>/…），
    # 而审核模块要按真实路径去拷副本，相对路径根本没法定位源。
    store.artifacts.add(tid, str(db.data_root() / "work" / str(tid) / name),
                        name, size=size)
    store.update_task(tid, summary=summary,
                      package_path=f"packages/task_{tid}.zip")
    store.set_status(tid, "done", actor="system", message="算完了")
    store.set_acceptance(tid, db.ACCEPTANCE_PENDING)
    return store.get_task(tid, is_admin=True)


def _fake_spawn(verdict=None, *, reason="看着都对", error="", write=True):
    """造一个假的审核进程：把 verdict.json 写出来就算跑完。"""
    calls: list[dict] = []

    def spawn(brief_path, work_dir, timeout, readonly_roots):
        calls.append({"brief_path": Path(brief_path),
                      "work_dir": Path(work_dir),
                      "timeout": timeout,
                      "readonly_roots": [Path(p) for p in readonly_roots]})
        if write and verdict is not None:
            (Path(work_dir) / "verdict.json").write_text(
                json.dumps({"verdict": verdict, "reason": reason},
                           ensure_ascii=False),
                encoding="utf-8")
        return {"ok": not error, "returncode": 0 if not error else 1,
                "output": "fake", "error": error}

    spawn.calls = calls
    return spawn


# ---------------------------------------------------------------- 方式解析


def test_global_default_is_human():
    """没设过时是「人工 + 条件判定」。**不是机器审**——
    平台不能一装上就自作主张替人验收。"""
    assert store.get_global_review_mode() == db.REVIEW_MODE_HUMAN


def test_review_mode_resolution_order(alice):
    """三级优先级：任务 > 工作流 > 全局默认。"""
    task = _done_task(_uid("alice"))

    # 只有全局默认
    store.set_setting(store.REVIEW_MODE_KEY, db.REVIEW_MODE_LLM)
    assert store.resolve_review_mode(task) == (db.REVIEW_MODE_LLM, "global")

    # 工作流盖过全局
    wf = store.create_workflow(_uid("alice"), "批次", "模板 {{x}}",
                               review_mode=db.REVIEW_MODE_HUMAN)
    store.update_task(task["id"], workflow_id=int(wf["id"]))
    task = store.get_task(task["id"], is_admin=True)
    assert store.resolve_review_mode(task) == (db.REVIEW_MODE_HUMAN, "workflow")

    # 任务再盖过工作流
    store.update_task(task["id"], review_mode=db.REVIEW_MODE_LLM)
    task = store.get_task(task["id"], is_admin=True)
    assert store.resolve_review_mode(task) == (db.REVIEW_MODE_LLM, "task")


def test_empty_review_mode_means_inherit(alice):
    """空串 = 继承上一层，**不是**「没有方式」。"""
    task = _done_task(_uid("alice"))
    store.set_setting(store.REVIEW_MODE_KEY, db.REVIEW_MODE_LLM)
    store.update_task(task["id"], review_mode="")
    task = store.get_task(task["id"], is_admin=True)
    mode, source = store.resolve_review_mode(task)
    assert mode == db.REVIEW_MODE_LLM
    assert source == "global"


def test_invalid_review_mode_is_rejected():
    """非法值必须被拒：写错了方式却静默当成人工，等于机器没审过。"""
    with pytest.raises(store.Invalid):
        store.normalize_review_mode("gpt")
    assert store.normalize_review_mode("") == ""
    assert store.normalize_review_mode("llm") == "llm"


# ---------------------------------------------------------------- 巡检


def test_sweep_only_picks_pending_llm_tasks(alice):
    """巡检只挑「算完了、等人验收、该走机器审、还没审过」的。"""
    store.set_setting(store.REVIEW_MODE_KEY, db.REVIEW_MODE_LLM)

    todo = _done_task(_uid("alice"))
    # 还在跑的不管
    running = store.create_task("在跑", "需求", owner_id=_uid("alice"),
                                actor="human")
    # 人工审的不管
    human = _done_task(_uid("alice"))
    store.update_task(human["id"], review_mode=db.REVIEW_MODE_HUMAN)
    # 已经审过的不管
    reviewed = _done_task(_uid("alice"))
    store.add_task_review(reviewed["id"], "pass", "过了")

    picked = {int(r["id"]) for r in store.list_tasks_needing_llm_review()}
    assert int(todo["id"]) in picked
    assert int(running["id"]) not in picked
    assert int(human["id"]) not in picked
    assert int(reviewed["id"]) not in picked


# ---------------------------------------------------------------- brief


def test_brief_carries_requirement_and_artifacts(alice):
    """brief 带着**原始需求 + 产物清单 + 这一轮的结论**。

    判据就这三样：不另设「验收标准」输入项——需求本身写清楚时，
    再让人填一遍验收标准只是多一道手续。
    """
    task = _done_task(_uid("alice"))
    brief = task_reviewer.build_review_brief(task)
    assert "原始需求" in brief
    assert "算一下这个蛋白的表面面积" in brief
    assert "result.pdb" in brief
    assert "算完了，结论在这里" in brief
    # 「只许写自己的目录」这件事必须写在 brief 里：守卫会挡，但模型得知道。
    assert "只能写你自己的工作目录" in brief


def test_brief_gives_the_reviewer_a_copy_not_the_original(alice, tmp_path):
    """审核进程拿到的是**副本**，原件的路径不出现在 brief 里。

    这是整件事的安全底座：交付物是这一批唯一拿得出手的东西，
    让审核进程**结构上碰不到**它，比「靠守卫保证它不去动」可靠得多。
    """
    task = _done_task(_uid("alice"))
    arts = store.artifacts.list(task["id"])
    src = Path(str(arts[0]["path"]))
    # 造一个真实存在的产物文件，让拷贝这一步有东西可拷
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("ATOM 1 N MET ALA\n", encoding="utf-8")
    try:
        copied = task_reviewer._copy_artifacts(arts, tmp_path / "copies")
        assert copied["copied"], "产物应当被拷进审核环境"
        dst = tmp_path / "copies" / copied["copied"][0]["rel"]
        assert dst.is_file()
        brief = task_reviewer.build_review_brief(task, arts, copied)
        assert "副本" in brief
        assert str(src) not in brief, "brief 里不能出现原件的路径"
    finally:
        try:
            src.unlink()
        except OSError:
            pass


def test_touched_artifact_voids_the_verdict(monkeypatch, alice, tmp_path):
    """审核期间原件被动过 → **结论作废**，验收状态不动。

    副本让这件事几乎不可能发生，但「几乎」不够：交付物只有一份。
    这一条是那份「宁可漏判不可误判」的兜底——宁可不给结论，
    也不能拿一份被动过的东西当验收依据。
    """
    task = _done_task(_uid("alice"))
    arts = store.artifacts.list(task["id"])
    src = Path(str(arts[0]["path"]))
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("ATOM 1 N MET ALA\n", encoding="utf-8")

    def spawn(brief_path, work_dir, timeout, readonly_roots):
        # 模拟「审核进程动了原件」
        src.write_text("ATOM 1 N MET GLY\n", encoding="utf-8")
        (Path(work_dir) / "verdict.json").write_text(
            json.dumps({"verdict": "pass", "reason": "看着都对"}),
            encoding="utf-8")
        return {"ok": True, "returncode": 0, "output": "", "error": ""}

    try:
        monkeypatch.setattr(task_reviewer, "_spawn_review_agent", spawn)
        res = task_reviewer.run_llm_review(task["id"])
        assert res["verdict"] == "error"
        updated = store.get_task(task["id"], is_admin=True)
        assert updated["acceptance"] == db.ACCEPTANCE_PENDING
    finally:
        try:
            src.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 进程身份


def test_review_agent_gets_no_token(monkeypatch, alice):
    """审核进程**拿不到任何令牌**：它不是管理员，也不是某个任务的计算进程。"""
    task = _done_task(_uid("alice"))
    seen: dict = {}

    def fake_run(cmd, env=None, cwd=None, capture_output=None, timeout=None,
                 **kwargs):
        seen.update(env or {})
        (Path(env["PROTFORGE_WORK_DIR"]) / "verdict.json").write_text(
            json.dumps({"verdict": "pass", "reason": "ok"}), encoding="utf-8")
        class P:
            returncode = 0
            stdout = b""
            stderr = b""
        return P()

    monkeypatch.setattr(task_reviewer.subprocess, "run", fake_run)
    monkeypatch.setenv("PROTFORGE_AGENT_TOKEN", "should-not-leak")
    monkeypatch.setenv("PROTFORGE_TASK_ID", "999")
    task_reviewer.run_llm_review(task["id"])

    assert "PROTFORGE_AGENT_TOKEN" not in seen
    assert "PROTFORGE_TASK_ID" not in seen
    assert seen["PROTFORGE_WEB_REVIEW_MODE"] == "task"
    # 只读根：被审任务的工作目录
    assert seen["PROTFORGE_WEB_REVIEW_READONLY_ROOTS"]


def test_review_agent_cannot_write_artifacts(tmp_path, monkeypatch, alice):
    """只读根挡住了「往被审任务的工作目录里写东西」。

    守卫在 agent 侧（guards.mjs），这里验的是**接线**：只读根真的传下去了，
    而且指的是被审任务的工作目录。
    """
    task = _done_task(_uid("alice"))
    seen: dict = {}

    def fake_run(cmd, env=None, cwd=None, capture_output=None, timeout=None,
                 **kwargs):
        seen.update(env or {})
        class P:
            returncode = 0
            stdout = b""
            stderr = b""
        return P()

    monkeypatch.setattr(task_reviewer.subprocess, "run", fake_run)
    task_reviewer.run_llm_review(task["id"])
    roots = seen["PROTFORGE_WEB_REVIEW_READONLY_ROOTS"].split(";")
    assert str(task_reviewer.guards.regions(task["id"])[
        task_reviewer.guards.WORK]) in roots


# ---------------------------------------------------------------- 结论效力


def test_pass_marks_accepted(monkeypatch, alice):
    """判通过 → 直接改成「验收通过」，并留档。"""
    task = _done_task(_uid("alice"))
    monkeypatch.setattr(task_reviewer, "_spawn_review_agent",
                        _fake_spawn("pass", reason="产物齐、结论对得上"))
    res = task_reviewer.run_llm_review(task["id"])

    assert res["verdict"] == "pass"
    updated = store.get_task(task["id"], is_admin=True)
    assert updated["acceptance"] == db.ACCEPTANCE_ACCEPTED
    assert store.is_accepted(updated) is True

    rec = store.latest_task_review(task["id"])
    assert rec["verdict"] == "pass"
    assert rec["auto_applied"] == 1
    assert "产物齐" in rec["reason"]
    msgs = " ".join(e["message"] for e in store.list_events(task["id"]))
    assert "机器审核通过" in msgs


def test_fail_marks_rejected(monkeypatch, alice):
    """判不通过 → 改成「验收没通过」，理由跟着走。"""
    task = _done_task(_uid("alice"))
    monkeypatch.setattr(task_reviewer, "_spawn_review_agent",
                        _fake_spawn("fail", reason="缺了表位那部分"))
    res = task_reviewer.run_llm_review(task["id"])

    assert res["verdict"] == "fail"
    updated = store.get_task(task["id"], is_admin=True)
    assert updated["acceptance"] == db.ACCEPTANCE_REJECTED
    rec = store.latest_task_review(task["id"])
    assert rec["auto_applied"] == 1
    msgs = " ".join(e["message"] for e in store.list_events(task["id"]))
    assert "机器审核没通过" in msgs


def test_broken_review_leaves_acceptance_alone(monkeypatch, alice):
    """审核没跑成 → **一律不动验收状态**。

    机器挂了就该说机器挂了：既不能把任务算成不通过（那是冤枉它），
    也不能算成通过（那是把坏结果当交付）。
    """
    task = _done_task(_uid("alice"))
    monkeypatch.setattr(task_reviewer, "_spawn_review_agent",
                        _fake_spawn(None, write=False, error="审核进程起不来"))
    res = task_reviewer.run_llm_review(task["id"])

    assert res["verdict"] == "error"
    updated = store.get_task(task["id"], is_admin=True)
    assert updated["acceptance"] == db.ACCEPTANCE_PENDING, \
        "审核没跑成，验收状态必须原样留在待验收"
    rec = store.latest_task_review(task["id"])
    assert rec["verdict"] == "error"
    assert rec["auto_applied"] == 0


def test_unrecognizable_verdict_is_an_error(monkeypatch, alice):
    """结论文件里的 verdict 认不出来 → 当没跑成，**不猜**。"""
    task = _done_task(_uid("alice"))

    def spawn(brief_path, work_dir, timeout, readonly_roots):
        (Path(work_dir) / "verdict.json").write_text(
            json.dumps({"verdict": "大概可以吧"}), encoding="utf-8")
        return {"ok": True, "returncode": 0, "output": "", "error": ""}

    monkeypatch.setattr(task_reviewer, "_spawn_review_agent", spawn)
    res = task_reviewer.run_llm_review(task["id"])
    assert res["verdict"] == "error"
    updated = store.get_task(task["id"], is_admin=True)
    assert updated["acceptance"] == db.ACCEPTANCE_PENDING


# ---------------------------------------------------------------- 接口


def test_manual_trigger_queues_review(client, alice):
    """手动触发 = 切到机器审 + 清掉旧结论 + 回到「待验收」。

    审核本身是**异步**的：它要 spawn 一个独立进程跑几分钟，
    放在请求里就是让页面转圈几分钟。
    """
    task = _done_task(_uid("alice"))
    store.add_task_review(task["id"], "pass", "上一次判过了")
    store.set_acceptance(task["id"], db.ACCEPTANCE_ACCEPTED)

    r = client.post(f"/api/tasks/{task['id']}/llm-review",
                    headers=_headers(alice), json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] is True
    assert body["cleared"] == 1
    # 动作端点一律返回详情信封（前端经 detailTask 拆）
    assert set(body) >= {"task", "events", "artifacts"}, sorted(body)
    assert body["task"]["id"] == int(task["id"])

    updated = store.get_task(task["id"], is_admin=True)
    assert updated["review_mode"] == db.REVIEW_MODE_LLM
    assert updated["acceptance"] == db.ACCEPTANCE_PENDING
    assert store.latest_task_review(task["id"]) is None


def test_detail_exposes_mode_and_llm_verdict(client, alice):
    """详情信封给出：这一轮用哪种方式审、机器说了什么。"""
    task = _done_task(_uid("alice"))
    store.update_task(task["id"], review_mode=db.REVIEW_MODE_LLM)
    store.add_task_review(task["id"], "fail", "少了表位", auto_applied=1)

    r = client.get(f"/api/tasks/{task['id']}", headers=_headers(alice))
    assert r.status_code == 200, r.text
    rv = r.json()["review"]
    assert rv["mode"] == db.REVIEW_MODE_LLM
    assert rv["mode_source"] == "task"
    assert rv["llm"]["verdict"] == "fail"
    assert rv["llm"]["reason"] == "少了表位"


def test_patch_task_review_mode(client, alice):
    """任务级覆盖：PATCH 改验收方式。空串 = 回到继承。"""
    task = _done_task(_uid("alice"))
    r = client.patch(f"/api/tasks/{task['id']}", headers=_headers(alice),
                     json={"review_mode": "llm"})
    assert r.status_code == 200, r.text
    assert store.get_task(task["id"], is_admin=True)["review_mode"] == "llm"

    r = client.patch(f"/api/tasks/{task['id']}", headers=_headers(alice),
                     json={"review_mode": ""})
    assert r.status_code == 200, r.text
    assert store.get_task(task["id"], is_admin=True)["review_mode"] == ""


def test_patch_workflow_review_mode(client, alice):
    """工作流级：整批设一次，不用逐行点。"""
    wf = store.create_workflow(_uid("alice"), "批次", "模板 {{x}}")
    r = client.patch(f"/api/workflows/{wf['id']}", headers=_headers(alice),
                     json={"review_mode": "llm"})
    assert r.status_code == 200, r.text
    assert store.get_workflow(int(wf["id"]), is_admin=True)["review_mode"] == "llm"


def test_dispatcher_review_pass_queues_the_task(monkeypatch, alice):
    """调度器的巡检只入队，真正的审核在后台线程上跑。

    **审核不能占调度器的并发槽**——那几条槽是给真正的计算任务用的；
    审核是「事后看结果」，不该把正在排队的计算挡住。
    """
    import dispatcher

    store.set_setting(store.REVIEW_MODE_KEY, db.REVIEW_MODE_LLM)
    task = _done_task(_uid("alice"))

    d = dispatcher.Dispatcher(db, max_concurrency=2, node_exe="node",
                              gpu_probe=lambda: 2)
    ran: list[int] = []

    def fake_review(task_id, **kw):
        ran.append(int(task_id))
        return {"verdict": "pass", "reason": "ok"}

    monkeypatch.setattr(task_reviewer, "run_llm_review", fake_review)
    try:
        assert d._review_pass() == 1
        for _ in range(100):
            if ran:
                break
            time.sleep(0.05)
    finally:
        d._review_stop()
    assert ran == [int(task["id"])]


def test_admin_panel_review_endpoints(root, alice):
    """值班台：改全局默认、改某一轮的方式、触发机器审核。"""
    import app_admin

    panel = TestClient(app_admin.create_admin_app())

    r = panel.put("/api/admin/review-settings", headers=_headers(root),
                  json={"review_mode": "llm"})
    assert r.status_code == 200, r.text
    assert store.get_global_review_mode() == db.REVIEW_MODE_LLM

    task = _done_task(_uid("alice"))
    r = panel.patch(f"/api/admin/tasks/{task['id']}/review-mode",
                    headers=_headers(root), json={"review_mode": "human"})
    assert r.status_code == 200, r.text
    assert store.get_task(task["id"], is_admin=True)["review_mode"] == "human"

    r = panel.post(f"/api/admin/tasks/{task['id']}/llm-review",
                   headers=_headers(root), json={})
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True
    assert store.get_task(task["id"], is_admin=True)["review_mode"] == "llm"

    # 详情里要能看到机器结论的位置（没审过就是 None）
    r = panel.get(f"/api/admin/tasks/{task['id']}", headers=_headers(root))
    assert r.status_code == 200, r.text
    rv = r.json()["review"]
    assert rv["mode_source"] == "task"
    assert rv["llm"] is None


def test_global_default_only_admin_can_change(client, alice, root):
    """全局默认是平台级设置：普通用户改不了。"""
    r = client.put("/api/review-settings", headers=_headers(alice),
                   json={"review_mode": "llm"})
    assert r.status_code == 403, r.text
    assert store.get_global_review_mode() == db.REVIEW_MODE_HUMAN

    r = client.put("/api/review-settings", headers=_headers(root),
                   json={"review_mode": "llm"})
    assert r.status_code == 200, r.text
    assert store.get_global_review_mode() == db.REVIEW_MODE_LLM
