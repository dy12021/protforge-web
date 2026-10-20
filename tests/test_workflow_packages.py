"""工作流级批量下载产物包。

为什么要有这个
--------------
结果表（``result.xlsx``）只是**状态与摘要**，真正的交付物在每行各自的产物包里
（结构、序列、脚本、报告）。一批 61 行就是一个一个下——其中一行就 90 MB，
下到什么时候是个问题；而且逐个下很容易漏掉几行，**漏掉的那些行在表上看着
像「跑过了」**。

这几条钉死的行为
----------------
- zip 里**每行一个子文件**，文件名带行号与源表关键列值（编号）；
- 没有包时 404（不给一个点了必然失败的入口）；
- 详情端点报**件数与总大小**（前端据此显示「N 个 · X MB」）；
- **别的任务的包进不来**：只认本任务自己的那一个（guards.resolve_package）。

夹具按**生产布局**造包（``data\\packages\\task_<id>.zip``）：写错布局的夹具
会替生产撒谎，这条测试就测不到生产的洞（tests/test_download_region.py 记着
同一个教训）。
"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

import app as svc
import artifacts
import auth
import db
import store

GOOD = "password-123"


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def client():
    """用户面板（8473 那个 app）的客户端。"""
    return TestClient(svc.create_public_app())


@pytest.fixture()
def owner():
    auth.create_user("pack-user", GOOD, role="user")
    row = auth.get_user("pack-user")
    return {"Authorization": "Bearer " + auth.issue_token(int(row["id"]))}, \
        int(row["id"])


def _write_source(path, rows=2) -> str:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "靶标名"])
    for i in range(rows):
        ws.append([f"KR{i + 1:04d}", f"靶标{chr(65 + i)}"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return str(path)


def _write_package(task_id: int, text: str = "算完了"):
    """按**生产布局**造产物包：data\\packages\\task_<id>.zip。"""
    pkg = artifacts.default_package_path(int(task_id))
    pkg.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("summary.txt", text)
    store.update_task(task_id, package_path=str(pkg))
    return pkg


@pytest.fixture()
def wf_with_packages(owner, tmp_path):
    """一个两行的工作流，两行都有产物包。"""
    _, owner_id = owner
    src = db.data_root() / "workflows" / "src.xlsx"
    source = _write_source(src, rows=2)
    wf = store.create_workflow(owner_id, "批量下载用表.xlsx", "算 {key}",
                               sheet="S1", source_path=source,
                               header_row=1, key_col=0, total_rows=2, writeback=1)
    wid = int(wf["id"])
    tids = []
    for i in range(2):
        t = store.create_task(f"第 {i + 1} 行", "brief", owner_id=owner_id,
                              actor="system", workflow_id=wid, workflow_row=i)
        tid = int(t["id"])
        store.set_status(tid, "done", actor="system", message="算完了")
        _write_package(tid, f"第 {i + 1} 行的结果")
        tids.append(tid)
    return {"wid": wid, "tids": tids, "owner": owner_id}


# ---------------------------------------------------------------- 围栏

def test_bulk_download_contains_one_zip_per_row(client, owner, wf_with_packages):
    """每行一个子文件，文件名带行号与源表里的编号。"""
    headers, _ = owner
    wid = wf_with_packages["wid"]
    r = client.get(f"/api/workflows/{wid}/packages", headers=headers)
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers.get("content-disposition", "")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = sorted(zf.namelist())
        assert names == ["第1行_KR0001.zip", "第2行_KR0002.zip"], names
        # 里面装的是**那一行自己的包**，不是空壳
        with zf.open("第1行_KR0001.zip") as inner:
            with zipfile.ZipFile(io.BytesIO(inner.read())) as izf:
                assert izf.read("summary.txt").decode() == "第 1 行的结果"


def test_bulk_download_404_when_there_is_no_package(client, owner, tmp_path):
    """没有包就 404——不给一个点了必然失败的入口。"""
    headers, owner_id = owner
    source = _write_source(db.data_root() / "workflows" / "empty.xlsx", rows=1)
    wf = store.create_workflow(owner_id, "空表.xlsx", "算 {key}", sheet="S1",
                               source_path=source, header_row=1, key_col=0,
                               total_rows=1, writeback=1)
    wid = int(wf["id"])
    t = store.create_task("第 1 行", "brief", owner_id=owner_id, actor="system",
                          workflow_id=wid, workflow_row=0)
    store.set_status(int(t["id"]), "done", actor="system", message="算完了")
    # 没有 package_path —— 打包还没产出
    r = client.get(f"/api/workflows/{wid}/packages", headers=headers)
    assert r.status_code == 404, r.text
    assert "产物包" in r.json()["detail"]


def test_detail_reports_package_count_and_bytes(client, owner, wf_with_packages):
    """详情端点要报件数与总大小：前端据此显示「N 个 · X MB」并决定显不显示入口。"""
    headers, _ = owner
    wid = wf_with_packages["wid"]
    r = client.get(f"/api/workflows/{wid}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["packages_count"] == 2, body
    expect = sum(artifacts.default_package_path(t).stat().st_size
                 for t in wf_with_packages["tids"])
    assert body["packages_bytes"] == expect, (body["packages_bytes"], expect)


def test_bulk_download_ignores_another_tasks_package(client, owner,
                                                     wf_with_packages):
    """**别的任务的包不能混进来**：package_path 指向别人的包时这一行不进 zip。

    判据是 guards.resolve_package：只认 ``packages\\task_<自己的 id>.zip``。
    少了这一条，一个把 package_path 写坏的行就能把别人的交付物打包带走。
    """
    headers, _ = owner
    wid = wf_with_packages["wid"]
    tids = wf_with_packages["tids"]
    # 把第 2 行的 package_path 指到第 1 行的包上
    store.update_task(tids[1], package_path=str(artifacts.default_package_path(tids[0])))
    r = client.get(f"/api/workflows/{wid}/packages", headers=headers)
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = sorted(zf.namelist())
    assert names == ["第1行_KR0001.zip"], names


def test_bulk_download_is_owner_scoped(client, owner, wf_with_packages):
    """别人的工作流：404，不泄露「这个工作流有没有包」。"""
    _, owner_id = owner
    auth.create_user("other-pack", GOOD, role="user")
    other = auth.get_user("other-pack")
    other_headers = {"Authorization": "Bearer " + auth.issue_token(int(other["id"]))}
    wid = wf_with_packages["wid"]
    r = client.get(f"/api/workflows/{wid}/packages", headers=other_headers)
    assert r.status_code == 404, r.text


def test_entry_name_carries_the_slot_values(client, owner, tmp_path):
    """文件名要带**用户在表里填的那几列**（slot），不然 61 个包没法认。

    只给「第N行_<关键列>」不够：真机那张表的关键列是「#」列（值 4/10/17…），
    用户看到「第3行_17.zip」根本不知道是什么靶标；带上 slot 值
    （种属/靶标名）就一眼认得出。
    """
    headers, owner_id = owner
    src = db.data_root() / "workflows" / "slots.xlsx"
    src.parent.mkdir(parents=True, exist_ok=True)
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "种属", "靶标名"])
    ws.append(["KR0101", "Human", "Cystatin C"])
    wb.save(src)
    wf = store.create_workflow(owner_id, "带 slot 的表.xlsx", "对{{种属}}的{{靶标}}做设计",
                               sheet="S1", source_path=str(src), header_row=1,
                               key_col=0, total_rows=1, writeback=1)
    wid = int(wf["id"])
    store.set_workflow_columns(wid, [
        {"col_index": 0, "header": "编号", "role": "key"},
        {"col_index": 1, "header": "种属", "role": "input", "slot": "种属"},
        {"col_index": 2, "header": "靶标名", "role": "input", "slot": "靶标"},
    ])
    t = store.create_task("第 1 行", "brief", owner_id=owner_id, actor="system",
                          workflow_id=wid, workflow_row=0)
    tid = int(t["id"])
    store.set_status(tid, "done", actor="system", message="算完了")
    _write_package(tid)

    r = client.get(f"/api/workflows/{wid}/packages", headers=headers)
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
    assert len(names) == 1, names
    assert "Cystatin C" in names[0], names
    assert "Human" in names[0], names
    assert names[0].startswith("第1行_"), names
