# -*- coding: utf-8 -*-
"""工作流的**运行台账**与**交付包**。

台账回答的是运行层面的问题——哪一行崩了、为什么、跑了多久、审没审过。
结果表（``result.xlsx``）回答的是科学问题（这个靶标算出了什么）。
两张表**行号对齐**：同一个「第 42 行」在两张表里说的是同一件事。

几条钉死的行为
----------------
- **行号与结果表对齐**，且**没跑过的行也在表里**——「哪些行还没跑」
  本身就是交付的一部分；
- 耗时是**真实耗时**（认领那一刻到结束），**不是** created_at 到结束：
  工作流把整批行任务先建出来再串行跑，拿 created_at 当起点会把
  「排队等前面的行」也算进去（实测某批 61 行的表：单行中位 8.4 小时，
  其中大半是在等前面的行）；
- 「用户跳过的行」标**已跳过**，不是失败；
- 机器审核的结论与理由**在表里**，「没审过」也是一种状态；
- 交付包 = 各行产物包 + **四份说明**（交付说明 / 逐行索引 / 运行台账 /
  机器审核结论）。只给产物包的话，拿到手的人不知道哪行崩了、为什么。
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app as svc
import auth
import db
import ledger
import store

GOOD = "password-123"


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def client():
    return TestClient(svc.create_public_app())


@pytest.fixture()
def owner():
    auth.create_user("ledger-user", GOOD, role="user")
    row = auth.get_user("ledger-user")
    return ({"Authorization": "Bearer " + auth.issue_token(int(row["id"]))},
            int(row["id"]))


def _source(rows: int = 4) -> str:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "靶标名"])
    for i in range(rows):
        ws.append([f"KR{i + 1:04d}", f"靶标{i + 1}"])
    p = db.data_root() / "workflows" / "ledger.xlsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(p)
    return str(p)


def _workflow(owner_id: int, rows: int = 4) -> dict:
    wf = store.create_workflow(owner_id, "台账用表.xlsx", "算 {key}",
                               sheet="S1", source_path=_source(rows),
                               header_row=1, key_col=0, total_rows=rows,
                               writeback=1)
    return {"wid": int(wf["id"]), "owner_id": owner_id}


def _row(wf: dict, idx: int, status: str = "done", acceptance: str = "none",
         error: str = "", summary: str = "", ran: bool = False) -> int:
    t = store.create_task(f"第 {idx + 1} 行", "brief", owner_id=wf["owner_id"],
                          actor="system", workflow_id=wf["wid"],
                          workflow_row=idx)
    tid = int(t["id"])
    if ran:
        store.set_status(tid, "running", actor="system", message="开始跑")
    if status in db.TERMINAL_STATUSES:
        store.set_status(tid, status, actor="system", message="算完了")
        store.update_task(tid, error=error, summary=summary)
    if acceptance != "none":
        store.set_acceptance(tid, acceptance)
    return tid


def _backdate_running_event(task_id: int, hours: float) -> None:
    """把「开始跑」那条事件往前挪——用来验证耗时算的是运行段而不是排队段。"""
    c = db.connect()
    try:
        when = (datetime.now() - timedelta(hours=hours)).isoformat()
        with c:
            c.execute(
                "UPDATE events SET created_at = ? WHERE task_id = ?"
                " AND kind = 'status' AND to_status = 'running'",
                (when, int(task_id)))
    finally:
        c.close()


# ---------------------------------------------------------------- 台账


def test_ledger_has_one_row_per_workflow_row_including_unrun(owner):
    """**行号与结果表对齐**，没跑过的行也在表里。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=4)
    _row(wf, 0, "done")
    _row(wf, 2, "failed", error="计算结束了，但没有找到任何产物")
    rows = ledger.build_rows(wf["wid"])
    assert [r["行号"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["状态"] == "完成"
    assert rows[1]["状态"] == "未开始", "没跑过的行也得在表里"
    assert rows[2]["状态"] == "失败"
    assert rows[2]["失败原因"] == "计算结束了，但没有找到任何产物"


def test_ledger_carries_the_source_key_and_target(owner):
    """编号与靶标来自源表——不然「第 42 行」对不上表。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=2)
    store.set_workflow_columns(wf["wid"], [
        {"col_index": 0, "header": "编号", "role": "key"},
        {"col_index": 1, "header": "靶标名", "role": "input", "slot": "靶标"},
    ])
    rows = ledger.build_rows(wf["wid"])
    assert rows[0]["编号"] == "KR0001"
    assert rows[0]["靶标"] == "靶标1"


def test_ledger_elapsed_is_running_time_not_queue_time(owner):
    """耗时是**运行段**，不是「建任务到结束」。

    工作流把整批行任务先建出来再串行跑：拿 created_at 当起点，
    会把「排队等前面的行」也算进这一行的用时里。
    """
    _, owner_id = owner
    wf = _workflow(owner_id, rows=1)
    tid = _row(wf, 0, "done", ran=True)
    _backdate_running_event(tid, hours=2)
    rows = ledger.build_rows(wf["wid"])
    assert rows[0]["耗时(分钟)"] != "", "有 running 事件却没算出耗时"
    assert 110 <= float(rows[0]["耗时(分钟)"]) <= 130, rows[0]["耗时(分钟)"]


def test_ledger_has_no_elapsed_for_a_row_that_never_ran(owner):
    """没跑过的行**不计时**——created_at 不是它的起点。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=1)
    _row(wf, 0, "done")
    rows = ledger.build_rows(wf["wid"])
    assert rows[0]["耗时(分钟)"] == ""


def test_ledger_marks_user_skipped_rows(owner):
    """用户跳过的行标**已跳过**，不是失败。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=2)
    _row(wf, 0, "failed", error="用户跳过，未运行")
    store.skip_workflow_row(wf["wid"], 0, "这行序列是坏的")
    rows = ledger.build_rows(wf["wid"])
    assert rows[0]["状态"] == "已跳过"
    assert rows[1]["状态"] == "未开始"


def test_ledger_carries_the_machine_review_verdict_and_reason(owner):
    """机器审核的结论与理由在表里；**没审过也是一种状态**。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=2)
    a = _row(wf, 0, "done", db.ACCEPTANCE_REJECTED)
    store.add_task_review(a, "fail", "缺了表位配对分析", reviewer="llm")
    _row(wf, 1, "done")
    rows = ledger.build_rows(wf["wid"])
    assert rows[0]["机器审核"] == "fail"
    assert "缺了表位配对分析" in rows[0]["审核理由"]
    assert rows[0]["验收"] == "打回"
    assert rows[1]["机器审核"] == "", "没审过的行不该有结论"


