"""工作流的「共享」与「转让」。

两件事，最容易做漏的各有专门用例：

- **共享是「加一个协作者」**，不是「换主人」。共享者能看、能跑、能暂停、
  能跳过行、能重跑行、**能改配置**；但**不能删除**、**不能再分享**、
  **不能转让**。越权一律 404（不承认这个 id 存在）。
- **转让是「换主人」**：旧主人**立刻失去访问**，已有的行任务、产物、结果
  全部转到新主人名下，共享关系一并清空。

裁定（2026-10-23）：
1. 共享者**可以改配置**（用户明确要「允许配置权限」）；
2. 转让后**旧主人立即失去访问**，已有行任务/产物/结果**全部转到新主人名下**；
3. 管理员对别人的工作流**仍然是只读的**（既有设计），但「分享/转让」这一件
   事上**代替 owner 操作**——值班台要能处理归属问题。

围栏清单（每条都能真的抓到回归）：

- ``test_shared_workflow_appears_in_others_list``  共享后出现在对方列表里
- ``test_shared_user_can_read_and_reconfigure``    共享者能看能改配置
- ``test_shared_user_cannot_delete``               共享者不能删除（404）
- ``test_shared_user_cannot_share_or_transfer``    共享者不能再分享/转让（404）
- ``test_stranger_cannot_see_or_share``            无关的人一律 404
- ``test_owner_can_remove_share_and_access_is_revoked``  取消共享即刻收回访问
- ``test_transfer_moves_owner_rows_and_revokes_old_owner`` 转让三件事一起发生
- ``test_transfer_clears_share_rows``              转让清空共享关系
- ``test_cannot_share_with_self_or_unknown_user``  422
- ``test_admin_can_share_on_behalf_of_owner``      管理员代劳
- ``test_admin_still_cannot_control_others_workflow``  管理员只读的老规矩没破
- ``test_user_search_returns_only_public_fields``  搜账户不泄露密码哈希
- ``test_admin_panel_lists_workflows_and_shares``  管理端能列、能共享
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

REPO = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO / "server"
ADMIN_DIR = REPO / "admin"
for _d in (SERVER_DIR, ADMIN_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import app as svc              # noqa: E402
import app_admin as panel      # noqa: E402
import auth                    # noqa: E402
import store                   # noqa: E402

XLSX_MIME = ("application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet")
ROWS = [["编号", "序列"], ["1", "AAA"], ["2", "BBB"]]


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def client():
    """用户面板（8473 那个 app）。不进 `with`——create_public_app() 没有 lifespan。"""
    return TestClient(svc.create_public_app())


@pytest.fixture()
def panel_client():
    """值班台（8474 那个 app）。"""
    return TestClient(panel.create_admin_app())


def _token(username, role="user"):
    """真实发令牌，不用 dev-login 旁路——这里要验的恰恰是归属校验。"""
    if auth.get_user(username) is None:
        auth.create_user(username, "pw-for-tests", role=role)
    row = auth.get_user(username)
    return auth.issue_token(int(row["id"]))


@pytest.fixture()
def alice():
    return _token("alice")


@pytest.fixture()
def bob():
    return _token("bob")


@pytest.fixture()
def root():
    return _token("root", role="admin")


@pytest.fixture()
def carol():
    return _token("carol")


def _headers(token):
    return {"Authorization": "Bearer " + token}


def _xlsx_bytes(rows):
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    wb.save(buf)
    return buf.getvalue()


def _make_workflow(client, token, name="表.xlsx"):
    """走真实的上传 + 配置端点，建一个带行任务的工作流。"""
    h = _headers(token)
    r = client.post("/api/workflows/upload", headers=h,
                    files={"file": (name, _xlsx_bytes(ROWS), XLSX_MIME)})
    assert r.status_code == 200, r.text
    wid = r.json()["workflow"]["id"]
    r = client.post("/api/workflows", headers=h, json={
        "workflow_id": wid,
        "prompt_template": "查 {{seq}}",
        "columns": [{"col_index": 0, "header": "编号", "role": "key"},
                    {"col_index": 1, "header": "序列", "role": "input",
                     "slot": "seq"}],
    })
    assert r.status_code == 200, r.text
    store.update_workflow(wid, status="running", total_rows=2)
    row = store.create_task("第 1 行", "brief",
                            owner_id=store.get_workflow(
                                wid, is_admin=True)["owner_id"],
                            actor="system", workflow_id=wid, workflow_row=0)
    return {"id": wid, "row_id": int(row["id"])}


def _uid(username):
    return int(auth.get_user(username)["id"])


# ---------------------------------------------------------------- 共享


def test_shared_workflow_appears_in_others_list(client, alice, bob):
    """共享之后，它出现在对方的列表里，并且**标明是共享给我的**。

    不标明的话，共享者会在自己的列表里看到一个自己不能删的东西。
    """
    wf = _make_workflow(client, alice)
    r = client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                    json={"user_id": _uid("bob")})
    assert r.status_code == 200, r.text

    r = client.get("/api/workflows", headers=_headers(bob))
    assert r.status_code == 200, r.text
    got = [w for w in r.json()["workflows"] if w["id"] == wf["id"]]
    assert len(got) == 1, "共享的工作流必须出现在对方列表里"
    assert got[0]["shared_with_me"] is True
    assert got[0]["is_owner"] is False
    assert got[0]["owner_username"] == "alice"
    assert got[0]["share_count"] == 1


def test_shared_user_can_read_and_reconfigure(client, alice, bob):
    """共享者能看详情，**也能改配置**（用户明确要「允许配置权限」）。"""
    wf = _make_workflow(client, alice)
    client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                json={"user_id": _uid("bob")})

    r = client.get(f"/api/workflows/{wf['id']}", headers=_headers(bob))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow"]["id"] == wf["id"]
    # 共享名单也要给：共享者要知道还有谁一起用。
    assert [s["username"] for s in body["shares"]] == ["bob"]

    r = client.post("/api/workflows", headers=_headers(bob), json={
        "workflow_id": wf["id"],
        "prompt_template": "改过了 {{seq}}",
        "columns": [{"col_index": 0, "header": "编号", "role": "key"},
                    {"col_index": 1, "header": "序列", "role": "input",
                     "slot": "seq"}],
    })
    assert r.status_code == 200, r.text
    assert store.get_workflow(wf["id"], is_admin=True)["prompt_template"] \
        == "改过了 {{seq}}"


def test_shared_user_can_retry_a_row(client, alice, bob):
    """共享者能**重跑那一行**。

    行任务的归属记的是**工作流的主人**（规格 8 裁定 2），所以共享者在
    ``tasks.owner_id`` 上永远对不上——按 owner 校验的话，他连自己共享来的
    那一行都动不了，「共享者能干活」就只剩一张看得见的表。
    """
    wf = _make_workflow(client, alice)
    client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                json={"user_id": _uid("bob")})
    store.set_status(wf["row_id"], "failed", actor="system", message="炸了")

    r = client.post(f"/api/tasks/{wf['row_id']}/retry", headers=_headers(bob))
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "queued"


def test_shared_user_cannot_delete(client, alice, bob):
    """共享者**不能删除**。越权一律 404。"""
    wf = _make_workflow(client, alice)
    client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                json={"user_id": _uid("bob")})
    r = client.delete(f"/api/workflows/{wf['id']}", headers=_headers(bob))
    assert r.status_code == 404, r.text
    assert store.get_workflow(wf["id"], is_admin=True) is not None, \
        "工作流不该被删掉"


def test_shared_user_cannot_share_or_transfer(client, alice, bob, carol):
    """共享者**不能再分享、不能转让**。他是协作者，不是主人。"""
    wf = _make_workflow(client, alice)
    client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                json={"user_id": _uid("bob")})

    r = client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(bob),
                    json={"user_id": _uid("carol")})
    assert r.status_code == 404, r.text

    r = client.post(f"/api/workflows/{wf['id']}/transfer", headers=_headers(bob),
                    json={"user_id": _uid("carol")})
    assert r.status_code == 404, r.text

    assert store.get_workflow(wf["id"], is_admin=True)["owner_id"] == _uid("alice"), \
        "归属不能被共享者改掉"


def test_stranger_cannot_see_or_share(client, alice, carol, bob):
    """完全无关的人：看详情 404、共享 404、列名单 404。

    响应体还不能包含可区分信息——「工作流不存在」和「接口不存在」都不是泄露，
    但「你无权」就是泄露。
    """
    wf = _make_workflow(client, alice)
    for r in (client.get(f"/api/workflows/{wf['id']}", headers=_headers(carol)),
              client.post(f"/api/workflows/{wf['id']}/shares",
                          headers=_headers(carol),
                          json={"user_id": _uid("bob")}),
              client.get(f"/api/workflows/{wf['id']}/shares",
                         headers=_headers(carol))):
        assert r.status_code == 404, r.text
        assert "无权" not in r.text


def test_owner_can_remove_share_and_access_is_revoked(client, alice, bob):
    """取消共享即刻收回访问——不是「等下次刷新才生效」。"""
    wf = _make_workflow(client, alice)
    client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                json={"user_id": _uid("bob")})
    assert client.get(f"/api/workflows/{wf['id']}",
                       headers=_headers(bob)).status_code == 200

    r = client.delete(f"/api/workflows/{wf['id']}/shares/{_uid('bob')}",
                      headers=_headers(alice))
    assert r.status_code == 200, r.text
    assert r.json()["shares"] == []

    assert client.get(f"/api/workflows/{wf['id']}",
                      headers=_headers(bob)).status_code == 404
    r = client.get("/api/workflows", headers=_headers(bob))
    assert [w for w in r.json()["workflows"] if w["id"] == wf["id"]] == []


# ---------------------------------------------------------------- 转让


def test_transfer_moves_owner_rows_and_revokes_old_owner(client, alice, bob):
    """转让三件事**必须同时发生**：换主人、旧主人失去访问、行任务跟着走。"""
    wf = _make_workflow(client, alice)
    client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                json={"user_id": _uid("bob")})

    r = client.post(f"/api/workflows/{wf['id']}/transfer",
                    headers=_headers(alice), json={"user_id": _uid("bob")})
    assert r.status_code == 200, r.text
    assert r.json()["workflow"]["owner_id"] == _uid("bob")

    # 旧主人立刻失去访问
    assert client.get(f"/api/workflows/{wf['id']}",
                      headers=_headers(alice)).status_code == 404
    r = client.get("/api/workflows", headers=_headers(alice))
    assert [w for w in r.json()["workflows"] if w["id"] == wf["id"]] == []

    # 新主人拿到了，且他是 owner
    r = client.get(f"/api/workflows/{wf['id']}", headers=_headers(bob))
    assert r.status_code == 200, r.text
    assert r.json()["workflow"]["is_owner"] is True

    # 行任务跟着转到新主人名下
    row = store.get_task(wf["row_id"], is_admin=True)
    assert row["owner_id"] == _uid("bob"), "行任务必须跟着工作流换主人"


def test_transfer_clears_share_rows(client, alice, bob, carol):
    """转让清空共享关系：旧主人的那些协作者不再有访问权。"""
    wf = _make_workflow(client, alice)
    client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                json={"user_id": _uid("carol")})
    r = client.post(f"/api/workflows/{wf['id']}/transfer",
                    headers=_headers(alice), json={"user_id": _uid("bob")})
    assert r.status_code == 200, r.text

    assert store.list_workflow_shares(wf["id"]) == []
    assert client.get(f"/api/workflows/{wf['id']}",
                      headers=_headers(carol)).status_code == 404


def test_cannot_share_with_self_or_unknown_user(client, alice):
    """共享给自己没有意义；账号不存在要 422 而不是 500。"""
    wf = _make_workflow(client, alice)
    r = client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                    json={"user_id": _uid("alice")})
    assert r.status_code == 422, r.text

    r = client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(alice),
                    json={"user_id": 999999})
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- 管理员


def test_admin_can_share_on_behalf_of_owner(client, root, bob):
    """管理员在「分享」这件事上**代替 owner 操作**。

    他对别人的工作流本来是只读的（值班台要看得见），但归属问题要有人能处理。
    """
    auth.create_user("dave", "pw-for-tests", role="user")
    dave = _token("dave")
    wf = _make_workflow(client, dave)

    r = client.post(f"/api/workflows/{wf['id']}/shares", headers=_headers(root),
                    json={"user_id": _uid("bob")})
    assert r.status_code == 200, r.text
    assert [s["username"] for s in r.json()["shares"]] == ["bob"]


def test_admin_still_cannot_control_others_workflow(client, root):
    """共享功能**不能顺手把管理员的只读改掉**。这条老规矩继续有效。"""
    auth.create_user("erin", "pw-for-tests", role="user")
    erin = _token("erin")
    wf = _make_workflow(client, erin)

    # 只读端点：看得见
    assert client.get(f"/api/workflows/{wf['id']}",
                      headers=_headers(root)).status_code == 200
    # 写端点：动不了
    r = client.post(f"/api/workflows/{wf['id']}/pause", headers=_headers(root))
    assert r.status_code == 404, r.text
    r = client.delete(f"/api/workflows/{wf['id']}", headers=_headers(root))
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------- 搜账户


def test_user_search_returns_only_public_fields(client, alice, bob):
    """搜账户只给 id / 用户名 / 角色——**绝不能给密码哈希**。"""
    r = client.get("/api/users", headers=_headers(alice))
    assert r.status_code == 200, r.text
    users = r.json()["users"]
    assert {u["username"] for u in users} >= {"alice", "bob"}
    for u in users:
        assert set(u.keys()) <= {"id", "username", "role"}, u
        assert "password" not in u and "password_hash" not in u


def test_user_search_filters_by_name(client, alice, bob):
    r = client.get("/api/users?q=bob", headers=_headers(alice))
    assert r.status_code == 200, r.text
    assert [u["username"] for u in r.json()["users"]] == ["bob"]


# ---------------------------------------------------------------- 管理端


def test_admin_panel_lists_workflows_and_shares(panel_client, alice, bob, root):
    """值班台要能列工作流并代替 owner 做共享。"""
    wf = _make_workflow(TestClient(svc.create_public_app()), alice)

    r = panel_client.get("/api/admin/workflows", headers=_headers(root))
    assert r.status_code == 200, r.text
    got = [w for w in r.json()["workflows"] if w["id"] == wf["id"]]
    assert len(got) == 1
    assert got[0]["owner_username"] == "alice"

    r = panel_client.post(f"/api/admin/workflows/{wf['id']}/shares",
                          headers=_headers(root), json={"user_id": _uid("bob")})
    assert r.status_code == 200, r.text
    assert [s["username"] for s in r.json()["shares"]] == ["bob"]

    # 普通用户调管理端的共享端点：401/403，不许混
    r = panel_client.post(f"/api/admin/workflows/{wf['id']}/shares",
                          headers=_headers(alice), json={"user_id": _uid("bob")})
    assert r.status_code in (401, 403), r.text


def test_admin_panel_share_error_keeps_the_real_reason(panel_client, alice, root):
    """管理端对越权/非法请求必须说真话。

    共享端点是从 server/app.py 挂过来的，它抛的是 PreciseHTTPException；
    管理端若不认这个类，404 会被换成「接口不存在」——那句话会把管理员
    引到完全错的方向（去查路由，而路由是好的）。
    """
    wf = _make_workflow(TestClient(svc.create_public_app()), alice)
    r = panel_client.post(f"/api/admin/workflows/{wf['id']}/shares",
                          headers=_headers(root), json={"user_id": 999999})
    assert r.status_code == 422, r.text
    assert "账号不存在" in r.text, r.text
