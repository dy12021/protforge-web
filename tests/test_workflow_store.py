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


# --- 改 ---------------------------------------------------------------------

def test_update_workflow_writes_every_whitelisted_field():
    """**逐字段读回核对**，不看返回值。

    白名单缺一项时它抛 Invalid——而归档 pass 的 `except (store.Invalid, OSError)`
    会把它吞成一行日志，于是 Excel 被占用时的降级写回**永远失败且不报错**。
    少一个 `writeback_pending` 就是这种后果，所以这里把白名单整个铺开逐项断言。
    """
    uid = _user()
    wf = _wf(uid)
    fields = {"name": "改过的名字", "sheet": "Sheet1", "source_path": "C:/x.xlsx",
              "prompt_template": "算 {{seq}} 的 {{proj}}", "header_row": 2,
              "key_col": 3, "total_rows": 9, "cursor": 5, "writeback": 1,
              "writeback_pending": 1, "abort_requested": 1,
              "result_path": "out.xlsx", "status": "running"}
    store.update_workflow(wf["id"], **fields)
    got = store.get_workflow(wf["id"], owner_id=uid)
    for k, v in fields.items():
        assert got[k] == v, k


def test_update_workflow_rejects_fields_outside_the_whitelist():
    """白名单外的字段必须抛 Invalid。尤其 owner_id：改它等于把工作流送给别人。"""
    uid = _user()
    wf = _wf(uid)
    for bad in ("owner_id", "id", "created_at", "updated_at", "nope"):
        with pytest.raises(store.Invalid):
            store.update_workflow(wf["id"], **{bad: 1})
    assert store.get_workflow(wf["id"], owner_id=uid)["owner_id"] == uid


def test_update_workflow_rejects_unknown_status():
    uid = _user()
    wf = _wf(uid)
    with pytest.raises(store.Invalid):
        store.update_workflow(wf["id"], status="runningg")
    assert store.get_workflow(wf["id"], owner_id=uid)["status"] == "draft"
    for ok in ("draft", "running", "paused", "finished"):
        assert store.update_workflow(wf["id"], status=ok)["status"] == ok


def test_update_workflow_raises_notfound_when_the_row_is_gone():
    """rowcount == 0 那一支。空更新走的是 get_workflow，同样必须是 NotFound。"""
    uid = _user()
    wf = _wf(uid)
    store.delete_workflow(wf["id"], owner_id=uid)
    with pytest.raises(store.NotFound):
        store.update_workflow(wf["id"], status="running")
    with pytest.raises(store.NotFound):
        store.update_workflow(wf["id"])          # 空 fields -> get_workflow


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
    # 缩列：解绑一列再保存，旧列行必须**跟着没了**。若 DELETE 只按本次传到的
    # col_index 删，上面两次调用照样对，但这里会是 3 行——用户以为已解绑的那一列
    # 仍在参与渲染，且渲染时拿到的是已经废弃的绑定。
    store.set_workflow_columns(wf["id"], cols[:2])
    got = store.list_workflow_columns(wf["id"])
    assert len(got) == 2
    assert {c["col_index"] for c in got} == {0, 1}


def test_set_workflow_columns_rejects_bad_col_index():
    """缺列号 / 列号不是整数都得是 Invalid（→ 422），不能冒成 KeyError（→ 500）。"""
    uid = _user()
    wf = _wf(uid)
    with pytest.raises(store.Invalid):
        store.set_workflow_columns(wf["id"], [{"header": "编号", "role": "key"}])
    with pytest.raises(store.Invalid):
        store.set_workflow_columns(wf["id"], [{"col_index": "第二列", "role": "key"}])
    assert store.list_workflow_columns(wf["id"]) == []


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