def test_ledger_records_when_the_review_happened(owner):
    """台账要记**审核时间**。

    这一行后来又重跑过的话，最近一次审核是**上一轮**的结论——台账里
    「机器审核 fail / 验收 未审」看着像自相矛盾，有了时间才看得出
    那是哪一轮的事。
    """
    _, owner_id = owner
    wf = _workflow(owner_id, rows=1)
    tid = _row(wf, 0, "done", db.ACCEPTANCE_REJECTED)
    store.add_task_review(tid, "fail", "缺了表位配对分析", reviewer="llm")
    rows = ledger.build_rows(wf["wid"])
    assert rows[0]["审核时间"], "没记审核时间"
    assert rows[0]["审核时间"].startswith("20"), rows[0]["审核时间"]


def test_ledger_counts_artifacts_and_package_size(owner):
    """产物数与产物包大小——「交上来了多少东西」也是交付的一部分。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=1)
    tid = _row(wf, 0, "done")
    work = db.data_root() / "work" / str(tid)
    work.mkdir(parents=True, exist_ok=True)
    (work / "a.pdb").write_text("ATOM\n", encoding="utf-8")
    (work / "b.fasta").write_text(">x\nACGT\n", encoding="utf-8")
    store.artifacts.add(tid, str(work / "a.pdb"), "a.pdb", size=5)
    store.artifacts.add(tid, str(work / "b.fasta"), "b.fasta", size=6)
    pkg = db.data_root() / "packages" / f"task_{tid}.zip"
    pkg.parent.mkdir(parents=True, exist_ok=True)
    pkg.write_bytes(b"PK\x03\x04" + b"0" * (1536 * 1024))
    store.update_task(tid, package_path=str(pkg))

    rows = ledger.build_rows(wf["wid"])
    assert rows[0]["产物数"] == 2
    assert rows[0]["产物包(MB)"] > 0


# ---------------------------------------------------------------- 端点


def test_ledger_endpoint_returns_an_xlsx(client, owner):
    """台账端点返回**能打开的 xlsx**，且有表头。"""
    headers, _ = owner
    _, owner_id = owner
    wf = _workflow(owner_id, rows=2)
    _row(wf, 0, "done")
    r = client.get(f"/api/workflows/{wf['wid']}/ledger.xlsx", headers=headers)
    assert r.status_code == 200, r.text
    assert "spreadsheetml" in r.headers.get("content-type", ""), r.headers
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "xl/workbook.xml" in zf.namelist(), zf.namelist()


def test_ledger_endpoint_is_owner_scoped(client, owner):
    """别人的工作流：404，不泄露「这个工作流有没有台账」。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=1)
    auth.create_user("other-ledger", GOOD, role="user")
    other = auth.get_user("other-ledger")
    other_headers = {"Authorization": "Bearer "
                      + auth.issue_token(int(other["id"]))}
    r = client.get(f"/api/workflows/{wf['wid']}/ledger.xlsx",
                   headers=other_headers)
    assert r.status_code == 404, r.text


def test_delivery_readme_says_which_rows_failed(owner, tmp_path):
    """交付说明要**写清哪些行崩了、为什么**——只给产物包是不够的。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=3)
    _row(wf, 0, "done")
    _row(wf, 1, "failed", error="模型额度已耗尽")
    _row(wf, 2, "failed", error="模型额度已耗尽")
    rows = ledger.build_rows(wf["wid"])
    readme = ledger.build_readme(wf["wid"], rows)
    assert "失败" in readme
    assert "2" in readme, "失败行数要写出来"
    assert "里面有什么" in readme


def test_delivery_review_conclusions_include_the_unreviewed_rows(owner):
    """「没审过的行」也要写进机器审核结论——那本身就是要交付的事实。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=2)
    a = _row(wf, 0, "done", db.ACCEPTANCE_REJECTED)
    store.add_task_review(a, "fail", "缺了表位配对分析", reviewer="llm")
    _row(wf, 1, "done")
    rows = ledger.build_rows(wf["wid"])
    md = ledger.build_review_conclusions(wf["wid"], rows)
    assert "缺了表位配对分析" in md
    assert "没审过" in md
    assert "没跑成" in md, "要说明「没跑成」不等于「不通过」"


def test_ledger_index_csv_has_a_bom_so_excel_opens_it(owner):
    """逐行索引是 **UTF-8 带 BOM**——Excel 双击直接开不乱码。"""
    _, owner_id = owner
    wf = _workflow(owner_id, rows=1)
    _row(wf, 0, "done")
    text = ledger.build_index_csv(ledger.build_rows(wf["wid"]))
    assert text.startswith("行号,"), text[:60]
    assert "\ufeff" not in text, "BOM 由调用方加，模块本身不加"
