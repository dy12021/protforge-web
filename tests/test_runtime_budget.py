# -*- coding: utf-8 -*-
"""时间阈值 + 用时复核 + 工具注册表（2026-10-25）。

这一层钉住的是**运行策略**，不是界面：

- 命令级超时：模型不申请时给默认额度、申请超过上限时夹住、按工具给差异化额度；
- 用时复核：跑得久了起一个独立上下文判「合不合理」，
  **合理就续跑、不合理才判失败、没跑成就不裁决**；
- 时间阈值全部**可配**（值班台能改），不写死在代码里。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import db
import dispatcher
import runtime_reviewer
import skills_index
import store

REPO = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO / "agent"

#: 值班台（admin/app_admin.py）在 admin/ 目录下。**本文件要自己加**：
#: conftest 只把 server/ 加上，单跑这个文件时 import app_admin 会 ModuleNotFoundError。
ADMIN_DIR = REPO / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))


def _uid(token: str) -> int:
    """令牌 -> 账号 id。"""
    import auth
    return int(auth.resolve_token(token)["id"])


@pytest.fixture()
def alice():
    """普通用户的令牌。**账号名与别的测试文件共用**（同一个临时库里）。"""
    import auth
    if auth.get_user("alice") is None:
        auth.create_user("alice", "pw-for-tests", role="user")
    return auth.issue_token(int(auth.get_user("alice")["id"]))


@pytest.fixture()
def root():
    """管理员的令牌。"""
    import auth
    if auth.get_user("root") is None:
        auth.create_user("root", "pw-for-tests", role="admin")
    return auth.issue_token(int(auth.get_user("root")["id"]))


# ---------------------------------------------------------------- 时间阈值


def test_cmd_timeout_defaults():
    assert store.get_cmd_timeout() == 600.0
    assert store.get_cmd_timeout_max() == 1800.0
    assert store.get_runtime_review_after() == 2700.0
    assert store.get_runtime_review_grant() == 2700.0


def test_thresholds_are_clamped():
    """填进去的数被夹在合理区间内——「超时 0 秒」会让每次审核立刻失败。"""
    assert store.set_cmd_timeout(1) == 30.0
    assert store.get_cmd_timeout() == 30.0
    assert store.set_cmd_timeout(10 ** 9) == 86400.0
    assert store.set_runtime_review_after(1) == 300.0
    assert store.set_runtime_review_grant(10 ** 9) == 86400.0


def test_broken_threshold_falls_back_to_default():
    """存坏了的值读出来必须是能用的数，不能让整台机器卡住。"""
    store.set_setting(store.CMD_TIMEOUT_KEY, "不是数字")
    assert store.get_cmd_timeout() == 600.0
    store.set_setting(store.CMD_TIMEOUT_KEY, "")


def test_thresholds_api_roundtrip(root):
    """阈值能从值班台改，也能从两个面板读出来。"""
    import app as svc
    import app_admin
    from fastapi.testclient import TestClient

    panel = TestClient(app_admin.create_admin_app())
    r = panel.put("/api/admin/review-settings", headers={"Authorization": "Bearer " + root},
                  json={"review_mode": "human", "cmd_timeout_s": 900,
                        "cmd_timeout_max_s": 3600, "runtime_review_after_s": 1800,
                        "runtime_review_grant_s": 1200})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cmd_timeout_s"] == 900, body
    assert body["cmd_timeout_max_s"] == 3600, body
    assert body["runtime_review_after_s"] == 1800, body
    assert body["runtime_review_grant_s"] == 1200, body

    r = panel.get("/api/admin/review-settings", headers={"Authorization": "Bearer " + root})
    assert r.json()["cmd_timeout_s"] == 900, r.text

    client = TestClient(svc.create_public_app())
    r = client.get("/api/review-settings",
                   headers={"Authorization": "Bearer " + root})
    assert r.status_code == 200, r.text
    assert r.json()["runtime_review_after_s"] == 1800, r.text


# ---------------------------------------------------------------- 工具注册表


def _skills_dir(tmp_path: Path) -> Path:
    """造一个临时技能仓。**不许碰真的技能仓**。"""
    root = tmp_path / "skills"
    d = root / "protforge-demo"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "demo.sh").write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    (d / "SKILL.md").write_text(
        "---\nname: protforge-demo\n"
        "description: \"Demo tool for tests\"\n---\n\n# demo\n",
        encoding="utf-8")
    (d / "tool.yaml").write_text(
        "id: demo\nversion: '1.2'\ndisplay_name: Demo 工具\n"
        "category: 结构生物学\nbatch: true\ntimeout_s: 900\n"
        "params:\n"
        "  - name: pdb_path\n    type: path\n    required: true\n    label: 输入结构\n"
        "  - name: chains\n    type: string\n    default: 'A'\n",
        encoding="utf-8")
    # 说明书型技能：没有入口脚本，不该被当成「可直接调用的工具」
    d2 = root / "protforge-router"
    d2.mkdir(parents=True)
    (d2 / "SKILL.md").write_text(
        "---\nname: protforge-router\ndescription: \"Router only\"\n---\n\n# router\n",
        encoding="utf-8")
    return root


def test_tool_manifest_is_parsed(tmp_path):
    tools = skills_index.list_tools(_skills_dir(tmp_path))
    by_id = {t["id"]: t for t in tools}
    assert "demo" in by_id
    d = by_id["demo"]
    assert d["version"] == "1.2"
    assert d["display_name"] == "Demo 工具"
    assert d["category"] == "结构生物学"
    assert d["batch"] is True
    assert d["directly_callable"] is True
    assert d["timeout_s"] == 900, "tool.yaml 的 timeout_s 要能覆盖代码里的档位表"
    names = [p["name"] for p in d["params"]]
    assert names == ["pdb_path", "chains"]
    assert d["params"][0]["required"] is True
    assert d["params"][1]["default"] == "A"


def test_router_skill_is_not_a_directly_callable_tool(tmp_path):
    """说明书型技能**不能上架成能点的工具**——点下去没有入口可跑。"""
    tools = {t["id"]: t for t in skills_index.list_tools(_skills_dir(tmp_path))}
    r = tools["router"]
    assert r["directly_callable"] is False
    assert r["batch"] is False


def test_tool_budgets_include_skill_bare_name(tmp_path):
    """预算表的键既有入口文件名，也有技能裸名（WSL 直调型只有名字能认出来）。"""
    b = skills_index.tool_budgets(_skills_dir(tmp_path))
    assert b.get("demo.sh") == 900
    assert b.get("demo") == 900
    assert b.get("router") == skills_index.timeout_for("protforge-router")


def test_broken_tool_manifest_is_not_silently_dropped(tmp_path):
    """契约文件坏掉 → 报错，不是当没有这个工具。

    静默丢掉 = 「这个工具存在但参数不校验」，手动模式会把参数名写错的事放过去。
    """
    root = _skills_dir(tmp_path)
    (root / "protforge-demo" / "tool.yaml").write_text(
        "id: demo\nparams:\n  - name: x\n    type: 怪类型\n", encoding="utf-8")
    with pytest.raises(skills_index.SkillIndexError):
        skills_index.scan(root)


# ---------------------------------------------------------------- 用时复核


def _verdict(task_id: int, verdict: str, reason: str = "因为") -> None:
    p = runtime_reviewer.verdict_path(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"verdict": verdict, "reason": reason},
                             ensure_ascii=False), encoding="utf-8")


def test_runtime_verdict_reading(alice):
    """认得出的词都收；**认不出 / 没跑成 = error，不猜**。

    两种裁决的后果完全相反（续跑 vs 判失败），猜错的代价比不裁决大得多。
    """
    task = store.create_task("t", "b", owner_id=_uid(alice), actor="human")
    tid = int(task["id"])
    runtime_reviewer.review_dir(tid).mkdir(parents=True, exist_ok=True)

    _verdict(tid, "reasonable", "文件在长")
    assert runtime_reviewer._read_verdict(tid) == ("reasonable", "文件在长")

    _verdict(tid, "unreasonable", "原地打转")
    assert runtime_reviewer._read_verdict(tid) == ("unreasonable", "原地打转")

    _verdict(tid, "不合理", "中文也认")
    assert runtime_reviewer._read_verdict(tid)[0] == "unreasonable"

    # **判不出来就是判不出来**——平台据此不裁决，让任务继续跑
    _verdict(tid, "unclear")
    assert runtime_reviewer._read_verdict(tid)[0] == "error"

    _verdict(tid, "瞎写的")
    assert runtime_reviewer._read_verdict(tid)[0] == "error"


def test_runtime_review_collects_evidence(alice):
    """证据里必须有**硬指标**：文件数、总字节、最新文件多久没动。"""
    task = store.create_task("t", "b", owner_id=_uid(alice), actor="human")
    tid = int(task["id"])
    ev = runtime_reviewer.collect_evidence(tid, 1234.0)
    assert ev["elapsed_s"] == 1234.0
    assert "files" in ev["work_dir"]
    assert "bytes" in ev["work_dir"]
    assert "commands" in ev
    assert "log_tail" in ev


def test_dispatcher_grants_time_when_reasonable(alice, monkeypatch):
    """判「合理」→ **续跑**：任务不被杀，事件里写明理由。"""
    task = store.create_task("t", "b", owner_id=_uid(alice), actor="human")
    tid = int(task["id"])
    d = dispatcher.Dispatcher(db, max_concurrency=1, node_exe=sys.executable)

    monkeypatch.setattr(runtime_reviewer, "review_runtime",
                        lambda tid, **kw: {"verdict": "reasonable",
                                            "reason": "文件在长"})
    d._runtime_review_worker(tid, 3000.0)

    assert store.get_task(tid, is_admin=True)["status"] == "queued", \
        "判合理绝不能把任务打死"
    msgs = [e["message"] for e in store.list_events(tid)]
    assert any("判定合理" in m for m in msgs), msgs


def test_dispatcher_fails_when_unreasonable(alice, monkeypatch):
    """判「不合理」→ **判失败**，并写明理由（不是「用户中止」）。"""
    task = store.create_task("t", "b", owner_id=_uid(alice), actor="human")
    tid = int(task["id"])
    store.set_status(tid, "running", actor="system", message="开始算")
    d = dispatcher.Dispatcher(db, max_concurrency=1, node_exe=sys.executable)

    monkeypatch.setattr(runtime_reviewer, "review_runtime",
                        lambda tid, **kw: {"verdict": "unreasonable",
                                            "reason": "同一脚本反复超时"})
    d._runtime_review_worker(tid, 3000.0)

    t = store.get_task(tid, is_admin=True)
    assert t["status"] == "failed", t
    assert "用时复核判定不合理" in (t["error"] or ""), t["error"]
    assert "同一脚本反复超时" in (t["error"] or ""), t["error"]


def test_dispatcher_does_not_judge_when_review_fails(alice, monkeypatch):
    """**复核没跑成 ≠ 用时不合理**：不裁决，任务继续跑。"""
    task = store.create_task("t", "b", owner_id=_uid(alice), actor="human")
    tid = int(task["id"])
    store.set_status(tid, "running", actor="system", message="开始算")
    d = dispatcher.Dispatcher(db, max_concurrency=1, node_exe=sys.executable)

    monkeypatch.setattr(runtime_reviewer, "review_runtime",
                        lambda tid, **kw: {"verdict": "error",
                                            "reason": "复核进程没写出结论文件"})
    d._runtime_review_worker(tid, 3000.0)

    assert store.get_task(tid, is_admin=True)["status"] == "running", \
        "复核挂了就把任务打死，等于把裁判失职当成任务有问题"


# ---------------------------------------------------------------- 守卫（node）


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_guard_timeout_node_tests_pass():
    """agent 侧的超时逻辑（默认值 / 夹住 / 工具差异化）由 node 测试钉住。

    **必须跑** —— 那段逻辑在 JS 里，pytest 覆盖不到；而它恰好是
    「一条命令能跑多久」的唯一执行点。
    """
    # **必须指定 encoding**：Windows 上默认按 cp936 解码，
    # node 的 UTF-8 输出里有中文会直接抛 UnicodeDecodeError（曾经踩过）。
    r = subprocess.run(["node", "--test", "guards.timeout.test.mjs"],
                       cwd=str(AGENT_DIR), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