def test_list_running_workflows_returns_only_running_in_id_order():
    """放行流程的**总入口**：`_workflow_run_pass` 每秒只拿这一个列表。

    把它改成 `WHERE status = 'runin'` 或干脆漏掉 WHERE，下面 24 条用例全绿，而调度器
    每秒拿到空列表 → 一行工作流任务都放不出去，功能整体死掉且不报任何错。
    （双向验证实测：这两种坏写法各让**本条**转红，其余 23 条照绿。）

    排序围栏里 owner 顺序与 id 顺序**故意反过来**（先给 owner 大的建工作流）：这样
    查询一旦退化成走 (owner_id, status) 索引返回，这条会红。同一个用户建两个工作流
    是测不出来的——那种情况下插入序恰好就是 id 序。
    """
    early = _user("a")      # 用户 id 小
    late = _user("b")       # 用户 id 大
    first = _wf(late, name="先建的")      # 工作流 id 小、owner id 大
    second = _wf(early, name="后建的")     # 工作流 id 大、owner id 小
    assert first["id"] < second["id"]
    assert first["owner_id"] > second["owner_id"]

    assert store.list_running_workflows() == []          # 一个 running 都没有

    store.update_workflow(first["id"], status="running")
    store.update_workflow(second["id"], status="running")
    assert [w["id"] for w in store.list_running_workflows()] == [
        first["id"], second["id"]]

    # draft / paused / finished 三个非 running 的态，一个都不许被返回。
    store.update_workflow(first["id"], status="draft")
    assert [w["id"] for w in store.list_running_workflows()] == [second["id"]]
    store.update_workflow(first["id"], status="finished")
    store.update_workflow(second["id"], status="paused")
    assert store.list_running_workflows() == []


def test_list_workflow_rows_filters_by_row_index():
    """`row_index` 那个分支是 Task 6「跳过第 N 行」端点**唯一**的依赖。

    `if row_index is None:` 被改成恒真时，「跳过第 5 行」会把该工作流**所有**行
    任务强制置 failed 并逐条写事件，而不是只动第 5 行。
    """
    uid = _user()
    wf = _wf(uid)
    made = [store.create_task(f"r{i}", "b", owner_id=uid, actor="system",
                              workflow_id=wf["id"], workflow_row=i)
            for i in range(3)]

    every = store.list_workflow_rows(wf["id"])
    assert [r["id"] for r in every] == [m["id"] for m in made]
    assert [r["workflow_row"] for r in every] == [0, 1, 2]

    assert [r["id"] for r in store.list_workflow_rows(wf["id"], 1)] == [made[1]["id"]]
    assert store.list_workflow_rows(wf["id"], 99) == []

    # 另一个工作流的行不许混进来（否则「跳过第 5 行」会误伤另一个工作流）。
    other = _wf(_user("o"), name="别人的")
    store.create_task("x", "b", owner_id=uid, actor="system",
                      workflow_id=other["id"], workflow_row=0)
    assert len(store.list_workflow_rows(other["id"])) == 1
    assert [r["workflow_row"] for r in store.list_workflow_rows(wf["id"])] == [0, 1, 2]


def test_has_unarchived_reflects_the_flag():
    uid = _user()
    wf = _wf(uid)
    assert store.workflow_has_unarchived(wf["id"]) is False
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    assert store.workflow_has_unarchived(wf["id"]) is True
    store.update_task(row["id"], status="done", workflow_archived=1)
    assert store.workflow_has_unarchived(wf["id"]) is False


def test_has_live_rows_counts_non_terminal_only():
    """闸门⑤ 只看 status，**不看** workflow_archived。

    用例名里不再提 archived：规格 6.4 的闸门⑤ 原文就是「有非终态的行任务」，实现里
    也没有 archived 过滤——审查已证明**反过来加过滤是错的**（archived=1 且非终态的行
    会既过⑤、又不过②③，直接放行下一行，同一工作流两行同时推进）。残留风险归
    Task 7 的 409 守卫，别把「archived」写进这条的名字里骗下一个人。
    """
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    assert store.workflow_has_live_rows(wf["id"]) is True
    store.update_task(row["id"], status="done")
    assert store.workflow_has_live_rows(wf["id"]) is False
    store.update_task(row["id"], status="failed")
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
    # failed **也是**终态，归档 pass 必须扫到它——错误信息同样要写回表。
    # 这半条是 db.TERMINAL_STATUSES 去重后唯一能咬住它的围栏：只收 done 的
    # 归档取数会把失败行永远留在「未归档」里，闸门② 于是恒真、整批锁死。
    r2 = store.create_task("r2", "b", owner_id=uid, actor="system",
                           workflow_id=wf["id"], workflow_row=2)
    store.update_task(r2["id"], status="failed")
    got = store.workflow_unarchived_terminal_rows(wf["id"])
    assert [r["workflow_row"] for r in got] == [0, 2]


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


