# -*- coding: utf-8 -*-
"""手动模式：工具市场 API 与「平台直跑脚本」的执行路径（2026-10-25）。

钉住的约定：

- 工具清单/详情来自**技能目录**，不是一个写死的列表；
- 参数**按契约校验**：契约外的名字拒、类型不对拒、必填缺了拒；
- 手动任务落库成 ``kind='tool'``，调度器起的是 **run_tool.py**（不是 agent）；
- 拼出来的 argv **顺序 = 契约顺序**（位置参数只能按顺序传）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import db
import dispatcher
import skills_index
import store
import tool_runner

REPO = Path(__file__).resolve().parent.parent
ADMIN_DIR = REPO / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
# args: <in> <job> <n>
IN="${1:?}"; JOB="${2:?}"; N="${3:?}"
echo "tool ran with $IN $JOB $N"
printf 'result for %s\\n' "$IN" > "out_$JOB.txt"
echo "PROTFORGE_STATUS: OK"
"""


@pytest.fixture()
def fake_skills(tmp_path, monkeypatch):
    """一个**临时**技能仓，里面有一个可执行的工具。"""
    root = tmp_path / "skills"
    d = root / "protforge-demo"
    (d / "scripts").mkdir(parents=True)
    runner = d / "scripts" / "demo.sh"
    runner.write_text(SCRIPT, encoding="utf-8", newline="\n")
    (d / "SKILL.md").write_text(
        "---\nname: protforge-demo\n"
        "description: \"Demo tool\"\n---\n\n# demo\n", encoding="utf-8")
    (d / "tool.yaml").write_text(
        "id: demo\nversion: \"1.0\"\ndisplay_name: Demo 工具\n"
        "category: 结构分析\nbatch: true\ntimeout_s: 120\n"
        "params:\n"
        "  - name: input\n    type: path\n    required: true\n"
        "  - name: job_name\n    type: string\n    default: demo\n"
        "  - name: n\n    type: int\n    default: \"3\"\n",
        encoding="utf-8")
    monkeypatch.setenv("PROTFORGE_WEB_SKILLS_DIR", str(root))
    return root


@pytest.fixture()
def client():
    import app as svc
    return TestClient(svc.create_public_app())


@pytest.fixture()
def alice():
    import auth
    if auth.get_user("alice") is None:
        auth.create_user("alice", "pw-for-tests", role="user")
    return auth.issue_token(int(auth.get_user("alice")["id"]))


def _headers(token):
    return {"Authorization": "Bearer " + token}


# ---------------------------------------------------------------- API


def test_tool_list_comes_from_the_skills_dir(client, alice, fake_skills):
    r = client.get("/api/tools", headers=_headers(alice))
    assert r.status_code == 200, r.text
    tools = r.json()["tools"]
    assert any(t["id"] == "demo" for t in tools), tools
    demo = [t for t in tools if t["id"] == "demo"][0]
    assert demo["display_name"] == "Demo 工具"
    assert demo["category"] == "结构分析"
    assert demo["directly_callable"] is True
    assert [p["name"] for p in demo["params"]] == ["input", "job_name", "n"]


def test_unknown_tool_is_404(client, alice, fake_skills):
    r = client.get("/api/tools/nope", headers=_headers(alice))
    assert r.status_code == 404, r.text


def test_run_rejects_unknown_param(client, alice, fake_skills):
    """契约外的参数名**一律拒**——写错了会静默用默认值。"""
    r = client.post("/api/tools/demo/run", headers=_headers(alice),
                    json={"params": {"input": "a.pdb", "jbo": "x"}})
    assert r.status_code == 422, r.text
    assert "jbo" in r.text


def test_run_rejects_missing_required_param(client, alice, fake_skills):
    r = client.post("/api/tools/demo/run", headers=_headers(alice),
                    json={"params": {}})
    assert r.status_code == 422, r.text
    assert "input" in r.text


def test_run_rejects_wrong_type(client, alice, fake_skills):
    r = client.post("/api/tools/demo/run", headers=_headers(alice),
                    json={"params": {"input": "a.pdb", "n": "不是数字"}})
    assert r.status_code == 422, r.text


def test_run_creates_a_tool_task(client, alice, fake_skills):
    r = client.post("/api/tools/demo/run", headers=_headers(alice),
                    json={"params": {"input": "a.pdb"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] is True
    task = body["task"]
    assert task["kind"] == "tool", task
    assert task["tool_id"] == "demo"
    assert json.loads(task["tool_params"]) == {"input": "a.pdb", "n": 3,
                                                "job_name": "t%d_demo" % task["id"]}, \
        "产物的落点必须带 t<任务号>_ 前缀，否则打包会拒"


def test_run_batch_creates_one_task_per_input(client, alice, fake_skills):
    r = client.post("/api/tools/demo/run", headers=_headers(alice),
                    json={"params": {},
                          "inputs": [{"input": "a.pdb"}, {"input": "b.pdb"}]})
    assert r.status_code == 200, r.text
    tasks = r.json()["tasks"]
    assert len(tasks) == 2, tasks
    assert all(t["kind"] == "tool" for t in tasks)


def test_router_skill_cannot_be_run_directly(client, alice, fake_skills):
    """说明书型技能**不能直接调用**——它没有入口脚本。"""
    d = fake_skills / "protforge-router"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: protforge-router\ndescription: \"Router\"\n---\n\n# r\n",
        encoding="utf-8")
    (d / "tool.yaml").write_text("id: router\nversion: \"1.0\"\n", encoding="utf-8")
    r = client.post("/api/tools/router/run", headers=_headers(alice),
                    json={"params": {}})
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- 执行路径


def test_argv_order_follows_the_contract(fake_skills):
    """位置参数只能按顺序传——顺序错了整个调用就错了位。"""
    tool = skills_index.get_tool("demo", str(fake_skills))
    argv = tool_runner.build_argv(tool, {"input": "a.pdb", "job_name": "j", "n": 7})
    # Windows 上可能是 bash.EXE（system32 里的 WSL bash），
    # 也可能是 Git 的 bash.exe——**比大小写**，别写死。
    assert argv[0].lower().endswith("bash.exe") or argv[0].lower().endswith("bash"), argv
    assert argv[1].endswith("demo.sh"), argv
    assert argv[2:] == ["a.pdb", "j", "7"], argv


def test_dispatcher_spawns_the_tool_runner_not_the_agent(fake_skills, alice):
    """工具任务起的是 **run_tool.py**，不是 node run.mjs。"""
    task = store.create_task("手动跑 demo", "", owner_id=_uid(alice), actor="human",
                             kind="tool", tool_id="demo",
                             tool_params={"input": "a.pdb"})
    tid = int(task["id"])
    d = dispatcher.Dispatcher(db, max_concurrency=1, node_exe=sys.executable)
    env = {}
    argv = d._tool_task_argv(store.get_task(tid, is_admin=True),
                             Path(db.data_root() / "work" / str(tid)), env)
    assert argv[0] == sys.executable
    assert argv[1].endswith("run_tool.py"), argv
    spec = json.loads(env["PROTFORGE_WEB_TOOL_SPEC"])
    assert spec["tool_id"] == "demo"
    assert spec["argv"][2:] == ["a.pdb", "t%d_demo" % tid, "3"], spec
    assert spec["timeout"] == 120


def _uid(token):
    import auth
    return int(auth.resolve_token(token)["id"])
