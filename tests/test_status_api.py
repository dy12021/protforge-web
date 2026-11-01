"""状态面板的后端（server/status_api.py）。

四条规矩，每条都要有**能红的**围栏：

1. **系统指标与全局速度只对管理员账号可见。** 常规用户调 /api/status/system
   必须 403 —— 不是「拿到了再由前端藏起来」。
2. **常规用户只看到自己的任务。** 别人的任务数、用时，一个都不能漏出去。
3. **单行用时只算真正在跑的那一段。** 工作流是先把整批行任务建出来再串行跑
   的，用 created_at 当起点会把「排队等前面的行」也算进去（实测某批 61 行的
   表：单行中位 8.4 小时，其中大半是在排队）。
4. **取不到就说取不到。** 没有样本时给 null，不给 0：0 会被面板显示成
   「成功率 0%」「平均耗时 0 秒」，那是在对着没发生过的事报平安。

围栏的形状：每条断言都**两头**能红——把实现改坏它就红（缺陷要红），
把实现改对它不红（正确实现不许误红）。造数据一律用**固定时间戳**直写库，
不用 sleep 猜时间：那既是慢的，也是不稳的（机器一忙就误红）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# admin/ 不在 conftest 的 sys.path 里（conftest 冻结，只加了 server/），
# 与 tests/test_workflow_admin.py、test_bash_audit.py 同一套做法。
ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

import app as svc              # noqa: E402
import app_admin as admin_svc  # noqa: E402
import auth                    # noqa: E402
import db                      # noqa: E402
import status_api              # noqa: E402
import store                   # noqa: E402

PWD = "password-123"


# ------------------------------------------------------------- 夹具

@pytest.fixture
def owner():
    return auth.create_user("owner1", PWD, role="user")


@pytest.fixture
def other():
    return auth.create_user("other1", PWD, role="user")


@pytest.fixture
def admin():
    return auth.create_user("boss", PWD, role="admin")


@pytest.fixture
def env():
    """用户面板（8473 那个 app）。它已经把状态 router 挂在 /api/status 下了。"""
    with TestClient(svc.create_public_app()) as c:
        yield c


def _bearer(user):
    return {"Authorization": f"Bearer {auth.issue_token(user['id'])}"}


def _make_task(owner_id, title="t", brief="b"):
    return store.create_task(title, brief, owner_id)


def _mk_finished(owner_id, status, started, ended, workflow_id=None,
                 workflow_row=None, title="t", queue_before="00:05:00"):
    """直写库造一条**时间固定**的终态任务，返回 task id。

    三个时间点都钉死：
      * ``created_at`` = 开始跑之前 5 分钟（那是排队时间，**不该**进用时）
      * events 里一条 ``to_status='running'`` 的状态事件 = 真正的起点
      * ``done_at`` = 结束

    走 store.create_task 的话 created_at 是「现在」，用时没法算准；
    这里能断言到一个确定的值（多 5 分钟的排队就是实现错了）。
    """
    created = _minus(started, queue_before)
    con = db.connect()
    try:
        with con:
            cur = con.execute(
                """INSERT INTO tasks
                   (title, brief, status, step, owner_id, priority,
                    uploads_dir, output_dir, package_path, summary,
                    error, session_id, workflow_id, workflow_row,
                    created_at, updated_at, done_at)
                   VALUES (?, '', ?, '', ?, 0, '', '', '', '', '', '', ?, ?,
                           ?, ?, ?)""",
                (title, status, int(owner_id), workflow_id, workflow_row,
                 created, ended, ended))
            tid = cur.lastrowid
            con.execute(
                """INSERT INTO events (task_id, kind, from_status, to_status,
                                        actor, message, created_at)
                   VALUES (?, 'status', 'queued', 'running', 'system', '', ?)""",
                (tid, started))
    finally:
        con.close()
    return tid


def _minus(ts, hms):
    """'2026-10-20T10:00:00.000' 往前推 '00:05:00'。"""
    from datetime import datetime, timedelta
    h, m, s = (int(x) for x in hms.split(":"))
    base = datetime.fromisoformat(ts)
    return (base - timedelta(hours=h, minutes=m, seconds=s)).isoformat()


# ------------------------------------------------------------- 可见性

def test_a_regular_user_sees_only_their_own_tasks(env, owner, other):
    _make_task(owner["id"], "a1")
    _make_task(owner["id"], "a2")
    _make_task(other["id"], "b1")

    r = env.get("/api/status/overview", headers=_bearer(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "user"
    assert body["tasks"]["total"] == 2, "别人的任务数漏出去了：%r" % body["tasks"]


def test_a_regular_users_numbers_never_include_the_others(env, owner, other):
    """别人有 7 个任务时，我的面板上一个都不许出现。

    只断「total == 2」是不够的：哪天有人把别的口径（比如吞吐、失败数）
    混进来，那条断言照样绿。这里断的是**整个响应体**里都没有别人的数。
    """
    for i in range(7):
        _make_task(other["id"], "别人的 %d" % i)
    _make_task(owner["id"], "我的")

    body = env.get("/api/status/overview", headers=_bearer(owner)).json()
    assert body["tasks"]["total"] == 1
    assert body["tasks"]["failed"] == 0
    assert body["tasks"]["done"] == 0
    # 全局吞吐是「别人任务的完成速度」，绝不能出现。
    assert "throughput_per_hour" not in body, \
        "常规用户拿到了全局速度：%r" % body


def test_an_admin_sees_everything(env, owner, other, admin):
    _make_task(owner["id"], "a1")
    _make_task(other["id"], "b1")
    # **必须有一个已完成的任务**：全局吞吐是拿完成时间跟 24 小时前的
    # cutoff 比的，而 cutoff 与库里的时间戳必须同为 naive —— 混了会
    # TypeError，真机上直接 500。
    done = _make_task(owner["id"], "a2")
    store.set_status(done["id"], "done", actor="system", message="ok")

    r = env.get("/api/status/overview", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "admin"
    assert body["tasks"]["total"] == 3
    assert "throughput_per_hour" in body, "管理员看不到全局吞吐"
    assert body["throughput_per_hour"] >= 0


def test_a_regular_user_does_not_get_the_global_throughput(env, owner):
    r = env.get("/api/status/overview", headers=_bearer(owner))
    body = r.json()
    assert "throughput_per_hour" not in body, \
        "常规用户拿到了全局速度：%r" % body


def test_the_overview_never_carries_the_system_metrics(env, owner, admin):
    """系统指标**只能**从 /api/status/system 拿。

    塞进 overview 再按角色删字段是另一条路，但那条路一旦漏删就是泄露；
    单独一个端点让「拿不到」成为服务端的事实。
    """
    body = env.get("/api/status/overview", headers=_bearer(owner)).json()
    assert "system" not in body, "overview 里混进了系统指标：%r" % body


def test_system_metrics_are_forbidden_for_regular_users(env, owner):
    r = env.get("/api/status/system", headers=_bearer(owner))
    assert r.status_code == 403, "常规用户拿到了系统指标：%r" % r.text


def test_an_admin_can_read_the_system_metrics(env, admin):
    r = env.get("/api/status/system", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("cpu", "memory", "gpu", "disk"):
        assert key in body, "少了 %s：%r" % (key, body)
    assert "percent" in body["cpu"]
    assert "percent" in body["memory"]
    # GPU 可能没有 —— 那时 available=False，不是缺字段
    assert "available" in body["gpu"]


def test_the_system_endpoint_needs_a_login(env):
    r = env.get("/api/status/system")
    assert r.status_code in (401, 422), r.text


def test_the_overview_needs_a_login(env):
    r = env.get("/api/status/overview")
    assert r.status_code in (401, 422), r.text


# ------------------------------------------------------------- 用时口径

def test_queueing_time_is_not_counted_as_runtime(env, owner):
    """created_at 是「任务被建出来」，不是「它开始跑」。

    工作流把整批行任务先建出来再串行跑，用 created_at 当起点的话，
    单行用时里会塞进「等前面那些行跑完」的时间——实测偏差能到几倍。
    """
    t = _make_task(owner["id"], "慢启动")
    time.sleep(1.0)                      # 这一秒是「排队」
    store.set_status(t["id"], "running", actor="system", message="开始")
    store.set_status(t["id"], "done", actor="system", message="完成")

    r = env.get("/api/status/overview", headers=_bearer(owner))
    body = r.json()
    assert body["duration"]["count"] == 1
    secs = body["duration"]["avg_seconds"]
    # 界限 0.8 秒：实现正确时这段只有几毫秒（两次 set_status 之间），
    # 实现错了（拿 created_at 当起点）会是 1 秒以上。**不要收到 0.2 秒**——
    # 那不是抓得更紧，是把「机器正忙」也判成失败。
    assert secs < 0.8, \
        "排队时间被算进单行用时了（%s 秒）：它该从认领那一刻起算" % secs


def test_the_average_duration_is_computed_from_fixed_data(env, owner):
    """**固定造的数据**算平均用时：要能断言到一个确定的值。

    三个任务分别真跑了 10 / 20 / 30 秒，而每个都多排了 5 分钟的队。
    平均必须是 20 秒——多出来的 5 分钟是排队，不算用时。
    """
    _mk_finished(owner["id"], "done", "2026-10-20T10:00:00.000",
                 "2026-10-20T10:00:10.000")
    _mk_finished(owner["id"], "done", "2026-10-20T11:00:00.000",
                 "2026-10-20T11:00:20.000")
    _mk_finished(owner["id"], "done", "2026-10-20T12:00:00.000",
                 "2026-10-20T12:00:30.000")

    body = env.get("/api/status/overview", headers=_bearer(owner)).json()
    d = body["duration"]
    assert d["count"] == 3, d
    assert d["avg_seconds"] == 20.0, \
        "平均用时应是 20 秒（排队那 5 分钟不算）：%r" % d
    assert d["median_seconds"] == 20.0, d
    assert d["max_seconds"] == 30.0, d
    assert body["success_rate"] == 1.0


def test_a_rerun_task_is_timed_from_its_last_run(env, owner):
    """管理员重跑会让任务有**两条** running 事件。

    取第一条的话，用时里会混进「上一轮跑完到这一轮重跑」之间的空档——
    那个空档可能是一天，而任务其实只跑了 30 秒。
    """
    _mk_finished(owner["id"], "done", "2026-10-20T10:00:00.000",
                 "2026-10-20T10:00:05.000")
    tid = _mk_finished(owner["id"], "done", "2026-10-21T10:00:00.000",
                       "2026-10-21T10:00:05.000")
    # 第一轮：早得多的一条 running 事件（同一条任务）。
    con = db.connect()
    try:
        with con:
            con.execute(
                """INSERT INTO events (task_id, kind, from_status, to_status,
                                        actor, message, created_at)
                   VALUES (?, 'status', 'queued', 'running', 'system', '', ?)""",
                (tid, "2026-10-19T10:00:00.000"))
    finally:
        con.close()

    body = env.get("/api/status/overview", headers=_bearer(owner)).json()
    d = body["duration"]
    # 两条都是 5 秒 —— 取错了起点（第一条）就会是好几万秒。
    assert d["max_seconds"] <= 60, \
        "重跑的任务用错了起点（取的是第一轮）：%r" % d


def test_duration_block_is_empty_when_nothing_finished(env, owner):
    """一个都没跑完时：**没有**成功率，也没有平均用时。

    给 0 会被面板显示成「成功率 0%」「平均耗时 0 秒」——那是在说
    「跑过 0 个、成功 0 个」，而事实是「一个都还没跑完」。两个前端都
    认 null（显示「—」），所以后端给 null 就行。
    """
    _make_task(owner["id"], "还在跑")
    body = env.get("/api/status/overview", headers=_bearer(owner)).json()
    assert body["duration"]["count"] == 0
    assert body["duration"]["avg_seconds"] is None, \
        "没有样本时给了 0，界面会显示成「平均耗时 0 秒」：%r" % body["duration"]
    assert body["success_rate"] is None, \
        "没有样本时给了 0，界面会显示成「成功率 0%%」：%r" % body


def test_the_throughput_only_counts_the_last_24_hours(env, owner, admin):
    """全局速度的口径是「最近 24 小时完成的任务数 / 24」。

    三天前完成的那个**不算**——它不代表现在的速度。三小时前完成的算。
    """
    from datetime import datetime, timedelta
    now = datetime.now()
    recent = (now - timedelta(hours=3)).isoformat()
    old = (now - timedelta(days=3)).isoformat()
    _mk_finished(owner["id"], "done", recent, recent)
    _mk_finished(owner["id"], "done", old, old)

    body = env.get("/api/status/overview", headers=_bearer(admin)).json()
    # 24 小时窗口里完成了 1 个 → 1/24 ≈ 0.04 个/小时
    assert body["throughput_per_hour"] == pytest.approx(1 / 24.0, abs=0.01), body


def test_success_rate_counts_done_over_terminal(env, owner):
    a = _make_task(owner["id"], "a")
    b = _make_task(owner["id"], "b")
    store.set_status(a["id"], "done", actor="system", message="ok")
    store.set_status(b["id"], "failed", actor="system", message="炸了")
    body = env.get("/api/status/overview", headers=_bearer(owner)).json()
    assert body["success_rate"] == 0.5
    assert body["tasks"]["done"] == 1
    assert body["tasks"]["failed"] == 1


def test_the_task_count_is_not_capped_by_the_sampling_limit(env, owner):
    """「任务总数」要真的是总数，不能被取数上限截断。

    面板上写「任务总数 5000」而库里其实有 5001 个——读的人不会怀疑这个
    数，它看起来就是个真数。计数走 SQL（精确），只有用时是样本。
    """
    n = status_api.STATS_LIMIT + 1
    con = db.connect()
    try:
        with con:
            con.executemany(
                """INSERT INTO tasks (title, brief, status, step, owner_id,
                                       priority, uploads_dir, output_dir,
                                       package_path, summary, error, session_id,
                                       created_at, updated_at)
                   VALUES ('t', '', 'queued', '', ?, 0, '', '', '', '', '', '',
                           '2026-10-20T10:00:00.000', '2026-10-20T10:00:00.000')""",
                [(int(owner["id"]),)] * n)
    finally:
        con.close()

    body = env.get("/api/status/overview", headers=_bearer(owner)).json()
    assert body["tasks"]["total"] == n, \
        "任务总数被取数上限截断了：%r != %r" % (body["tasks"]["total"], n)


# ------------------------------------------------------------- 单工作流

def test_workflow_status_only_counts_that_workflow(env, owner):
    wf = store.create_workflow(owner["id"], "批次A", "做 {x}")
    other_wf = store.create_workflow(owner["id"], "批次B", "做 {y}")
    assert wf["id"] != other_wf["id"]

    # 批次 A 的行：一行做完，一行失败
    r1 = store.create_task("A-1", "x", owner["id"], workflow_id=wf["id"],
                            workflow_row=0)
    r2 = store.create_task("A-2", "y", owner["id"], workflow_id=wf["id"],
                            workflow_row=1)
    store.set_status(r1["id"], "done", actor="system", message="ok")
    store.set_status(r2["id"], "failed", actor="system", message="炸了")
    # 批次 B 的一行（不该被算进 A）
    r3 = store.create_task("B-1", "z", owner["id"], workflow_id=other_wf["id"],
                            workflow_row=0)
    store.set_status(r3["id"], "done", actor="system", message="ok")

    r = env.get(f"/api/status/workflow/{wf['id']}", headers=_bearer(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow_id"] == wf["id"]
    assert body["rows"]["done"] == 1
    assert body["rows"]["failed"] == 1
    assert body["rows"]["done"] + body["rows"]["failed"] == 2, \
        "把别的工作流的行算进来了：%r" % body["rows"]


def test_a_skipped_row_is_not_counted_as_a_failed_row(env, owner):
    """跳过的行**不算失败**（与 server/app.py 的 _row_stats 同一判据）。

    跳过的行在库里是终态 failed（补偿任务建出来就是终态），而同一行在
    结果表里写的是「已跳过」。不排除的话就是「表说一套、面板说另一套」：
    跳 2 行 → 表里两行「已跳过」，面板标红「失败 2 行」——而这两件事
    该做的处理正好相反（不用管 vs 要查）。
    """
    wf = store.create_workflow(owner["id"], "批次", "做 {x}", total_rows=3)
    ok = store.create_task("A-1", "x", owner["id"], workflow_id=wf["id"],
                           workflow_row=0)
    store.set_status(ok["id"], "done", actor="system", message="ok")
    sk = store.create_task("A-2", "y", owner["id"], workflow_id=wf["id"],
                            workflow_row=1)
    store.set_status(sk["id"], "failed", actor="human", message="用户跳过")
    store.skip_workflow_row(wf["id"], 1, "序列是坏的")

    body = env.get(f"/api/status/workflow/{wf['id']}",
                   headers=_bearer(owner)).json()
    assert body["rows"]["skipped"] == 1, body["rows"]
    assert body["rows"]["failed"] == 0, \
        "跳过的行被算成失败了：%r" % body["rows"]
    assert body["rows"]["done"] == 1


def test_a_skipped_row_does_not_pull_the_average_down(env, owner):
    """跳过的行**不进用时统计**。

    这里特意造一条**真跑过、后来才被跳过**的行：只有这种行才会带着
    running 事件与 done_at 进用时统计。补偿任务（压根没跑就建出来是终态
    的那种）本来就进不去——它没有 running 事件，在 :func:`_durations` 里
    就被排除了，拿它来测这条围栏是**测不到东西的**（围栏必须真的能红）。
    """
    wf = store.create_workflow(owner["id"], "批次", "做 {x}", total_rows=2)
    _mk_finished(owner["id"], "done", "2026-10-20T10:00:00.000",
                 "2026-10-20T10:00:30.000", workflow_id=wf["id"], workflow_row=0)
    # 这一行真跑过（10:00:00 起跑，10:00:05 结束），之后才被用户跳过。
    sk = store.create_task("A-2", "y", owner["id"], workflow_id=wf["id"],
                           workflow_row=1)
    store.set_status(sk["id"], "running", actor="system", message="开始")
    store.set_status(sk["id"], "failed", actor="human", message="用户跳过")
    store.skip_workflow_row(wf["id"], 1, "序列是坏的")

    body = env.get(f"/api/status/workflow/{wf['id']}",
                   headers=_bearer(owner)).json()
    d = body["duration"]
    assert d["count"] == 1, "跳过的行被算进用时了：%r" % d
    assert d["avg_seconds"] == 30.0, \
        "平均单行用时被跳过行拉到 15 秒：%r" % d


def test_the_workflow_status_carries_the_field_the_panel_reads(env, owner):
    """**契约围栏**：前端工作流卡片读的是 ``duration.avg_row_seconds``。

    web/app.js 的工作流卡片那一句「每行平均 …」取的就是这个键。
    后端只给 ``avg_seconds`` 的话，界面那句话**永远不显示**——而且不报错，
    卡片少了一行字，谁也不会发现。
    """
    wf = store.create_workflow(owner["id"], "批次", "做 {x}", total_rows=1)
    _mk_finished(owner["id"], "done", "2026-10-20T10:00:00.000",
                 "2026-10-20T10:00:30.000", workflow_id=wf["id"], workflow_row=0)

    body = env.get(f"/api/status/workflow/{wf['id']}",
                   headers=_bearer(owner)).json()
    assert "avg_row_seconds" in body["duration"], \
        "前端读的是 duration.avg_row_seconds，后端没给：%r" % body["duration"]
    assert body["duration"]["avg_row_seconds"] == 30.0


def test_the_eta_is_unknown_until_a_row_has_finished(env, owner):
    """一行都还没跑完时，**预计剩余时间给 null**，不是 0。

    0 会被面板显示成「预计还要 0 秒」——而事实是「还不知道要多久」。
    """
    wf = store.create_workflow(owner["id"], "批次", "做 {x}", total_rows=5)
    store.create_task("A-1", "x", owner["id"], workflow_id=wf["id"], workflow_row=0)

    body = env.get(f"/api/status/workflow/{wf['id']}",
                   headers=_bearer(owner)).json()
    assert body["eta_seconds"] is None, \
        "没有样本时给了 0，界面会显示成「预计还要 0 秒」：%r" % body
    assert body["throughput_rows_per_hour"] is None
    assert body["rows_remaining"] == 5


def test_the_eta_is_computed_from_the_median_row_time(env, owner):
    """两行各跑 30 秒，还剩 2 行 → 预计 60 秒。"""
    wf = store.create_workflow(owner["id"], "批次", "做 {x}", total_rows=4)
    for row in (0, 1):
        _mk_finished(owner["id"], "done", "2026-10-20T10:00:00.000",
                     "2026-10-20T10:00:30.000", workflow_id=wf["id"],
                     workflow_row=row)

    body = env.get(f"/api/status/workflow/{wf['id']}",
                   headers=_bearer(owner)).json()
    assert body["rows_remaining"] == 2
    assert body["eta_seconds"] == 60, body
    assert body["throughput_rows_per_hour"] == 120.0, body


def test_workflow_status_of_someone_elses_workflow_is_404(env, owner, other):
    wf = store.create_workflow(other["id"], "别人的", "做 {x}")
    r = env.get(f"/api/status/workflow/{wf['id']}", headers=_bearer(owner))
    assert r.status_code == 404, r.text


def test_workflow_status_of_a_missing_workflow_is_404(env, owner):
    r = env.get("/api/status/workflow/999999", headers=_bearer(owner))
    assert r.status_code == 404, r.text


def test_an_admin_may_read_any_workflow_status(env, other, admin):
    wf = store.create_workflow(other["id"], "别人的", "做 {x}")
    r = env.get(f"/api/status/workflow/{wf['id']}", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    assert r.json()["workflow_id"] == wf["id"]


# ------------------------------------------------------------- 降级

def test_gpu_probe_failure_degrades_instead_of_raising(monkeypatch):
    """这台机器可能没有 GPU，也可能 nvidia-smi 卡住。不能因此 500。"""
    def _boom(*a, **kw):
        raise OSError("no nvidia-smi")

    monkeypatch.setattr(status_api.subprocess, "run", _boom)
    gpu = status_api._gpu()
    assert gpu["available"] is False
    # **不是 0**：没有 GPU 是「确定没有」，不是一个 0% 的占用率。
    assert gpu["util_percent"] is None, gpu


def test_gpu_probe_timeout_degrades_instead_of_raising(monkeypatch):
    """nvidia-smi 卡住是最常见的一种（驱动半死不活）。超时必须降级。"""
    def _hang(*a, **kw):
        raise subprocess.TimeoutExpired("nvidia-smi", 2.5)

    monkeypatch.setattr(status_api.subprocess, "run", _hang)
    gpu = status_api._gpu()
    assert gpu["available"] is False
    assert gpu["util_percent"] is None


def test_a_busy_gpu_wins_over_an_idle_one(monkeypatch):
    """多卡时利用率取**最忙的一张**。

    平台一张卡跑一个任务，机器忙不忙看的是最忙那张；只报第一张的话，
    第二张卡满载时面板上显示的还是 0%。
    """
    class _Proc:
        returncode = 0
        stderr = ""
        stdout = "3, 100, 24000\n91, 200, 24000\n"

    monkeypatch.setattr(status_api.subprocess, "run",
                        lambda *a, **kw: _Proc())
    gpu = status_api._gpu()
    assert gpu["available"] is True
    assert gpu["util_percent"] == 91, gpu
    # 显存是整台机器的账：求和。
    assert gpu["total_gb"] == 46.9, gpu
    assert gpu["count"] == 2, gpu


def test_the_gpu_endpoint_does_not_500_when_there_is_no_gpu(env, admin, monkeypatch):
    """端点层面也钉一条：没有 GPU 时 /api/status/system 照常 200。"""
    def _boom(*a, **kw):
        raise OSError("no nvidia-smi")

    monkeypatch.setattr(status_api.subprocess, "run", _boom)
    r = env.get("/api/status/system", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    assert r.json()["gpu"]["available"] is False


def test_the_cpu_probe_reports_none_when_it_cannot_read_the_machine(monkeypatch):
    """取不到就说取不到——**不是 0%**。

    0% 会被面板显示成「CPU 0%」，读的人以为机器闲着，其实是没读到。
    """
    def _boom(*a, **kw):
        raise OSError("读不到")

    if os.name == "nt":
        monkeypatch.setattr(status_api, "_windows_times", _boom)
    else:
        monkeypatch.setattr(status_api.os, "getloadavg", _boom)
    assert status_api._cpu_percent() is None


def test_the_memory_probe_reports_none_when_it_cannot_read_the_machine(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("读不到")

    if os.name == "nt":
        monkeypatch.setattr(status_api, "_windows_memory", _boom)
    else:
        monkeypatch.setattr("builtins.open", _boom)
    mem = status_api._memory()
    assert mem["percent"] is None, mem
    assert mem["used_gb"] is None, mem


def test_the_disk_probe_reports_none_when_the_volume_cannot_be_read(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("读不到")

    monkeypatch.setattr(status_api.shutil, "disk_usage", _boom)
    disk = status_api._disk()
    assert disk["free_gb"] is None, disk
    assert disk["total_gb"] is None, disk


def test_the_system_endpoint_survives_a_broken_probe(env, admin, monkeypatch):
    """任何一项探测坏了都不能让整个端点 500——四项是各自独立的。"""
    def _boom(*a, **kw):
        raise OSError("读不到")

    monkeypatch.setattr(status_api, "_cpu_percent", _boom)
    monkeypatch.setattr(status_api, "_memory", _boom)
    r = env.get("/api/status/system", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cpu"]["percent"] is None
    assert body["memory"]["percent"] is None
    # 磁盘与 GPU 没被动过，照常给数。
    assert "disk" in body and "gpu" in body


def test_cpu_probe_returns_a_sane_number():
    pct = status_api._cpu_percent()
    assert pct is None or 0.0 <= pct <= 100.0


# ------------------------------------------------------------- 挂载

def test_the_status_router_is_mounted_where_the_panels_look_for_it():
    """三个 app 都要挂同一个 router，路径一致。

    值班台的前端 apiBase() 自带 ``/api/admin`` 前缀，所以管理端**还要**
    有第二份 ``/api/admin/status/*``——少了它，值班台的状态面板 404，
    而页面其余部分照常工作，看着像「这台机器没数据」。
    """
    def _paths(app_obj):
        return {getattr(r, "path", "") for r in app_obj.routes}

    for app_obj in (svc.create_public_app(), svc.create_internal_app(),
                    admin_svc.create_admin_app()):
        paths = _paths(app_obj)
        assert "/api/status/overview" in paths, sorted(paths)
        assert "/api/status/system" in paths, sorted(paths)
        assert "/api/status/workflow/{workflow_id}" in paths, sorted(paths)

    admin_paths = _paths(admin_svc.create_admin_app())
    assert "/api/admin/status/overview" in admin_paths, sorted(admin_paths)
    assert "/api/admin/status/system" in admin_paths, sorted(admin_paths)
