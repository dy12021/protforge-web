"""工作流的 store 层 CRUD 与调度器要用的那几问（规格 4.2 / 6.3 / 6.4）。

重点在末尾那几个「问」函数：它们是调度器闸门的**唯一**数据来源，
闸门写对了但这里查错，一样是静默放行或静默卡住。
"""
from __future__ import annotations

import pytest

import store


def _user(username="u1", role="user"):
    import db
    c = db.connect()
    try:
        with c:
            c.execute(
                "INSERT INTO users (username, password_hash, salt, role,"
                " disabled, created_at) VALUES (?, 'h', 's', ?, 0, 't')",
                (username, role))
        return c.execute("SELECT id FROM users WHERE username=?",
                         (username,)).fetchone()[0]
    finally:
        c.close()


def _wf(owner_id, **kw):
    return store.create_workflow(
        owner_id, kw.pop("name", "wf"),
        kw.pop("prompt_template", "查 {{seq}}"),
        **kw)


# --- 建与读 -----------------------------------------------------------------

def test_create_workflow_defaults():
    uid = _user()
    wf = _wf(uid)
    assert wf["status"] == "draft" and wf["cursor"] == 0
    assert wf["writeback_pending"] == 0 and wf["abort_requested"] == 0


def test_get_workflow_is_owner_scoped():
    a, b = _user("a"), _user("b")
    wf = _wf(a)
    assert store.get_workflow(wf["id"], owner_id=a)["id"] == wf["id"]
    with pytest.raises(store.NotFound):
        store.get_workflow(wf["id"], owner_id=b)
    # 管理员不受限
    admin = _user("root", "admin")
    assert store.get_workflow(wf["id"], owner_id=admin, is_admin=True)["id"] == wf["id"]


def test_list_workflows_is_owner_scoped():
    a, b = _user("a"), _user("b")
    _wf(a, name="A 的")
    _wf(b, name="B 的")
    names = {w["name"] for w in store.list_workflows(owner_id=a)}
    assert names == {"A 的"}


# --- 列角色 -----------------------------------------------------------------

def test_set_workflow_columns_replaces_and_is_idempotent():
    uid = _user()
    wf = _wf(uid)
    cols = [{"col_index": 0, "header": "编号", "role": "key"},
            {"col_index": 1, "header": "序列", "role": "input", "slot": "seq"},
            {"col_index": 2, "header": "项目", "role": "context", "slot": "proj"}]
    store.set_workflow_columns(wf["id"], cols)
    store.set_workflow_columns(wf["id"], cols)      # 再来一次，不能炸也不能翻倍
    got = store.list_workflow_columns(wf["id"])
    assert len(got) == 3
    assert {c["col_index"] for c in got} == {0, 1, 2}
    ctx = [c for c in got if c["role"] == "context"][0]
    assert ctx["slot"] == "proj"


def test_set_workflow_columns_rejects_bad_role():
    uid = _user()
    wf = _wf(uid)
    with pytest.raises(store.Invalid):
        store.set_workflow_columns(
            wf["id"], [{"col_index": 0, "role": "contextt"}])


# --- 行任务：三列真的写进去了 -----------------------------------------------

def test_row_task_carries_the_three_columns():
    """**读回数据库核对值**，不是断言 create_task 返回成功。

    白名单缺列时它会抛异常，忘传参时它静默传 None——只断言「没抛异常」的
    用例在两种坏写法下都是绿的。
    """
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("第 1 行", "brief", owner_id=uid,
                            actor="system", workflow_id=wf["id"],
                            workflow_row=0)
    back = store.get_task(row["id"], owner_id=uid)
    assert back["workflow_id"] == wf["id"]
    assert back["workflow_row"] == 0
    assert back["workflow_archived"] == 0


def test_regular_task_has_null_workflow_columns():
    uid = _user()
    row = store.create_task("普通", "b", owner_id=uid)
    back = store.get_task(row["id"], owner_id=uid)
    assert back["workflow_id"] is None and back["workflow_row"] is None


def test_null_workflow_id_is_the_membership_contract():
    """「workflow_id IS NULL = 不属于任何工作流」这条契约**单独**钉死。

    Task 1 只给新列补了值、没有回填历史数据，所以线上存量 task 的 workflow_id
    全是 NULL——那是**正常状态**，不是脏数据。三条各自钉一个方向，任何一条坏了
    都是静默失效（不抛异常，只是放错行或永不放行），所以不合并：

      a. 存量行（不带三列直插，等价于迁移前的行）= NULL，且**照样**是闸门⑥ 眼里的
         「常规任务」。否则迁移一上线，所有老用户的常规任务就从闸门⑥ 的视野里消失，
         等于凭空多出一份无限并发。
      b. 既有调用形态 create_task(title, brief, owner_id, actor) 一个字都不用改，
         落库必须是 NULL。
      c. 行任务绝不能混进闸门⑥ 的集合——混进去就是闸门恒真、工作流永不放行，
         而且**不报任何错**。
    """
    uid = _user()

    # a. 存量行：不带三列直插，取列的默认值（迁移前就是这样）。
    import db
    c = db.connect()
    try:
        with c:
            cur = c.execute(
                "INSERT INTO tasks (title, brief, status, step, owner_id, priority,"
                " uploads_dir, output_dir, created_at, updated_at)"
                " VALUES ('存量的', 'b', 'queued', '', ?, 0, '', '', 't', 't')",
                (uid,))
            legacy_id = cur.lastrowid
    finally:
        c.close()
    legacy = store.get_task(legacy_id, owner_id=uid)
    assert legacy["workflow_id"] is None
    assert legacy["workflow_row"] is None
    assert legacy["workflow_archived"] == 0
    assert store.user_has_live_regular_tasks(uid) is True

    # b. 既有调用形态（actor 走位置参数，不带两个新关键字参数）。
    fresh = store.create_task("常规", "b", uid, "human")
    back = store.get_task(fresh["id"], owner_id=uid)
    assert back["workflow_id"] is None and back["workflow_row"] is None

    # c. 换个只有行任务的用户：闸门⑥ 必须为假——行任务自己不能把自己挡住。
    solo = _user("solo")
    solo_wf = _wf(solo, name="solo 的")
    store.create_task("r", "b", owner_id=solo, actor="system",
                      workflow_id=solo_wf["id"], workflow_row=0)
    assert store.user_has_live_regular_tasks(solo) is False


