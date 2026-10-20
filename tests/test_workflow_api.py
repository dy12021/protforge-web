"""工作流的 HTTP 层（规格 9.1 / 9.2 / 6.5 洞三）。

三件事最容易做漏，各有专门用例：

- **归属校验**：越权一律 404 不 403。``result.xlsx`` 是风险最高的一个——
  它绕过所有列表层过滤，且是唯一返回非 JSON 的端点。
- **scope 不是越权**：``scope=all`` 是「行任务也算进来」不是「所有人的都
  算进来」，两个正交维度。
- **上传校验不留痕**：空文件 / .sh / 魔数不对，库里都不能有记录，staging
  目录也不能留下半截文件。

几条**上游裁定**在本文件里有钉死项（逐条对应 brief 的编号）：

1. 越权 404、响应体不含可区分信息（``test_detail_cross_user_matches_unknown_id``）
2. 管理员对别人的工作流只读（``test_admin_can_read_but_not_control``）
3. 删除的归属校验在 ``_abort_live_rows`` **之前**（``test_delete_is_owner_scoped``
   断言别人的行任务**仍是 queued**，没被中止过）
4. 不从请求体透传 ``workflow_id``（``test_skip_ignores_workflow_id_in_body``）
5. ``{action}`` 白名单（``test_unknown_action_changes_nothing``）
6. ``scope`` 白名单（``test_scope_invalid_is_422``）
7. ``writeback`` **是**可开关的（规格 2 决策表 / 7.1 / 9.1）：接口接受、视图
   返回、真的落库，归档路径也真的读它（``test_writeback_switch_is_accepted_and_returned``、
   ``test_result_xlsx_is_404_when_writeback_is_off``）
8. 行任务身份：归属工作流的主人、事件 actor=system（调度器侧已钉，这里钉
   「HTTP 层不许改归属、不许写非法 actor」）
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import app as svc
import auth
import db
import store
import workflow as wfmod

XLSX_MIME = ("application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet")
ROWS = [["编号", "序列", "项目"],
        ["1", "AAA", "XBR"],
        ["2", "BBB", "XBR"]]


@pytest.fixture()
def client():
    """用户面板（8473 那个 app）。不进 `with`——create_public_app() 没有
    lifespan，TestClient 的上下文管理器会顺手去跑它。"""
    return TestClient(svc.create_public_app())


def _token(username, role="user"):
    """真实发令牌，不用 dev-login 旁路——旁路开着等于这个进程所有接口都不要凭证，
    而这里要验的恰恰是**归属校验**，走旁路就没有归属可言了。"""
    auth.create_user(username, "pw-for-tests", role=role)
    row = auth.get_user(username)
    return auth.issue_token(int(row["id"]))


@pytest.fixture()
def auth_headers():
    return {"Authorization": "Bearer " + _token("alice")}


@pytest.fixture()
def other_headers():
    return {"Authorization": "Bearer " + _token("bob")}


@pytest.fixture()
def admin_headers():
    return {"Authorization": "Bearer " + _token("root", role="admin")}


@pytest.fixture()
def other_user():
    """bob 的 id。**自己建**而不是依赖别的 fixture 顺手建过——否则哪个用例少请求
    一个 fixture，这里就从「用户的 id」变成 TypeError，报错还指不到真正的原因。"""
    if auth.get_user("bob") is None:
        auth.create_user("bob", "pw-for-tests", role="user")
    return int(auth.get_user("bob")["id"])


def _xlsx_bytes(rows):
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    wb.save(buf)
    return buf.getvalue()


def _upload(client, headers, rows=None, name="表.xlsx", data=None):
    payload = data if data is not None else _xlsx_bytes(rows or ROWS)
    return client.post(
        "/api/workflows/upload", headers=headers,
        files={"file": (name, payload, XLSX_MIME)})


def _configure(client, headers, wid, template="查 {{seq}}，项目 {{proj}}",
               columns=None, **extra):
    cols = columns if columns is not None else [
        {"col_index": 0, "header": "编号", "role": "key"},
        {"col_index": 1, "header": "序列", "role": "input", "slot": "seq"},
        {"col_index": 2, "header": "项目", "role": "context", "slot": "proj"},
        {"col_index": 3, "header": "状态", "role": "output", "field": "status"},
    ]
    body = {"workflow_id": wid, "prompt_template": template, "columns": cols}
    body.update(extra)
    r = client.post("/api/workflows", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()["workflow"]


@pytest.fixture()
def uploaded(client, auth_headers):
    """真走一次上传端点，返回解析结果。后面几个 fixture 都建在它上面。"""
    r = _upload(client, auth_headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def one_workflow(client, auth_headers, uploaded):
    """带列映射与模板、status=running 的工作流，外加一行 queued 的行任务。

    行任务的 owner 是**工作流的主人**而不是点开始的人（规格 8 / 裁定 2）：
    放行行的是调度器，不是任何人，所以归属只能跟着工作流走。
    """
    wid = uploaded["workflow"]["id"]
    _configure(client, auth_headers, wid)
    store.update_workflow(wid, status="running", total_rows=2)
    row = store.create_task("第 1 行", "brief",
                            owner_id=store.get_workflow(
                                wid, is_admin=True)["owner_id"],
                            actor="system", workflow_id=wid, workflow_row=0)
    return {"id": wid, "row_id": int(row["id"])}


def is_chinese(detail) -> bool:
    return isinstance(detail, str) and any("一" <= ch <= "鿿" for ch in detail)


# ---------------------------------------------------------------- 上传

def test_upload_returns_preview_and_fixed_source_path(client, auth_headers,
                                                      uploaded):
    """落盘用固定名 source.xlsx；展示名才来自用户给的文件名。"""
    wid = uploaded["workflow"]["id"]
    assert uploaded["headers"] == ["编号", "序列", "项目"]
    assert uploaded["row_count"] == 2
    assert uploaded["preview"][0] == ["1", "AAA", "XBR"]
    wf = store.get_workflow(wid)
    assert wf["name"] == "表.xlsx", "展示名应保留用户给的文件名"
    assert wf["owner_id"] == int(auth.get_user("alice")["id"]), \
        "工作流必须归上传者本人"
    src = Path(wf["source_path"])
    assert src.name == "source.xlsx", "落盘名必须是固定的，不能来自用户输入"
    assert src.parent.name == str(wid)
    assert src.is_file()
    cols = {c["col_index"]: c["role"]
            for c in store.list_workflow_columns(wid)}
    assert cols == {0: "key", 1: "ignore", 2: "ignore"}


def test_upload_rejects_shell(tmp_path, monkeypatch):
    root = Path(tmp_path) / "data" / "workflows"
    monkeypatch.setattr(svc.db, "data_root", lambda: Path(tmp_path) / "data")
    try:
        (root).mkdir(parents=True, exist_ok=True)
        svc.save_upload("evil.sh", b"echo hi", owner_id=1)
    except store.Invalid:
        pass
    else:
        raise AssertionError("写 .sh 进库了")


def test_save_upload_uses_fixed_filename(tmp_path, monkeypatch):
    """路径穿越：filename 是用户可控的，落盘必须用固定名。"""
    root = Path(tmp_path) / "data" / "workflows"
    monkeypatch.setattr(svc.db, "data_root", lambda: Path(tmp_path) / "data")
    # 真的建一个用户：workflows.owner_id 有外键，owner_id=1 在空库里插不进去。
    owner = int(auth.create_user("uploader", "pw-for-tests")["id"])
    final = svc.save_upload("../../evil.xlsx",
                            _xlsx_bytes([["编号", "序列"], ["1", "A"]]),
                            owner_id=owner)
    assert final["workflow"]["name"] == "evil.xlsx"
    src = Path(store.get_workflow(final["workflow"]["id"])["source_path"])
    assert src.name == "source.xlsx"
    assert src.parent.name == str(final["workflow"]["id"])
    assert "evil" not in str(src)
    assert not (Path(tmp_path) / "evil.xlsx").exists()


def test_save_upload_leaves_no_db_row_on_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(svc.db, "data_root", lambda: Path(tmp_path) / "data")
    before = len(store.list_workflows(is_admin=True))
    try:
        svc.save_upload("空.xlsx", b"", owner_id=1)
    except store.Invalid:
        pass
    assert len(store.list_workflows(is_admin=True)) == before


def test_upload_bad_magic_is_422_and_leaves_nothing(client, auth_headers):
    """扩展名对、内容不对：422，库里没记录，staging 也不留半截文件。

    staging 用例带**自证探针**：先自己放一个诱饵文件，并断言 glob 确实只匹配到
    它。上一轮把临时文件名改成带 pid 之后，原本的 ``glob("*.tmp.xlsx")`` 断言
    就恒真了（匹配不到任何东西），却没有任何测试报警——探针就是为了让那种情况
    变成红的。
    """
    before = len(store.list_workflows(is_admin=True))
    staging = db.data_root() / "workflows" / "staging"
    decoy = staging / "decoy.xlsx"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_bytes(b"x")
    assert decoy.is_file()
    assert list(staging.glob("*.xlsx")) == [decoy], \
        "探针失灵：staging 的匹配本身恒真，本用例等于没断言"

    r = _upload(client, auth_headers, name="假.xlsx", data=b"echo hi")
    assert r.status_code == 422 and is_chinese(r.json()["detail"])
    assert len(store.list_workflows(is_admin=True)) == before
    assert [p for p in staging.glob("*.xlsx") if p != decoy] == [], \
        "解析失败在 staging 留下了半截文件"


def test_parse_failure_after_staging_leaves_nothing(client, auth_headers):
    """魔数对、但**解析**失败——这才是 `staging.unlink()` 真正覆盖的那条路径。

    上一版把这条断言挂在「魔数不对」的用例上，可魔数不对是在 ``check_upload``
    里就抛的，staging 压根还没写：那个断言对 unlink 恒真（变异 M17 打上去 0 条红）。
    这里用一个只有表头、没有数据行的真 xlsx——`read_source` 在 staging 写完
    之后才抛「表里没有有效数据行」，正好落在 unlink 存在的意义上。
    """
    before = len(store.list_workflows(is_admin=True))
    staging = db.data_root() / "workflows" / "staging"
    decoy = staging / "decoy2.xlsx"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_bytes(b"x")
    assert list(staging.glob("*.xlsx")) == [decoy], \
        "探针失灵：staging 的匹配本身恒真，本用例等于没断言"

    r = _upload(client, auth_headers, rows=[["编号", "序列"]])
    assert r.status_code == 422 and "数据行" in r.json()["detail"]
    assert len(store.list_workflows(is_admin=True)) == before
    assert [p for p in staging.glob("*.xlsx") if p != decoy] == [], \
        "解析失败在 staging 留下了半截文件"


def test_upload_size_limit(client, auth_headers, monkeypatch):
    """20MB 上限真的钉在常量上；用改小的常量喂它，省得造 20MB 的请求体。"""
    assert wfmod.UPLOAD_MAX == 20 * 1024 * 1024
    monkeypatch.setattr(wfmod, "UPLOAD_MAX", 16)
    r = _upload(client, auth_headers, data=b"PK\x03\x04" + b"0" * 64)
    assert r.status_code == 422 and "上限" in r.json()["detail"]
    assert store.list_workflows(is_admin=True) == []


def test_upload_requires_auth(client):
    assert _upload(client, {}).status_code == 401


# ---------------------------------------------------------------- 配置（创建）

def test_create_rejects_unbound_slot(client, auth_headers, uploaded):
    r = client.post("/api/workflows", headers=auth_headers, json={
        "workflow_id": uploaded["workflow"]["id"],
        "prompt_template": "查 {{seq}} 与 {{proj}}",
        "columns": [{"col_index": 0, "role": "key"},
                    {"col_index": 1, "role": "input", "slot": "seq"}]})
    # store.Invalid → 422（app 的全局约定），不是 brief 里写的 400。
    assert r.status_code == 422, r.text
    assert "proj" in r.json()["detail"]


def test_create_requires_exactly_one_key_column(client, auth_headers, uploaded):
    wid = uploaded["workflow"]["id"]
    r = client.post("/api/workflows", headers=auth_headers, json={
        "workflow_id": wid, "prompt_template": "查 {{seq}}",
        "columns": [{"col_index": 0, "role": "key"},
                    {"col_index": 1, "role": "key"},
                    {"col_index": 2, "role": "input", "slot": "seq"}]})
    assert r.status_code == 422 and "标识列" in r.json()["detail"]


def test_create_rejects_unknown_output_field(client, auth_headers, uploaded):
    wid = uploaded["workflow"]["id"]
    r = client.post("/api/workflows", headers=auth_headers, json={
        "workflow_id": wid, "prompt_template": "查",
        "columns": [{"col_index": 0, "role": "key"},
                    {"col_index": 3, "role": "output", "field": "随便"}]})
    assert r.status_code == 422 and "字段名不认识" in r.json()["detail"]


def test_create_is_owner_scoped(client, auth_headers, other_headers, uploaded):
    """越权 404，且**不因为「别人的工作流」而多说什么**（裁定 1）。"""
    wid = uploaded["workflow"]["id"]
    r = client.post("/api/workflows", headers=other_headers, json={
        "workflow_id": wid, "prompt_template": "偷改 {{seq}}",
        "columns": [{"col_index": 0, "role": "key"},
                    {"col_index": 1, "role": "input", "slot": "seq"}]})
    assert r.status_code == 404
    assert is_chinese(r.json()["detail"])
    # 未绑定的 proj 会让合法用户拿到 422，越权者必须拿不到这个差异
    assert r.status_code != 422
    assert store.get_workflow(wid)["prompt_template"] == svc.PROMPT_PLACEHOLDER


def test_create_replaces_columns_wholesale(client, auth_headers, uploaded):
    """解绑一列再保存，旧列必须消失——否则它继续参与渲染。"""
    wid = uploaded["workflow"]["id"]
    _configure(client, auth_headers, wid)
    assert len(store.list_workflow_columns(wid)) == 4
    _configure(client, auth_headers, wid, template="不变量词",
               columns=[{"col_index": 0, "header": "编号", "role": "key"}])
    left = store.list_workflow_columns(wid)
    assert [c["col_index"] for c in left] == [0], left


def test_create_sets_name_when_given(client, auth_headers, uploaded):
    wid = uploaded["workflow"]["id"]
    _configure(client, auth_headers, wid, name="  第一批抗体  ")
    assert store.get_workflow(wid)["name"] == "第一批抗体"
    # 空名字不该把上传时的展示名抹掉
    _configure(client, auth_headers, wid, name="")
    assert store.get_workflow(wid)["name"] == "第一批抗体"


def test_writeback_switch_is_accepted_and_returned(client, auth_headers, uploaded):
    """回写是**可开关的**（规格 2 决策表 / 7.1 / 9.1）：接口接受、视图返回、
    而且真的落库。

    三处缺任何一处，用户勾上的框就只是个装饰——比没有这个框更危险：
    用户以为自己关掉了回写，实际磁盘上照样整表复制、照样能下载。
    """
    wid = uploaded["workflow"]["id"]
    on = _configure(client, auth_headers, wid, writeback=1)
    assert on["writeback"] == 1, "响应里看不到开关，前端就没法回显用户的选择"
    assert store.get_workflow(wid)["writeback"] == 1, "请求体里的 writeback 没落库"
    detail = client.get(f"/api/workflows/{wid}", headers=auth_headers).json()
    assert detail["workflow"]["writeback"] == 1
    assert client.get("/api/workflows", headers=auth_headers).json()[
        "workflows"][0]["writeback"] == 1

    off = _configure(client, auth_headers, wid, writeback=0)
    assert off["writeback"] == 0
    assert store.get_workflow(wid)["writeback"] == 0, "关掉之后库里还是 1"


# ---------------------------------------------------------------- 列表 / 详情

def test_workflow_list_is_owner_scoped(client, auth_headers, one_workflow,
                                       other_headers):
    r = client.get("/api/workflows", headers=other_headers)
    assert r.json()["workflows"] == []
    ids = [w["id"] for w in
           client.get("/api/workflows", headers=auth_headers).json()["workflows"]]
    assert one_workflow["id"] in ids


def test_workflow_detail_has_columns_skips_rows(client, auth_headers, one_workflow):
    r = client.get(f"/api/workflows/{one_workflow['id']}", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert [c["role"] for c in body["columns"]] == ["key", "input", "context", "output"]
    assert body["skips"] == []
    assert [row["id"] for row in body["rows"]] == [one_workflow["row_id"]]


def test_detail_cross_user_matches_unknown_id(client, auth_headers,
                                              other_headers, one_workflow):
    """越权与「压根不存在」必须**逐字节同答**（裁定 1）。"""
    real = client.get(f"/api/workflows/{one_workflow['id']}", headers=other_headers)
    fake = client.get("/api/workflows/999999", headers=other_headers)
    assert real.status_code == fake.status_code == 404
    assert real.json() == fake.json()
    assert str(one_workflow["id"]).encode("utf-8") not in real.content
    assert "表.xlsx" not in real.text and "抗体筛选" not in real.text


# ---------------------------------------------------------------- 预览

def test_preview_renders_each_row(client, auth_headers, one_workflow):
    wid = one_workflow["id"]
    r = client.get(f"/api/workflows/{wid}/preview?row=1", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["rendered"] == "查 BBB，项目 XBR"
    r0 = client.get(f"/api/workflows/{wid}/preview?row=0", headers=auth_headers)
    assert r0.json()["rendered"] == "查 AAA，项目 XBR"


def test_preview_out_of_range_is_422(client, auth_headers, one_workflow):
    r = client.get(f"/api/workflows/{one_workflow['id']}/preview?row=9",
                   headers=auth_headers)
    assert r.status_code == 422 and is_chinese(r.json()["detail"])


def test_preview_cross_user_is_404(client, other_headers, one_workflow):
    r = client.get(f"/api/workflows/{one_workflow['id']}/preview",
                   headers=other_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------- 控制

def test_control_actions(client, auth_headers, uploaded):
    wid = uploaded["workflow"]["id"]
    _configure(client, auth_headers, wid)
    assert client.post(f"/api/workflows/{wid}/start",
                       headers=auth_headers).json()["workflow"]["status"] == "running"
    assert client.post(f"/api/workflows/{wid}/pause",
                       headers=auth_headers).json()["workflow"]["status"] == "paused"
    assert client.post(f"/api/workflows/{wid}/abort",
                       headers=auth_headers).json()["workflow"]["abort_requested"] == 1
    # resume 必须顺手清 abort_requested，否则调度器那一行永远不放行
    r = client.post(f"/api/workflows/{wid}/resume", headers=auth_headers)
    assert r.json()["workflow"]["status"] == "running"
    assert r.json()["workflow"]["abort_requested"] == 0
    assert store.get_workflow(wid)["abort_requested"] == 0


def test_start_refuses_unconfigured_workflow(client, auth_headers, uploaded):
    """还没配模板就点开始：每一行都会拿同一句占位文本白烧一次计算。"""
    wid = uploaded["workflow"]["id"]
    r = client.post(f"/api/workflows/{wid}/start", headers=auth_headers)
    assert r.status_code == 422 and "模板" in r.json()["detail"]
    assert store.get_workflow(wid)["status"] == "draft"


def test_abort_terminates_live_rows(client, auth_headers, one_workflow):
    row = one_workflow["row_id"]
    store.update_task(row, status="running")
    r = client.post(f"/api/workflows/{one_workflow['id']}/abort",
                    headers=auth_headers)
    assert r.status_code == 200, r.text
    assert store.get_task(row)["status"] == "failed"
    assert "中止" in (store.get_task(row)["error"] or "")
    evs = [e for e in store.list_events(row) if e["kind"] == "status"]
    assert evs and evs[-1]["actor"] == "human", "中止是用户点的，actor 必须是 human"


def test_unknown_action_changes_nothing(client, auth_headers, one_workflow):
    """裁定 5：非法 action 不许落进任何一条控制流。

    写不成 `if False` 就没事——白名单被删掉时 action 会掉进 else 分支执行
    **中止**，而中止除了改 status 还会把在跑的行杀掉。断言 abort_requested
    与行的状态，正是为了钉住这条「静默生效」的路径。
    """
    wid, row = one_workflow["id"], one_workflow["row_id"]
    for bad in ("restart", "delete", "../admin/panic", "", "START"):
        r = client.post(f"/api/workflows/{wid}/{bad}", headers=auth_headers)
        # 405 也算「没进去」：空 action 在路径上就拼不出这条路由（尾斜杠）。
        assert r.status_code in (404, 405, 422), (bad, r.status_code)
    wf = store.get_workflow(wid)
    assert wf["status"] == "running" and wf["abort_requested"] == 0
    assert store.get_task(row)["status"] == "queued"


def test_control_cross_user_is_404(client, other_headers, one_workflow):
    """裁定 1 + 2：别人的工作流一律 404，且什么都不能改。"""
    wid, row = one_workflow["id"], one_workflow["row_id"]
    for act in ("start", "pause", "resume", "abort"):
        r = client.post(f"/api/workflows/{wid}/{act}", headers=other_headers)
        assert r.status_code == 404, act
    r = client.post(f"/api/workflows/{wid}/rows/0/skip", headers=other_headers,
                    json={"reason": "我以为是别人的"})
    assert r.status_code == 404
    assert store.get_workflow(wid)["status"] == "running"
    assert store.get_workflow(wid)["abort_requested"] == 0
    assert store.get_task(row)["status"] == "queued"
    assert store.list_workflow_skips(wid) == set()


def test_admin_can_read_but_not_control(client, auth_headers, admin_headers,
                                        one_workflow):
    """§9.3：值班台**只读**。看得见是因为列表传了 is_admin=True；控制权不放开是
    刻意取舍，不然一次误点就停了别人几十分钟 GPU 的批次。"""
    wid = one_workflow["id"]
    ids = [w["id"] for w in
           client.get("/api/workflows", headers=admin_headers).json()["workflows"]]
    assert wid in ids
    assert client.get(f"/api/workflows/{wid}",
                      headers=admin_headers).status_code == 200
    for act in ("start", "pause", "resume", "abort"):
        r = client.post(f"/api/workflows/{wid}/{act}", headers=admin_headers)
        assert r.status_code == 404, f"管理员竟然能 {act} 别人的工作流"
    r = client.post(f"/api/workflows/{wid}/rows/0/skip", headers=admin_headers,
                    json={"reason": "x"})
    assert r.status_code == 404
    assert store.get_workflow(wid)["status"] == "running"
    assert store.list_workflow_skips(wid) == set()


# ---------------------------------------------------------------- 跳过

def _card(client, headers, wid):
    """列表端点给这张卡片的那一份视图（failed_rows 只在这里挂）。"""
    r = client.get("/api/workflows", headers=headers)
    assert r.status_code == 200, r.text
    for w in r.json()["workflows"]:
        if int(w["id"]) == int(wid):
            return w
    raise AssertionError(f"列表里没有 {wid}")


def test_failed_rows_counts_the_rows_that_really_failed(client, auth_headers,
                                                         one_workflow):
    """Minor-1：``failed_rows`` 是**算出来的数**，不是恒 0，也不是「源码里出现过 failed」。

    上一轮这条只有源码文本围栏：把 ``out[wid]["failed_rows"] = int(r[2])`` 改成
    ``= 0``（保留 ``if r["status"] == "failed"`` 字面量），143 条全绿——卡片于是对
    任何工作流恒显「没有失败行」，对着没发生过的故障报平安，而规格 7.4 把它列为
    最显眼处。所以这里断**返回值**：一行真失败 + 一行已完成，数出来必须是 1。
    """
    wid, row = one_workflow["id"], one_workflow["row_id"]
    store.update_task(row, status="failed", error="崩了")
    ok = store.create_task("第 2 行", "b", owner_id=int(
        store.get_workflow(wid, is_admin=True)["owner_id"]),
        actor="system", workflow_id=wid, workflow_row=1)
    store.update_task(ok["id"], status="done", summary="算完了")

    assert _card(client, auth_headers, wid)["failed_rows"] == 1, \
        "真失败的那一行没被数进 failed_rows——卡片恒显「没有失败行」"

    store.update_task(row, status="done")          # 故障处理完了，计数要跟着回落
    assert _card(client, auth_headers, wid)["failed_rows"] == 0


def test_skipped_rows_are_not_reported_as_failed(client, auth_headers, one_workflow):
    """Major-1：用户跳过的行，**卡片上不得表现为失败**。

    跳过的行在 tasks 里 status 是 failed（补偿任务建出来就是终态），
    而同一行在结果表里写的是「已跳过」。不排除的话：跳 1 行 → 表里「已跳过」，
    卡片标红「失败 1 行」，「只看失败行」也把它列出来——两件事的处理方式正好相反
    （前者不用管，后者要查），用户分不清。

    混合断言是必要的：只测「全跳过时是 0」，把整个统计改成恒 0 也能过。
    """
    wid, row = one_workflow["id"], one_workflow["row_id"]
    store.update_task(row, status="failed", error="用户跳过，未运行")
    store.skip_workflow_row(wid, 0, "序列坏了")
    # 夹具自证：这一行确实长得跟真失败一模一样（这正是必须靠 workflow_skips 区分的原因）
    assert store.get_task(row)["status"] == "failed"
    assert _card(client, auth_headers, wid)["failed_rows"] == 0, \
        "跳过的行被算成失败行了：卡片说「失败」，结果表说「已跳过」"

    # 同一个工作流里再来一行**真**失败：它必须照常被数进去。
    real = store.create_task("第 2 行", "b", owner_id=int(
        store.get_workflow(wid, is_admin=True)["owner_id"]),
        actor="system", workflow_id=wid, workflow_row=1)
    store.update_task(real["id"], status="failed", error="真的崩了")
    assert _card(client, auth_headers, wid)["failed_rows"] == 1, \
        "真失败的行也被一起排除了——那是对着故障报平安"

    # 跳过一整行之后，行状态仍然是那条补偿任务的 failed（数据模型没改，
    # 改 CHECK 是表重建级别的迁移，见 fix2-report）；界面那一侧由
    # workflow_skips 判，两边说的是同一件事。
    detail = client.get(f"/api/workflows/{wid}", headers=auth_headers).json()
    assert detail["skips"] == [0]
    assert [r["status"] for r in detail["rows"]] == ["failed", "failed"]


def test_skipped_rows_do_not_hide_the_current_row(client, auth_headers,
                                                  one_workflow):
    """排除跳过行不许顺手把 current_row 也算没**：跳过是终态，不影响当前行。"""
    wid, row = one_workflow["id"], one_workflow["row_id"]
    store.update_task(row, status="failed", error="用户跳过，未运行")
    store.skip_workflow_row(wid, 0, "序列坏了")
    live = store.create_task("第 2 行", "b", owner_id=int(
        store.get_workflow(wid, is_admin=True)["owner_id"]),
        actor="system", workflow_id=wid, workflow_row=1)
    store.update_task(live["id"], status="running")

    card = _card(client, auth_headers, wid)
    assert card["current_row"] == 1, "在跑的那一行不见了：卡片会以为整批没在动"
    assert card["current_status"] == "running"


def test_skip_terminates_row_and_records_reason(client, auth_headers, one_workflow):
    wid, row = one_workflow["id"], one_workflow["row_id"]
    r = client.post(f"/api/workflows/{wid}/rows/1/skip", headers=auth_headers,
                    json={"reason": "序列不对"})
    assert r.status_code == 200, r.text
    assert r.json()["skips"] == [1]
    assert store.list_workflow_skips(wid) == {1}
    # 这一行还没有任务（还没放行）也不能报错——跳过先于放行是常态
    assert store.list_workflow_rows(wid, 1) == []

    store.update_task(row, status="running")
    client.post(f"/api/workflows/{wid}/rows/0/skip", headers=auth_headers,
                json={"reason": ""})
    assert store.get_task(row)["status"] == "failed", \
        "跳过后不置终态，闸门② 会一直等这一行，整批卡住"
    msgs = [e["message"] for e in store.list_events(row) if e["kind"] == "status"]
    assert any("用户跳过" in m for m in msgs)


def test_skip_ignores_workflow_id_in_body(client, auth_headers, other_headers,
                                          one_workflow):
    """裁定 4：请求体里的 workflow_id 绝不能被转手写进行任务。

    `update_task(row, workflow_id=别人的)` 能把一行搬进别人的工作流——之后那一行
    归 B 的闸门管、A 的批次永远等不到它，整批卡住，且没有任何报错。
    """
    wid, row = one_workflow["id"], one_workflow["row_id"]
    other = _upload(client, other_headers).json()["workflow"]["id"]
    r = client.post(f"/api/workflows/{wid}/rows/0/skip", headers=auth_headers,
                    json={"reason": "x", "workflow_id": other})
    assert r.status_code == 200, r.text
    assert store.get_task(row)["workflow_id"] == wid, \
        "请求体里的 workflow_id 透传进了行任务"
    assert store.list_workflow_skips(other) == set()


def test_control_ignores_workflow_id_in_body(client, auth_headers, other_headers,
                                             one_workflow):
    wid, row = one_workflow["id"], one_workflow["row_id"]
    other = _upload(client, other_headers).json()["workflow"]["id"]
    client.post(f"/api/workflows/{wid}/pause", headers=auth_headers,
                json={"workflow_id": other})
    # 控制端点根本没有请求体模型：body 里的 workflow_id 无人读取，
    # 暂停落在 URL 指明的那个工作流上，别人的那个一动不动。
    assert store.get_workflow(wid)["status"] == "paused"
    assert store.get_workflow(other)["status"] == "draft"
    assert store.list_workflow_rows(other) == []
    assert store.get_task(row)["workflow_id"] == wid
    assert store.get_task(row)["status"] == "queued"


# ---------------------------------------------------------------- 结果表下载

def _make_result(wf_id: int) -> Path:
    """造一份真 xlsx 并写回 result_path——**放在调度器真正会放的位置**。"""
    src = Path(store.get_workflow(wf_id)["source_path"])
    path = src.parent / "result.xlsx"
    path.write_bytes(_xlsx_bytes([["编号", "状态"], ["1", "done"]]))
    store.update_workflow(wf_id, result_path=str(path))
    return path


def test_result_xlsx_downloads_when_generated(client, auth_headers, one_workflow):
    path = _make_result(one_workflow["id"])
    r = client.get(f"/api/workflows/{one_workflow['id']}/result.xlsx",
                   headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"PK")
    assert path.name in r.headers["content-disposition"]


def test_result_xlsx_cross_user_is_404(client, other_headers, one_workflow):
    _make_result(one_workflow["id"])          # 文件确实存在，排除「碰巧没有文件」
    r = client.get(f"/api/workflows/{one_workflow['id']}/result.xlsx",
                   headers=other_headers)
    assert r.status_code == 404
    assert b"PK" not in r.content, "响应体里不该有任何 xlsx 字节"
    fake = client.get("/api/workflows/999999/result.xlsx", headers=other_headers)
    assert r.json() == fake.json()


def test_result_xlsx_admin_may_read_others(client, auth_headers, admin_headers,
                                           one_workflow):
    """裁定 2：管理员对别人的工作流只读——结果表属于「可读」那一档。"""
    _make_result(one_workflow["id"])
    r = client.get(f"/api/workflows/{one_workflow['id']}/result.xlsx",
                   headers=admin_headers)
    assert r.status_code == 200 and r.content.startswith(b"PK")


def test_result_xlsx_without_output_cols_is_404(client, auth_headers, uploaded):
    """没配结果列 → 调度器压根不生成 result.xlsx，这里要说清为什么没有。"""
    wid = uploaded["workflow"]["id"]
    _configure(client, auth_headers, wid, template="查 {{seq}}", columns=[
        {"col_index": 0, "role": "key"},
        {"col_index": 1, "role": "input", "slot": "seq"}])
    _make_result(wid)                           # 文件在，但没理由给它
    r = client.get(f"/api/workflows/{wid}/result.xlsx", headers=auth_headers)
    assert r.status_code == 404 and "结果列" in r.json()["detail"]


def test_result_xlsx_not_generated_yet_is_404(client, auth_headers, one_workflow):
    """`Path("")` 是 `.`，`.exists()` 对它为真——用 exists() 判就会把一个目录
    交给 FileResponse，用户看到的是 500 而不是「还没生成」。"""
    assert store.get_workflow(one_workflow["id"])["result_path"] == ""
    r = client.get(f"/api/workflows/{one_workflow['id']}/result.xlsx",
                   headers=auth_headers)
    assert r.status_code == 404 and is_chinese(r.json()["detail"])


def test_result_xlsx_is_404_when_writeback_is_off(client, auth_headers,
                                                  one_workflow):
    """规格 9.1「开关打开时才有」：关掉回写就不该有可下的结果表。

    即使磁盘上**碰巧**有一份（比如用户先跑了几行才关掉），这个端点也必须
    404 ——否则「关掉回写」就只是不更新、既有的整表副本照样能被下载，
    用户要的根本不是这个。
    """
    wid = one_workflow["id"]
    _make_result(wid)                                  # 文件确实在
    store.update_workflow(wid, writeback=0)
    r = client.get(f"/api/workflows/{wid}/result.xlsx", headers=auth_headers)
    assert r.status_code == 404 and is_chinese(r.json()["detail"])
    assert b"PK" not in r.content


def test_result_xlsx_downloads_when_writeback_is_on(client, auth_headers,
                                                    one_workflow):
    """配套围栏：开关打开时该下的就要下得掉。

    少了这条，上面那条「关掉就 404」可以靠「永远 404」蒙混过关——
    而用户已经跑完的批次就此永远拿不到结果表。
    """
    wid = one_workflow["id"]
    _make_result(wid)
    assert store.get_workflow(wid)["writeback"] == 1
    r = client.get(f"/api/workflows/{wid}/result.xlsx", headers=auth_headers)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 删除

def test_delete_removes_workflow_and_cascades(client, auth_headers, one_workflow):
    wid, row = one_workflow["id"], one_workflow["row_id"]
    src = Path(store.get_workflow(wid)["source_path"])
    assert src.is_file(), "夹具本身没把源表写出来"
    r = client.delete(f"/api/workflows/{wid}", headers=auth_headers)
    assert r.status_code == 200 and r.json() == {"ok": True}
    with pytest.raises(store.NotFound):
        store.get_workflow(wid)
    with pytest.raises(store.NotFound):
        store.get_task(row)
    # **盘上的源表与结果表也一起清掉。**
    #
    # 只删数据库的话它们会**永远**留在 data/workflows/<id>/：用户以为删干净了，
    # 实际上那些表还在（上一批的结果也在里面），而 janitor 也不扫这一块——
    # 所以这条删除路径不自己清，就再没有人清。
    assert not src.exists(), "工作流删了，源表还留在盘上"
    assert not Path(db.data_root() / "workflows" / str(wid)).exists(), \
        "工作流删了，它的目录还留在盘上"


def test_delete_workflow_keeps_other_workflows_files(client, auth_headers, one_workflow):
    """**只删自己那一份。** 别人的源表一个字都不许动。"""
    wid = one_workflow["id"]
    other = _upload(client, auth_headers, name="别人的表.xlsx")
    other_wid = other.json()["workflow"]["id"]
    other_src = Path(store.get_workflow(other_wid)["source_path"])
    assert other_src.is_file(), "夹具本身没把源表写出来"

    assert client.delete(f"/api/workflows/{wid}",
                         headers=auth_headers).status_code == 200
    assert other_src.exists(), "删一个工作流把别人的源表也删了"


def test_delete_aborts_live_row_before_dropping_record(client, auth_headers,
                                                       one_workflow, monkeypatch):
    """规格 6.7：先写 failed、留出投递时间、再删记录。

    顺序反了（先删后中止）的话，级联删掉 tasks 行后 `_db_status` 返回 ""、
    `_abort_signal` 因 "" not in TERMINAL_STATUSES 直接返回 None，调度器不会
    去杀它——留下一个还在烧 GPU 的僵尸，而库里什么记录都没有。
    """
    wid, row = one_workflow["id"], one_workflow["row_id"]
    store.update_task(row, status="running")
    seen = {}
    real_delete = store.delete_workflow

    def spy(workflow_id, *a, **k):
        seen["statuses"] = [r["status"] for r in
                            store.list_workflow_rows(workflow_id)]
        return real_delete(workflow_id, *a, **k)

    monkeypatch.setattr(store, "delete_workflow", spy)
    assert client.delete(f"/api/workflows/{wid}",
                         headers=auth_headers).status_code == 200
    assert seen["statuses"] == ["failed"], \
        "删记录那一刻行任务还没被写成 failed：中止信号发不出去"


def test_delete_is_owner_scoped(client, auth_headers, other_headers, one_workflow):
    """裁定 3：归属校验必须在 `_abort_live_rows` 之前。

    顺序反了的话，越权删除会先把 B 在跑的计算杀掉、再返回 404——凭空造出一条
    「B 的工作流刚被中止过」的时间线，而 B 什么都没点。所以这里钉的是
    **行任务仍是 queued**，而不只是「工作流还在」。
    """
    wid, row = one_workflow["id"], one_workflow["row_id"]
    r = client.delete(f"/api/workflows/{wid}", headers=other_headers)
    assert r.status_code == 404 and is_chinese(r.json()["detail"])
    assert store.get_workflow(wid)["status"] == "running"
    assert store.get_workflow(wid)["abort_requested"] == 0
    assert store.get_task(row)["status"] == "queued", \
        "越权删除把别人的行任务中止了"
    assert store.get_task(row)["error"] in (None, "")


def test_delete_admin_cannot_delete_others(client, admin_headers, one_workflow):
    r = client.delete(f"/api/workflows/{one_workflow['id']}", headers=admin_headers)
    assert r.status_code == 404
    assert store.get_task(one_workflow["row_id"])["status"] == "queued"


def test_delete_missing_workflow_is_404(client, auth_headers):
    assert client.delete("/api/workflows/999999",
                         headers=auth_headers).status_code == 404


# ---------------------------------------------------------------- scope + 行任务删除守卫

def test_delete_workflow_row_task_returns_409(client, auth_headers, one_workflow):
    """不挡就静默丢结果 + 留僵尸进程（规格 6.5 洞三）。"""
    row = one_workflow["row_id"]
    r = client.delete(f"/api/tasks/{row}", headers=auth_headers)
    assert r.status_code == 409
    assert "跳过" in r.json()["detail"] or "工作流" in r.json()["detail"]
    assert store.get_task(row) is not None, "守卫不该删数据"


def test_regular_task_delete_still_works(client, auth_headers):
    """守卫写反（`is None`）的话这一条就红——常规任务必须照常删。"""
    row = store.create_task("常规", "b", owner_id=auth.get_user("alice")["id"])
    r = client.delete(f"/api/tasks/{int(row['id'])}", headers=auth_headers)
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_scope_regular_excludes_workflow_rows(client, auth_headers, one_workflow):
    r = client.get("/api/tasks?scope=regular", headers=auth_headers)
    assert one_workflow["row_id"] not in {t["id"] for t in r.json()["tasks"]}
    r = client.get("/api/tasks?scope=workflow", headers=auth_headers)
    assert one_workflow["row_id"] in {t["id"] for t in r.json()["tasks"]}


def test_scope_default_is_regular(client, auth_headers, one_workflow):
    """默认 regular（规格 8.1）：不传 scope 时行任务不该混进常规列表。"""
    r = client.get("/api/tasks", headers=auth_headers)
    assert one_workflow["row_id"] not in {t["id"] for t in r.json()["tasks"]}


def test_scope_all_is_not_cross_user(client, auth_headers, one_workflow, other_user):
    store.create_task("别人的常规任务", "b", owner_id=other_user)
    r = client.get("/api/tasks?scope=all", headers=auth_headers)
    assert all(t["owner_id"] != other_user for t in r.json()["tasks"])


def test_scope_invalid_is_422(client, auth_headers, one_workflow):
    """裁定 6：scope 是用户可控的查询维度，白名单之外的值不许落进「不过滤」。"""
    r = client.get("/api/tasks?scope=%27%20or%201=1", headers=auth_headers)
    assert r.status_code == 422 and is_chinese(r.json()["detail"])


def test_row_task_owner_is_workflow_owner(client, auth_headers, one_workflow):
    """裁定 8/2：行任务归工作流的主人，不是点开始的那个人。

    放行行的是调度器（actor=system），不是任何人——所以归属只能跟着工作流走。
    这里钉住 HTTP 层碰它不动：控制一轮之后 owner 仍然是工作流的主人。
    """
    wf = store.get_workflow(one_workflow["id"], is_admin=True)
    assert store.get_task(one_workflow["row_id"])["owner_id"] == wf["owner_id"]
    client.post(f"/api/workflows/{one_workflow['id']}/start", headers=auth_headers)
    assert store.get_task(one_workflow["row_id"])["owner_id"] == wf["owner_id"]


# ---------------------------------------------------------------- 静态资源

def test_workflow_ui_files_are_allowlisted(client):
    """值班台/工作流页是程序的一部分，走同一个单文件白名单（不是整树挂载）。"""
    assert {"workflow.html", "workflow.js"} <= set(svc.USER_UI_FILES)
    r = client.get("/secret.txt")
    assert r.status_code == 404 and is_chinese(r.json()["detail"])


def test_preview_admin_may_read_others(client, admin_headers, other_headers,
                                      one_workflow):
    """R-2：§9.3 的「只读」不等于「看不见」。

    预览是纯读操作，不泄露任何写能力，所以跟列表/详情/结果表同属值班台可见项；
    但**别的普通用户**仍然是 404——只读开给的是管理员，不是所有人。
    """
    wid = one_workflow["id"]
    r = client.get(f"/api/workflows/{wid}/preview?row=1", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["rendered"] == "查 BBB，项目 XBR"
    assert client.get(f"/api/workflows/{wid}/preview",
                      headers=other_headers).status_code == 404
