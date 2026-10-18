"""值班台对工作流行任务的三处处理（规格 6.5 守卫侧 / 8.3）。

最狠的一条是 force_status 能设 running/packaging：库里真写成非终态，
claim_queued 接不走，闸门⑤从此恒真，**整个工作流从那一行起永久卡死**，
而界面显示「正在跑第 N 行」且没有任何进程。

三处 fixture（`admin_headers` / `one_row` / `one_regular`）**本文件自带**：
- `tests/conftest.py` 是冻结的（只加了 `server/` 到 sys.path）；
- `admin_headers` 原本只定义在 `tests/test_workflow_api.py`，import 它会引入
  跨文件耦合与收集顺序依赖；`one_row` / `one_regular` 全仓库此前不存在。
所以按本仓库既有惯例在本地各定义一份。

每条围栏都**读回数据库**核对，而不是只看 HTTP 状态码——只断言 200/409 的
测试，在「守卫拦下了但顺手把状态也改了」这种坏写法下依然全绿。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# admin/ 不在 conftest 的 sys.path 里（conftest 冻结，只加了 server/），
# 与 tests/test_bash_audit.py、test_admin_stream.py 同一套做法。
ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

import app_admin as admin_svc       # noqa: E402
import auth                         # noqa: E402
import store                        # noqa: E402


def _headers(username: str, role: str = "user") -> dict:
    """真发令牌，不走 dev-login 旁路——旁路开着等于这个进程所有接口都不要凭证。"""
    auth.create_user(username, "pw-for-tests", role=role)
    row = auth.get_user(username)
    return {"Authorization": "Bearer " + auth.issue_token(int(row["id"]))}


@pytest.fixture()
def client():
    return TestClient(admin_svc.create_admin_app())


@pytest.fixture()
def admin_headers():
    return _headers("root-admin", role="admin")


@pytest.fixture()
def owner_id():
    auth.create_user("alice-row", "pw-for-tests", role="user")
    return int(auth.get_user("alice-row")["id"])


@pytest.fixture()
def one_row(owner_id):
    """一条**已终态（failed）且带 workflow_id** 的行任务。

    初始状态钉成 failed 有两个原因：围栏 1 的读回核对断言的是 `== "failed"`
    （没被守卫改动），而 failed 又是「人看得出来的坏状态」——值班台正是为它存在。
    """
    wid = store.create_workflow(owner_id, "值班台用表.xlsx", "算 {key}",
                                total_rows=1)["id"]
    task = store.create_task("第 1 行", "brief", owner_id=owner_id,
                             actor="system", workflow_id=wid, workflow_row=0)
    tid = int(task["id"])
    store.set_status(tid, "failed", actor="system", message="算崩了")
    back = store.get_task(tid)
    # fixture 自证：这两条断言是围栏 1 的**前提**。fixture 若悄悄造出 queued 或
    # 普通任务，下面两条围栏会变成恒真而不报任何错。
    assert back["workflow_id"] is not None, "one_row 必须是行任务"
    assert back["status"] == "failed", "one_row 的初始状态必须是 failed"
    return back


@pytest.fixture()
def one_regular(owner_id):
    """一条**不带 workflow_id** 的普通任务（闸门⑥ 的 `workflow_id IS NULL`）。"""
    task = store.create_task("普通任务", "brief", owner_id=owner_id)
    tid = int(task["id"])
    store.set_status(tid, "failed", actor="system", message="算崩了")
    back = store.get_task(tid)
    assert back["workflow_id"] is None, "one_regular 必须不属于任何工作流"
    return back


# ------------------------------------------------------------- force_status

def test_force_status_running_on_row_task_is_409(client, admin_headers, one_row):
    """设成 running/packaging 会让整批从这一行起永久卡死（规格 6.5 守卫侧）。"""
    tid = one_row["id"]
    for target in ("running", "packaging"):
        r = client.post(f"/api/admin/tasks/{tid}/status",
                        headers=admin_headers, json={"status": target})
        assert r.status_code == 409, target
        assert "工作流" in r.json()["detail"]
        # 读回库核对：**光断言 409 不够**——「先拦下再把状态改了」照样 409，
        # 但那一行已经被推进非终态，闸门⑤ 仍然恒真。
        assert store.get_task(tid)["status"] == "failed", target


def test_force_status_terminal_still_allowed(client, admin_headers, one_row):
    """终态是安全的（没人会接走它），守卫不许扩大到终态。"""
    tid = one_row["id"]
    r = client.post(f"/api/admin/tasks/{tid}/status",
                    headers=admin_headers, json={"status": "done"})
    assert r.status_code == 200, r.text
    assert store.get_task(tid)["status"] == "done"


def test_force_status_regular_task_unaffected(client, admin_headers, one_regular):
    """**防误伤**：守卫只针对行任务，普通任务照常能设 running。"""
    tid = one_regular["id"]
    r = client.post(f"/api/admin/tasks/{tid}/status",
                    headers=admin_headers, json={"status": "running"})
    assert r.status_code == 200, r.text
    assert store.get_task(tid)["status"] == "running"


def test_force_status_terminal_on_row_clears_archived(client, admin_headers,
                                                      one_row):
    """force 成终态后必须清 workflow_archived，否则界面说完成、表里还是旧值。

    与 retry 的 bug 是同一个、只是入口不同：归档 pass 扫的是
    「终态 **且** archived=0」，archived=1 的行它永远扫不到。
    """
    tid = one_row["id"]
    store.update_task(tid, workflow_archived=1)
    assert store.get_task(tid)["workflow_archived"] == 1
    r = client.post(f"/api/admin/tasks/{tid}/status",
                    headers=admin_headers, json={"status": "done"})
    assert r.status_code == 200, r.text
    assert store.get_task(tid)["workflow_archived"] == 0


# -------------------------------------------------------------------- retry

def test_retry_clears_archived(client, admin_headers, one_row):
    """漏清这个标记 → 重跑跑完了、状态是 done、但表里还是旧值（规格 6.5）。"""
    tid = one_row["id"]
    store.update_task(tid, workflow_archived=1)
    r = client.post(f"/api/admin/tasks/{tid}/retry", headers=admin_headers)
    assert r.status_code == 200, r.text
    back = store.get_task(tid)
    assert back["status"] == "queued"
    assert back["workflow_archived"] == 0


# --------------------------------------------------------------- 值班台列表

def test_console_list_excludes_workflow_rows(client, admin_headers, one_row,
                                             one_regular):
    """默认排除行任务：值班台是「现在有什么不对」的地方。"""
    r = client.get("/api/admin/tasks", headers=admin_headers)
    assert r.status_code == 200, r.text
    ids = {t["id"] for t in r.json()["tasks"]}
    # 自证探针：下面两条断言在任何「列表恒空」的实现下也成立，那就白测了。
    assert ids, "列表不能是空的，否则这组断言恒真"
    assert one_regular["id"] in ids
    assert one_row["id"] not in ids


def test_console_can_still_see_workflow_rows_explicitly(client, admin_headers,
                                                        one_row):
    """排除不等于藏起来：显式 ?scope=workflow 仍然看得到。"""
    r = client.get("/api/admin/tasks?scope=workflow", headers=admin_headers)
    assert r.status_code == 200, r.text
    ids = {t["id"] for t in r.json()["tasks"]}
    assert ids, "列表不能是空的，否则这组断言恒真"
    assert one_row["id"] in ids


def test_console_scope_all_shows_both(client, admin_headers, one_row,
                                      one_regular):
    ids = {t["id"] for t in client.get("/api/admin/tasks?scope=all",
                                       headers=admin_headers).json()["tasks"]}
    assert ids, "列表不能是空的，否则这组断言恒真"
    assert {one_row["id"], one_regular["id"]} <= ids


def test_scope_invalid_is_422(client, admin_headers):
    """scope 是白名单：随手编的值不能落进「不过滤」的分支。"""
    r = client.get("/api/admin/tasks?scope=bogus", headers=admin_headers)
    assert r.status_code == 422, r.text