def test_skips_round_trip_is_idempotent():
    """跳过集只测存取与幂等。

    名字里不再提 block_creation：store 层**没有**「阻止建行」这个能力，跳过集是
    放行时（Task 5）用来过滤数据行的，这里没有「建行」动作可挡。谁要挡，是 Task 5
    的闸门④ 拿这个集合去比 cursor。
    """
    uid = _user()
    wf = _wf(uid)
    store.skip_workflow_row(wf["id"], 3, reason="序列坏了")
    store.skip_workflow_row(wf["id"], 3)          # 再来一次，不能炸也不能插第二条
    assert store.list_workflow_skips(wf["id"]) == {3}
    store.skip_workflow_row(wf["id"], 4, reason="另一条")
    assert store.list_workflow_skips(wf["id"]) == {3, 4}
    assert store.list_workflow_skips(_wf(_user("x"), name="别人的")["id"]) == set()


def test_delete_workflow_cascades_rows():
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    store.delete_workflow(wf["id"], owner_id=uid)
    assert store.list_workflows(owner_id=uid) == []
    with pytest.raises(store.NotFound):
        store.get_workflow(wf["id"], owner_id=uid, is_admin=True)
    with pytest.raises(store.NotFound):
        store.get_task(row["id"])


def test_delete_workflow_is_owner_scoped():
    """围栏②：越权 owner_id 删不动，且**库里的东西一个没少**。

    只断「抛了 NotFound」是不够的——实现写成「先中止在跑的行任务、再抛
    NotFound」照样能过，而那时 A 的计算已经被杀、时间线已经被伪造了。
    所以必须回读数据库核对值，而不是只断函数抛没抛。
    """
    a, b = _user("a"), _user("b")
    wf = _wf(a)
    row = store.create_task("r", "b", owner_id=a, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    with pytest.raises(store.NotFound):
        store.delete_workflow(wf["id"], owner_id=b)
    assert store.get_workflow(wf["id"], owner_id=a)["id"] == wf["id"], "工作流被删了"
    assert store.get_task(row["id"], owner_id=a)["status"] == "queued", "行任务被动了"


def test_delete_workflow_without_owner_is_refused():
    """围栏③：缺 owner_id 且非管理员 → NotFound，一个字节都没删。

    **这是整个修复的核心：默认拒绝。** store 层这闸门存在的全部意义就是拦
    「将来某个新调用方忘传参数」那种疏漏——忘传就该炸掉，而不是恰好删掉别人的
    工作流。注意不能只靠 get_workflow：它在 owner_id 为 None 时是**放行**的
    （语义是「没声明是谁就当内部调用」），拿它当闸门的话这里恰好会删成。

    两种「没传」都断：完全不传、以及显式传 None + is_admin=False。
    """
    uid = _user()
    wf = _wf(uid)
    row = store.create_task("r", "b", owner_id=uid, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    with pytest.raises(store.NotFound):
        store.delete_workflow(wf["id"])          # 完全不传参数
    with pytest.raises(store.NotFound):
        store.delete_workflow(wf["id"], owner_id=None, is_admin=False)
    assert store.get_workflow(wf["id"], owner_id=uid)["id"] == wf["id"], "工作流被删了"
    assert store.get_task(row["id"], owner_id=uid)["status"] == "queued", "行任务被动了"
    assert store.list_workflows(owner_id=uid), "列表里也不该少东西"


def test_delete_workflow_admin_path_works():
    """围栏④：管理员路径确实能用——默认拒绝不能把管理员一起拒掉。

    管理员带别人的 owner_id 能删，且不声明 owner 也能删（is_admin=True 时
    默认拒绝不适用）。两条都断，否则「一律拒绝、谁也删不掉」也能蒙混过关。
    """
    a, c = _user("a"), _user("c")
    admin = _user("root", "admin")
    wf = _wf(a)
    row = store.create_task("r", "b", owner_id=a, actor="system",
                            workflow_id=wf["id"], workflow_row=0)
    store.delete_workflow(wf["id"], owner_id=admin, is_admin=True)
    with pytest.raises(store.NotFound):
        store.get_workflow(wf["id"], is_admin=True)
    with pytest.raises(store.NotFound):
        store.get_task(row["id"], is_admin=True)
    # 不声明 owner 的管理员路径
    wf2 = _wf(c, name="另一个")
    store.delete_workflow(wf2["id"], is_admin=True)
    with pytest.raises(store.NotFound):
        store.get_workflow(wf2["id"], is_admin=True)
