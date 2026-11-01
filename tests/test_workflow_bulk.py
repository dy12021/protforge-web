# -*- coding: utf-8 -*-
"""工作流级批量重跑/续跑。

为什么要有这个
--------------
一批 61 行的批次里，**模型额度耗尽会一次打掉连续二十多行**（真实案例：
第 42–61 行全是「计算结束了，但没有找到任何产物」，理由都是额度用完）。
逐行点重跑要点二十多次；更糟的是**每一行的原因都不一样**（有的缺表位、
有的产物不对、有的额度耗尽），统一一句话套给二十行等于什么都没说。

两条路各管各的
--------------
**重跑 = 崩溃行**（``status='failed'``）：从零重来。清结果包、清结论、
清收口标记，算力重烧一遍。产物文件**不删**（产物表按 (task_id, path) 去重，
新一轮的 discover 会把同一个目录更新回来），代价是两次运行的产物混在一起。

**续跑 = 被否决行**（``status='done'`` 且 ``acceptance='rejected'``）：带着
上一轮接着干。产物留着，每一行的提示词里带上**它自己的**审核理由，
再���上人写的统一补充要求。

**为什么不能混**：崩溃行没有可带的东西（没产物、只有一句错误），被否决行
有产物也有审核理由。给崩溃行「续跑」等于把半成品当上下文带进去；给被否决行
「重跑」等于把审核理由清掉、白跑一遍。

几条钉死的行为
----------------
- 「用户跳过的行」**不算崩溃行**：跳过在库里也是 failed，但用户的本意是
  「这行别跑」；
- finished 的工作流**必须改回 running** 才会放行重投的行（调度器闸门①）；
- **paused 的工作流不动**（那是用户主动停的，不能替他决定继续跑）；
- 管理员对别人的工作流一律 **404**（控制类端点，跟 start/pause 同口径）；
- 顺序是**先排队、再改工作流状态**：反过来的话，中间那一瞬调度器会看到
  「running 但没有 queued 行、cursor 已经到底」，直接把工作流判成 finished，
  刚排进去的行永远不会被认领。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as svc
import auth
import db
import store

GOOD = "password-123"


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def client():
    return TestClient(svc.create_public_app())


@pytest.fixture()
def owner():
    auth.create_user("bulk-user", GOOD, role="user")
    row = auth.get_user("bulk-user")
    return ({"Authorization": "Bearer " + auth.issue_token(int(row["id"]))},
            int(row["id"]))


@pytest.fixture()
def stranger():
    """另一个管理员。用来验「管理员对别人的工作流只读」。"""
    auth.create_user("bulk-admin", GOOD, role="admin")
    row = auth.get_user("bulk-admin")
    return ({"Authorization": "Bearer " + auth.issue_token(int(row["id"]))},
            int(row["id"]))


def _source(tmp_path: Path, rows: int = 6) -> str:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "靶标名"])
    for i in range(rows):
        ws.append([f"KR{i + 1:04d}", f"靶标{i + 1}"])
    p = db.data_root() / "workflows" / "bulk.xlsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(p)
    return str(p)


def _make_workflow(owner_id: int, rows: int = 6) -> dict:
    source = _source(db.data_root() / "workflows" / "bulk.xlsx", rows)
    wf = store.create_workflow(owner_id, "批量用表.xlsx", "算 {key}",
                               sheet="S1", source_path=source,
                               header_row=1, key_col=0, total_rows=rows,
                               writeback=1)
    return {"wid": int(wf["id"]), "owner_id": owner_id}


def _row(wf: dict, idx: int, status: str = "done", acceptance: str = "none",
         error: str = "", summary: str = "") -> int:
    """造一行。**行序号就是 idx**（0 起）。"""
    t = store.create_task(f"第 {idx + 1} 行", "brief", owner_id=wf["owner_id"],
                          actor="system", workflow_id=wf["wid"],
                          workflow_row=idx)
    tid = int(t["id"])
    if status in db.TERMINAL_STATUSES:
        store.set_status(tid, status, actor="system", message="算完了")
        store.update_task(tid, error=error, summary=summary,
                          package_path=f"packages/task_{tid}.zip")
        store.update_task(tid, workflow_archived=1)
        store.record_finished(tid, actor="system")
    if acceptance != "none":
        store.set_acceptance(tid, acceptance)
    return tid


# ---------------------------------------------------------------- 行判定


def test_bulk_targets_split_failed_and_rejected(owner):
    """崩溃行进重跑集合，被否决行进续跑集合，**互不重叠**。"""
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    failed = _row(wf, 0, "failed", error="计算结束了，但没有找到任何产物")
    rejected = _row(wf, 1, "done", db.ACCEPTANCE_REJECTED)
    plain = _row(wf, 2, "done", db.ACCEPTANCE_ACCEPTED)

    retry_rows = store.workflow_bulk_targets(wf["wid"], "retry")
    cont_rows = store.workflow_bulk_targets(wf["wid"], "continue")

    assert [int(r["id"]) for r in retry_rows] == [failed]
    assert [int(r["id"]) for r in cont_rows] == [rejected]
    assert plain not in [int(r["id"]) for r in retry_rows + cont_rows]


def test_bulk_targets_exclude_user_skipped_rows(owner):
    """**用户跳过的行不算崩溃行。**

    跳过在库里也是 failed（补偿任务建出来就是终态），但用户的本意是
    「这行别跑」——把它算进崩溃行去重跑，等于推翻用户的决定。
    """
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    crashed = _row(wf, 0, "failed", error="崩了")
    skipped = _row(wf, 1, "failed", error="用户跳过，未运行")
    store.skip_workflow_row(wf["wid"], 1, "这行序列是坏的")

    retry_rows = store.workflow_bulk_targets(wf["wid"], "retry")
    assert [int(r["id"]) for r in retry_rows] == [crashed], (
        "用户跳过的行被算进崩溃行了")


# ---------------------------------------------------------------- 重跑


def test_bulk_retry_requeues_rows_and_clears_the_markers(client, owner):
    """重跑要清掉**每一个**标记——漏一个的后果是静默的。"""
    headers, _ = owner
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    tid = _row(wf, 0, "failed", error="崩了", summary="半截结论")
    store.update_workflow(wf["wid"], status="finished")

    r = client.post(f"/api/workflows/{wf['wid']}/rows/retry-failed",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1

    t = store.get_task(tid, is_admin=True)
    assert t["status"] == "queued"
    assert not t["done_at"], "done_at 没清：这一行会被算成还在跑"
    assert not t["package_path"], "package_path 没清：新一轮的产物进不了包"
    assert not t["summary"], "summary 没清：上一轮的结论会混进新一轮"
    assert int(t["workflow_archived"]) == 0, (
        "workflow_archived 没清：新一轮的结果永远写不回结果表")
    assert not store.has_finished(tid), "收口标记没清：第二轮 finish 会被 422"


def test_bulk_retry_reopens_a_finished_workflow(client, owner):
    """finished 的工作流一行都不放行（调度器闸门①），所以必须改回 running。"""
    headers, _ = owner
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    _row(wf, 0, "failed", error="崩了")
    store.update_workflow(wf["wid"], status="finished")

    r = client.post(f"/api/workflows/{wf['wid']}/rows/retry-failed",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["workflow_reopened"] is True
    assert store.get_workflow(wf["wid"])["status"] == "running"


def test_bulk_retry_does_not_resume_a_paused_workflow(client, owner):
    """**paused 是用户主动停的**，不能替他决定继续跑。

    行照样排队（用户想重跑这件事是明确的），但工作流保持暂停——
    等他自己点「继续」。
    """
    headers, _ = owner
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    tid = _row(wf, 0, "failed", error="崩了")
    store.update_workflow(wf["wid"], status="paused")

    r = client.post(f"/api/workflows/{wf['wid']}/rows/retry-failed",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["workflow_reopened"] is False
    assert store.get_workflow(wf["wid"])["status"] == "paused"
    assert store.get_task(tid, is_admin=True)["status"] == "queued"


def test_bulk_retry_keeps_the_artifact_files(client, owner, tmp_path):
    """产物文件**不删**（新一轮的 discover 会把同一个目录更新回来）。

    代价是两次运行的产物混在一起——所以界面必须写明这一点。
    """
    headers, _ = owner
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    tid = _row(wf, 0, "failed", error="崩了")
    work = db.data_root() / "work" / str(tid)
    work.mkdir(parents=True, exist_ok=True)
    art = work / "result.pdb"
    art.write_text("ATOM\n", encoding="utf-8")
    store.artifacts.add(tid, str(art), "result.pdb", size=5)

    r = client.post(f"/api/workflows/{wf['wid']}/rows/retry-failed",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    assert art.is_file(), "产物文件被删了——这一轮白跑"


# ---------------------------------------------------------------- 续跑


def test_bulk_continue_carries_each_rows_own_reason(client, owner):
    """每一行带**它自己的**审核理由。

    一批被否决的行问题各不相同，统一一句话套给二十行等于什么都没说。
    """
    headers, _ = owner
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    a = _row(wf, 0, "done", db.ACCEPTANCE_REJECTED)
    b = _row(wf, 1, "done", db.ACCEPTANCE_REJECTED)
    store.add_task_review(a, "fail", "缺了表位配对分析", reviewer="llm")
    store.add_task_review(b, "fail", "产物全是下载的公开结构", reviewer="llm")

    r = client.post(f"/api/workflows/{wf['wid']}/rows/continue-rejected",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2

    fa = store.get_followup(a)
    fb = store.get_followup(b)
    assert "缺了表位配对分析" in fa
    assert "缺了表位配对分析" not in fb, "b 行的续跑里混进了 a 行的理由"
    assert "产物全是下载的公开结构" in fb


def test_bulk_continue_appends_the_shared_note(client, owner):
    """人写的统一补充要求，**每一行都要带上**。"""
    headers, _ = owner
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    tid = _row(wf, 0, "done", db.ACCEPTANCE_REJECTED)
    store.add_task_review(tid, "fail", "表位选错了", reviewer="llm")

    r = client.post(f"/api/workflows/{wf['wid']}/rows/continue-rejected",
                    headers=headers, json={"note": "统一按 IgG1 骨架重做"})
    assert r.status_code == 200, r.text
    text = store.get_followup(tid)
    assert "表位选错了" in text, "自动带的那条理由没了"
    assert "统一按 IgG1 骨架重做" in text, "人写的补充要求没进去"


def test_bulk_continue_keeps_artifacts_and_clears_the_package(client, owner):
    """续跑**保留产物**（工作目录不清），但结果包路径要清。"""
    headers, _ = owner
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    tid = _row(wf, 0, "done", db.ACCEPTANCE_REJECTED)
    work = db.data_root() / "work" / str(tid)
    work.mkdir(parents=True, exist_ok=True)
    art = work / "result.pdb"
    art.write_text("ATOM\n", encoding="utf-8")
    store.artifacts.add(tid, str(art), "result.pdb", size=5)

    r = client.post(f"/api/workflows/{wf['wid']}/rows/continue-rejected",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    t = store.get_task(tid, is_admin=True)
    assert art.is_file(), "续跑把产物删了——它本来就是要带着上一轮接着干"
    assert not t["package_path"], "package_path 没清：新一轮的产物进不了包"
    assert t["acceptance"] == db.ACCEPTANCE_NONE, (
        "验收状态没回到 none：新一轮算完了还会被当成已验收")


# ---------------------------------------------------------------- 计划与权限


def test_bulk_plan_reports_counts_and_rows(client, owner):
    """影响面：两个数、每行的行号、续跑那批还要带上理由。"""
    headers, _ = owner
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    _row(wf, 0, "failed", error="崩了")
    rej = _row(wf, 1, "done", db.ACCEPTANCE_REJECTED)
    store.add_task_review(rej, "fail", "表位不对", reviewer="llm")

    r = client.get(f"/api/workflows/{wf['wid']}/bulk-plan", headers=headers)
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    assert plan["retry_count"] == 1
    assert plan["continue_count"] == 1
    assert plan["retry"][0]["row"] == 1
    assert plan["continue"][0]["row"] == 2
    assert "表位不对" in plan["continue"][0]["reason"]


def test_admin_cannot_bulk_retry_someone_elses_workflow(client, stranger):
    """管理员对别人的工作流**只读**：控制类端点一律 404。

    404 而不是 403——403 等于承认这个 id 存在。
    """
    auth.create_user("bulk-owner2", GOOD, role="user")
    owner_row = auth.get_user("bulk-owner2")
    wf = _make_workflow(int(owner_row["id"]))
    _row(wf, 0, "failed", error="崩了")

    headers, _ = stranger
    r = client.post(f"/api/workflows/{wf['wid']}/rows/retry-failed",
                    headers=headers, json={})
    assert r.status_code == 404, r.text
    r = client.post(f"/api/workflows/{wf['wid']}/rows/continue-rejected",
                    headers=headers, json={})
    assert r.status_code == 404, r.text


def test_bulk_retry_on_a_workflow_without_crashed_rows_is_a_noop(client, owner):
    """没有崩溃行就什么都不做——**不报错**（界面不该给按钮，但接口要老实）。"""
    headers, _ = owner
    _, owner_id = owner
    wf = _make_workflow(owner_id)
    _row(wf, 0, "done", db.ACCEPTANCE_ACCEPTED)

    r = client.post(f"/api/workflows/{wf['wid']}/rows/retry-failed",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0


# ---------------------------------------------------------------- 界面契约

def test_workflow_page_has_the_bulk_controls():
    """工作流详情页要有那两颗按钮和一个统一补充要求的输入框。"""
    html = (Path(__file__).resolve().parent.parent / "web" / "workflow.html").read_text(
        encoding="utf-8")
    for uid in ("wf-bulk-retry", "wf-bulk-continue", "wf-bulk-note"):
        assert f'id="{uid}"' in html, f"界面少了 {uid}"


def test_workflow_js_asks_twice_before_bulk_action():
    """批量动作**必须两步确认**：第一下只说明影响面，第二下才发请求。"""
    js = (Path(__file__).resolve().parent.parent / "web" / "workflow.js").read_text(
        encoding="utf-8")
    assert "function armBulk" in js
    assert "bulkArmed" in js
    # 影响面要写在提示条里，不能只有一句「确定吗」
    assert "结果包与结论会清掉" in js, "重跑的影响面没有写出来"
    assert "审核理由" in js, "续跑的影响面没有写出来"
