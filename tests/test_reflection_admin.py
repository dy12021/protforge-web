"""值班台对反思记录的处理：**重跑**这条唯一的补救口子。

为什么单独立一个文件：`tests/test_reflection.py` 测的是 server 层的反思生成，
不带 admin 应用；这里测的是 HTTP 端点 + 库里状态的组合。两边各自自带 fixture
（`admin_headers` 那种）——`tests/conftest.py` 是冻结的，跨文件 import fixture
会引入收集顺序依赖。

**这条围栏是被真机抓出来的**：值班台上那条「生成失败」的反思卡片上**一个按钮
都没有**。而 `generate` 见到记录就直接返回，于是模型调不通一次、正文写坏一次，
这条反思就永远停在失败态——管理员既不能重试也不能否决，只能干看着。

每条围栏都**读回数据库**核对，而不是只看 HTTP 状态码。
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
import db                           # noqa: E402
import reflection                   # noqa: E402
import store                        # noqa: E402


@pytest.fixture()
def client():
    return TestClient(admin_svc.create_admin_app())


@pytest.fixture()
def admin_headers():
    auth.create_user("rsi-root", "pw-for-tests", role="admin")
    row = auth.get_user("rsi-root")
    return {"Authorization": "Bearer " + auth.issue_token(int(row["id"]))}


@pytest.fixture()
def user_headers():
    auth.create_user("rsi-plain", "pw-for-tests", role="user")
    row = auth.get_user("rsi-plain")
    return {"Authorization": "Bearer " + auth.issue_token(int(row["id"]))}


@pytest.fixture()
def owner_id():
    auth.create_user("rsi-owner2", "pw-for-tests", role="user")
    return int(auth.get_user("rsi-owner2")["id"])


def _terminal_task(owner: int) -> int:
    """一条已收工的普通计算任务。"""
    t = store.create_task("收工了", "brief", owner_id=owner)
    tid = int(t["id"])
    store.set_status(tid, "running", actor="system")
    store.set_status(tid, "done", actor="system", message="收工")
    return tid


def _failed_reflection(tid: int) -> dict:
    """一条「生成失败」的反思记录，连同它那份失败存根。"""
    p = reflection.body_path(tid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# 反思 · 任务 #{tid}（生成失败）\n\n- 失败原因：模型调不通\n",
                 encoding="utf-8")
    rec = store.create_reflection(tid, str(p))
    store.mark_reflection_failed(rec["id"], "模型调不通")
    return store.get_reflection(rec["id"])


def test_retry_only_works_on_a_failed_reflection(client, admin_headers, owner_id):
    """已经分类过的那条**不许**重跑。

    重跑会把管理员的判断一起冲掉：判过「经验」的那条，重跑之后模型可能给出
    完全不同的建议，而那条已经写进经验文件的经验不会跟着撤。
    """
    tid = _terminal_task(owner_id)
    rec = store.create_reflection(tid, str(reflection.body_path(tid)))
    store.classify_reflection(rec["id"], "lesson", "", "rsi-root")

    r = client.post(f"/api/admin/reflections/{rec['id']}/retry", headers=admin_headers)
    # 422 是这个仓库对 store.Invalid 的既定映射（app_admin 的异常处理器）。
    assert r.status_code == 422, f"已分类的反思居然能重跑：{r.status_code} {r.text}"
    # 拒绝之后这条记录必须还在。
    assert store.get_reflection(rec["id"]) is not None, \
        "被拒绝的重跑把记录也删了：管理员的分类被冲掉了"


def test_retry_clears_the_record_so_the_sweep_can_pick_the_task_up_again(
        client, admin_headers, owner_id):
    """重跑之后，这条任务重新变成「终态但还没反思」，巡检下一轮就会重做一遍。

    这条是重跑能生效的**唯一机制**：端点只删不生成，生成由 dispatcher 那个
    进程做（admin 与 public 是两个进程，各生成一次的话两份正文会互相覆盖）。
    """
    tid = _terminal_task(owner_id)
    rec = _failed_reflection(tid)
    assert store.get_reflection_by_task(tid) is not None

    r = client.post(f"/api/admin/reflections/{rec['id']}/retry", headers=admin_headers)
    assert r.status_code == 200, f"重跑失败：{r.status_code} {r.text}"

    assert store.get_reflection_by_task(tid) is None, \
        "重跑之后记录还在：巡检扫不到它，这条反思永远重做不了"
    swept = [int(x["id"]) for x in store.terminal_tasks_without_reflection(limit=20)]
    assert tid in swept, "重跑之后巡检扫不到这条任务：它永远不会被重新生成"


def test_retry_removes_the_stale_failure_stub(client, admin_headers, owner_id):
    """那份「生成失败」的存根要一起删掉。

    留着的话，管理员展开正文看到的是上一次的失败说明，而新的反思正文还没
    生成出来——两个说法同时在页面上，没有一个是真的。
    """
    tid = _terminal_task(owner_id)
    rec = _failed_reflection(tid)
    body = reflection.body_path(tid)
    assert body.is_file()

    r = client.post(f"/api/admin/reflections/{rec['id']}/retry", headers=admin_headers)
    assert r.status_code == 200, f"重跑失败：{r.status_code} {r.text}"
    assert not body.exists(), "旧的失败存根还在：页面上会同时挂着两个说法"


def test_retry_needs_an_admin(client, user_headers, owner_id):
    """普通用户不许重跑。反思是管理员的活。"""
    tid = _terminal_task(owner_id)
    rec = _failed_reflection(tid)

    r = client.post(f"/api/admin/reflections/{rec['id']}/retry", headers=user_headers)
    assert r.status_code in (401, 403), \
        f"普通用户能重跑管理员的反思：{r.status_code}"
    assert store.get_reflection_by_task(tid) is not None, \
        "没被拦住却把记录删了"
