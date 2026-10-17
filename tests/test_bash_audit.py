"""bash 全量审计的接线测试（规格 §9.2 / §14）。

这条链路曾经**整条都不存在**，而每一段单测都是绿的：

- ``store.audit.log`` 本身有测试（``test_store.py``），能写库；
- ``createGuardedBashOperations`` 本身有测试（``guards.test.mjs``），判定正确；
- 管理端审计表格本身有测试（``test_admin_static.py``），列名齐全；
- janitor 有测试保证审计行 30 天会被清掉。

**没有一处**保证「agent 跑过的命令真的会进 audit_log」。守卫判定完就执行，
不留痕；agent 侧没有上报；服务端连收审计的端点都没有。于是：

- ``audit_log`` 表恒为空（真机实测：8 个任务、跑完一轮，0 行）；
- 管理端审计页签恒显示「没有匹配的审计记录」，而它**不报任何错**；
- 导出按钮只会说「还没有审计记录可导出」；
- 「全量审计」这条安全承诺（L1 命令管控之上的事后追溯层）实际不存在。

这组测试按「谁调用谁」钉：端点真写库、agent 令牌才写得进去、**管理端真读得到**。
最后一条尤其关键——只测端点能写库，管不了「面板读的是不是另一个数据源」。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import store
import app as svc

# admin/ 不在 conftest 的 sys.path 里（conftest 冻结，只加了 server/），
# 而「审计能不能被面板查到」这一层必须真走到面板代码才算测到。
ADMIN_DIR = Path(__file__).resolve().parent.parent / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

import app_admin      # noqa: E402

PWD = "password-123"


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def owner():
    return auth.create_user("owner1", PWD, role="user")


@pytest.fixture
def admin():
    return auth.create_user("boss", PWD, role="admin")


@pytest.fixture
def task_id(owner):
    return store.create_task(title="分析 1ubq", brief="算一下表面暴露",
                             owner_id=owner["id"])["id"]


@pytest.fixture
def env():
    internal = svc.create_internal_app()
    panel = app_admin.create_admin_app()
    with TestClient(internal) as ci, TestClient(panel) as cp:
        yield ci, cp


def _bearer(user):
    return {"Authorization": f"Bearer {auth.issue_token(user['id'])}"}


def _report(internal, tid, **body):
    token = svc.issue_agent_token(tid)
    return internal.post(f"/api/internal/agent/{tid}/audit", json=body,
                         headers={"X-Agent-Token": token})


# ---------------------------------------------------------------- 端点：真写库

def test_reported_command_lands_in_audit_log(env, task_id):
    ci, _ = env
    r = _report(ci, task_id, kind="bash", command="fold.py job.json",
                exit_code=0, elapsed_ms=8123)
    assert r.status_code == 200, r.text

    rows = store.audit.list(task_id)
    assert len(rows) == 1, f"审计表里没有这一行，实际 {len(rows)} 行"
    row = rows[0]
    assert row["command"] == "fold.py job.json", "命令原文必须一字不改地存下来"
    assert row["exit_code"] == 0
    assert row["elapsed_ms"] == 8123
    assert row["kind"] == "bash"
    assert row["actor"] == "agent"
    assert row["task_id"] == task_id


def test_denied_command_keeps_the_raw_text_and_the_reason(env, task_id):
    """被守卫拦下的命令：第一行原文不变，拒绝理由跟在后面。

    审计表的列是规格定死的（没有 reason 列），而「为什么被拦」是这段证据的
    一半。改写原文又会毁掉「里面有没有 rm」的判断，所以按行拼接。
    """
    ci, _ = env
    CMD = 'bash -c "rm -rf /"'
    r = _report(ci, task_id, kind="bash_denied",
                command=f"{CMD}\n# 被守卫拒绝：删除根目录", exit_code=None)
    assert r.status_code == 200, r.text

    row = store.audit.list(task_id)[0]
    assert row["command"].split("\n")[0] == CMD, "命令原文必须原样留在第一行"
    assert "被守卫拒绝" in row["command"], "拒绝理由也要留下"
    assert row["exit_code"] is None, "从没执行过就没有退出码"


def test_audit_rows_never_reach_the_timeline(env, task_id):
    """§9.2 的物理隔离：一次 rfdiffusion 上万条命令，混进 events 会刷爆
    用户时间线并拖慢 SSE 的游标扫描。"""
    ci, _ = env
    store.add_event(task_id, "created", "human", "提交了")
    for i in range(5):
        _report(ci, task_id, kind="bash", command=f"step-{i}.py", exit_code=0)
    ci.post(f"/api/internal/agent/{task_id}/step", json={"text": "在跑"},
            headers={"X-Agent-Token": svc.issue_agent_token(task_id)})

    kinds = [e["kind"] for e in store.list_events(task_id)]
    assert kinds.count("created") == 1
    assert "bash" not in kinds, f"审计混进了时间线：{kinds}"
    assert len(store.audit.list(task_id)) == 5


def test_every_row_gets_its_own_id_for_cursor_polling(env, task_id):
    """管理端靠 `id > after_id` 增量拉取，id 必须逐条递增且互不相同。"""
    ci, _ = env
    for i in range(3):
        _report(ci, task_id, kind="bash", command=f"c{i}", exit_code=0)

    ids = [r["id"] for r in store.audit.list(task_id)]
    assert len(set(ids)) == 3, f"审计 id 重复，增量拉取会漏行：{ids}"
    assert ids == sorted(ids, reverse=True), "list 应按 id 倒序返回"


# ---------------------------------------------------------------- 令牌闸

def test_audit_requires_the_agent_token(env, task_id):
    ci, _ = env
    r = ci.post(f"/api/internal/agent/{task_id}/audit",
                json={"kind": "bash", "command": "ls"})
    assert r.status_code == 401, r.text


def test_agent_token_cannot_audit_another_task(env, owner, task_id):
    """作用域保证：任务 A 的令牌打任务 B 必须 403，不能往别的任务里塞证据。"""
    ci, _ = env
    other = store.create_task(title="别的", brief="别的", owner_id=owner["id"])["id"]
    token = svc.issue_agent_token(task_id)
    r = ci.post(f"/api/internal/agent/{other}/audit",
                json={"kind": "bash", "command": "rm -rf /", "exit_code": 0},
                headers={"X-Agent-Token": token})
    assert r.status_code == 403, r.text
    assert store.audit.list(other) == [], "被拒的越权写入不该留下任何痕迹"


def test_agent_cannot_forge_admin_rpc_rows(env, task_id):
    """kind 收窄到 agent 自己产生的三种。

    放开 `admin_rpc` 的话，agent 可以往审计表里写「管理员干预」，
    把追责记录伪造成管理员干的——这正是这张表存在的意义所在。
    """
    ci, _ = env
    r = _report(ci, task_id, kind="admin_rpc", command="steer 换个算法")
    assert r.status_code == 422, f"agent 能冒充管理员干预：{r.text}"
    assert store.audit.list(task_id) == []


def test_audit_does_not_exist_on_the_public_app(owner):
    """内部通道绝不能挂在公网那个 app 上。"""
    with TestClient(svc.create_public_app()) as c:
        r = c.post(f"/api/internal/agent/1/audit", json={"kind": "bash", "command": "ls"},
                   headers={"X-Agent-Token": "whatever"})
    assert r.status_code == 404, r.text


def test_elapsed_ms_from_the_agent_is_bounded(env, task_id):
    """elapsed_ms 来自 agent 进程，是不可信输入：10^18 能撑爆任何按时间
    聚合的查询。"""
    ci, _ = env
    r = _report(ci, task_id, kind="bash", command="sleep 1",
                elapsed_ms=10 ** 18)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- 管理端真读得到

def test_the_admin_panel_can_read_those_rows(env, task_id, admin):
    """闭环：agent 上报 → audit_log → 管理端审计页签拿到的就是这一行。

    这一条是整组里唯一能证伪「面板读的是另一个数据源」的。
    只测端点写得进库，管不到面板。
    """
    ci, cp = env
    _report(ci, task_id, kind="bash", command="fold.py job.json",
            exit_code=0, elapsed_ms=8123)

    r = cp.get("/api/admin/audit?limit=100", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    rows = r.json()["audit"]
    assert any(x["command"] == "fold.py job.json" for x in rows), (
        f"管理端看不到 agent 上报的审计行，它返回的是：{rows}")


def test_the_panel_task_scoped_audit_sees_them_too(env, task_id, admin):
    """按任务过滤的那条视图也是同一个数据源。"""
    ci, cp = env
    _report(ci, task_id, kind="bash_denied",
            command='rm -rf /\n# 被守卫拒绝：删除根目录')

    r = cp.get(f"/api/admin/tasks/{task_id}/audit", headers=_bearer(admin))
    assert r.status_code == 200, r.text
    rows = r.json()["audit"]
    assert [x["kind"] for x in rows] == ["bash_denied"], rows


def test_a_normal_user_cannot_read_the_audit_log(env, owner):
    cp = env[1]
    r = cp.get("/api/admin/audit?limit=100", headers=_bearer(owner))
    assert r.status_code in (401, 403), r.text


# ---------------------------------------------------------------- 接线本身

# 上面所有测试都是「调端点」，它们证明不了**生产路径真的会去调这个端点**。
# 守卫测试注入的是假的 onAudit，所以只要 run.mjs 里那行接线被删掉，
# agent 就再也不会上报，而上面 12 条与 9 条 Node 测试**全部照绿**。
# 这就是本项目反复出现的那一类缺陷：模块都对，只是没人调用它们。
# 因此下面三条直接钉源码里的接线。

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"


def _agent_src(name: str) -> str:
    return (AGENT_DIR / name).read_text(encoding="utf-8")


def test_run_mjs_wires_the_audit_hook_into_the_guard():
    """agent 入口必须把回传层的 audit 挂进守卫选项。

    删掉这行，`opts.onAudit` 变成 undefined，守卫照常工作、
    照常拦截，但一条命令都不会上报——静默到没有任何测试会红。
    """
    src = _agent_src("run.mjs")
    assert "onAudit" in src, "run.mjs 没有把审计回传接进命令守卫"
    guard_opts_block = re.search(r"const guardOpts\s*=\s*\{(.*?)\n  \};", src, re.S)
    assert guard_opts_block, "找不到 guardOpts 定义"
    block = guard_opts_block.group(1)
    assert "onAudit" in block, "onAudit 没写在 guardOpts 里，传给了别的地方"
    # 接的必须是回传层的 audit，而不是随手一个空函数
    assert re.search(r"onAudit:[^,\n]*\.audit\(", block), (
        "onAudit 没有调用 reporter.audit，写的是什么："
        + block.strip().splitlines()[0])


def test_tools_mjs_reports_to_the_audit_kind():
    """回传层必须有 audit 方法，且打的是 'audit' 这个 kind。"""
    src = _agent_src("tools.mjs")
    m = re.search(r"async audit\([^)]*\)\s*\{(.*?)\n    \},", src, re.S)
    assert m, "tools.mjs 里没有 reporter.audit 方法"
    assert "'audit'" in m.group(1), "reporter.audit 打的不是 audit 端点"


def test_every_kind_the_agent_posts_lands_on_a_real_route(env, task_id):
    """跨边界契约：agent 拼出来的每个 URL 都必须命中服务端的一条真路由。

    两边各写各的 URL 时，agent 会一直收到 404——而它按设计**吞掉回传失败**
    （回传失败不该让任务失败），于是症状是「审计表永远是空的」，
    agent 日志里什么都看不出来。

    这里不靠字符串模板硬拼：从 tools.mjs 里把它实际会 POST 的 kind 全部抠出来
    （拼错一个 kind 就是一条打不通的 URL），逐个对着**真起着的内部 app** 打，
    用「不是 404」判定路由存在。服务端那边用的是 FastAPI 的真路由表，
    不是再解析一遍源码——否则两边用同一种错误的方式理解同一个字符串，
    测试就只会确认「两边犯的是同一个错」。
    """
    src = _agent_src("tools.mjs")

    m = re.search(r"const url = \(kind\) => `\$\{base\}(/api/internal/agent/[^`]*)`", src)
    assert m, "tools.mjs 里 url() 的模板变了，需同步本测试"
    template = m.group(1)

    # 它实际会 POST 的 kind：post('step',…) / post('finish',…) / post('audit',…)
    kinds = sorted(set(re.findall(r"\bpost\('([a-z_]+)'", src)))
    assert "audit" in kinds, f"tools.mjs 里没有 post('audit', …)，实际 kinds={kinds}"

    ci, _ = env
    token = svc.issue_agent_token(task_id)
    for kind in kinds:
        path = (template
                .replace("${encodeURIComponent(cfg.taskId)}", str(task_id))
                .replace("${kind}", kind))
        r = ci.post(path, json={}, headers={"X-Agent-Token": token})
        assert r.status_code != 404, (
            f"agent 会 POST 的 {kind!r} 拼出了 {path}，"
            f"服务端没有这条路由（404）。内部 app 的真路由："
            f"{sorted(p for p in _internal_paths(env) if 'internal' in p)}")
        assert r.status_code != 405, f"{kind} 打过去是 405：路由在，但方法对不上"


def _internal_paths(env) -> set[str]:
    """内部 app 真实的路由表（给断言消息用，省得猜）。"""
    return {r.path for r in env[0].app.routes if hasattr(r, "path")}