def test_update_workflow_archived():
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=2)
    store.update_task(row["id"], workflow_archived=1)
    assert store.get_task(row["id"], owner_id=uid)["workflow_archived"] == 1


# --- 闸门要问的那几个问题 ---------------------------------------------------

def _row(wf_id, idx, status, archived=0, owner=1):
    return store.create_task(f"r{idx}", "b", owner_id=owner, actor="system",
                             workflow_id=wf_id, workflow_row=idx,
                             ) and store.update_task(
        store.list_workflow_rows(wf_id, idx)[0]["id"], status=status,
        workflow_archived=archived) and None


def test_has_unarchived_reflects_the_flag():
    uid = _user()
    wf = _wf(uid)
    assert store.workflow_has_unarchived(wf["id"]) is False
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    assert store.workflow_has_unarchived(wf["id"]) is True
    store.update_task(row["id"], status="done", workflow_archived=1)
    assert store.workflow_has_unarchived(wf["id"]) is False


def test_has_live_rows_ignores_archived_and_terminal():
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    assert store.workflow_has_live_rows(wf["id"]) is True
    store.update_task(row["id"], status="done")
    assert store.workflow_has_live_rows(wf["id"]) is False


def test_queued_rows_and_unarchived_terminal_rows():
    uid = _user()
    wf = _wf(uid)
    r0 = store.create_task("r0", "b", owner_id=uid, actor="system",
                           workflow_id=wf["id"], workflow_row=0)
    r1 = store.create_task("r1", "b", owner_id=uid, actor="system",
                           workflow_id=wf["id"], workflow_row=1)
    assert {r["id"] for r in store.workflow_queued_rows(wf["id"])} == {r0["id"], r1["id"]}
    assert store.workflow_unarchived_terminal_rows(wf["id"]) == []
    store.update_task(r0["id"], status="done")
    got = store.workflow_unarchived_terminal_rows(wf["id"])
    assert [r["workflow_row"] for r in got] == [0]


def test_unarchived_terminal_rows_excludes_already_archived():
    """归档 pass 的取数**必须**排除已归档的行。

    探针实测过的洞：把 `AND workflow_archived = 0` 摘掉，brief 那 15 条用例**全绿**——
    它们从没让一行处于「终态且已归档」。摘掉之后归档 pass 会把已经写回表的行再写一遍。
    后半段走的是管理员重跑的形状（archived 清回 0），那种行必须重新进待归档集合。
    """
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    store.update_task(row["id"], status="done", workflow_archived=1)
    assert store.workflow_unarchived_terminal_rows(wf["id"]) == []
    # 管理员重跑把 1 清回 0（规格 6.5），这行就得重新被扫到。
    store.update_task(row["id"], workflow_archived=0)
    got = store.workflow_unarchived_terminal_rows(wf["id"])
    assert [r["id"] for r in got] == [row["id"]]


def test_user_has_live_regular_tasks_scopes_to_owner():
    a, b = _user("a"), _user("b")
    store.create_task("A 的", "b", owner_id=a)
    assert store.user_has_live_regular_tasks(a) is True
    assert store.user_has_live_regular_tasks(b) is False


def test_workflow_rows_do_not_count_as_regular_tasks():
    """闸门⑥ 问的是常规任务；行任务自己不能把自己挡住。"""
    uid = _user()
    wf = _wf(uid)
    store.create_task("r", "b", owner_id=uid, actor="system",
                      workflow_id=wf["id"], workflow_row=0)
    assert store.user_has_live_regular_tasks(uid) is False


def test_skips_round_trip_and_block_creation():
    uid = _user()
    wf = _wf(uid)
    store.skip_workflow_row(wf["id"], 3, reason="序列坏了")
    store.skip_workflow_row(wf["id"], 3)          # 再来一次，不能炸
    assert store.list_workflow_skips(wf["id"]) == {3}


def test_delete_workflow_cascades_rows():
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    store.delete_workflow(wf["id"])
    assert store.list_workflows(owner_id=uid) == []
    with pytest.raises(store.NotFound):
        store.get_task(row["id"])
