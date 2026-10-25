# -*- coding: utf-8 -*-
"""工具详情文档 + agent 辅助填参数 + 用时复核登记（2026-10-25）。

钉住的约定：

- **文档的唯一来源是技能仓里那份 SKILL.md**。平台不另写一份——写两份就会漂移，
  而漂移出来的文档比没文档更坏。
- **agent 只填参数，没有执行权**。它填歪的（契约外的名字、类型不对）一律丢掉；
  填不出的必填项摆出来让人补，**不编**。
- **用时复核必须真的登记得起跑时刻**。少了登记，elapsed 恒为 0，复核永远
  判不出「跑久了」——机制在、但等于没有。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import db
import dispatcher
import llm
import skills_index
import store
import tool_runner

REPO = Path(__file__).resolve().parent.parent
ADMIN_DIR = REPO / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
echo "tool ran with ${1:-} ${2:-} ${3:-}"
echo "PROTFORGE_STATUS: OK"
"""

SKILL_MD = (
    "---\n"
    "name: protforge-demo\n"
    "description: \"Demo 工具的说明：这是一个用来测接口的假工具。\"\n"
    "---\n"
    "\n"
    "# demo\n"
    "\n"
    "## 用法\n"
    "\n"
    "把 pdb 路径丢进来就行。\n"
)

TOOL_YAML = (
    "id: demo\n"
    "version: \"1.0\"\n"
    "display_name: Demo 工具\n"
    "category: 结构分析\n"
    "batch: true\n"
    "timeout_s: 120\n"
    "params:\n"
    "  - name: input\n"
    "    type: path\n"
    "    required: true\n"
    "  - name: job_name\n"
    "    type: string\n"
    "    default: demo\n"
    "  - name: n\n"
    "    type: int\n"
    "    default: \"3\"\n"
)


@pytest.fixture()
def fake_skills(tmp_path, monkeypatch):
    """一个**临时**技能仓：两个可执行工具 + 一个没有 tool.yaml 的技能。"""
    root = tmp_path / "skills"

    d = root / "protforge-demo"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "demo.sh").write_text(SCRIPT, encoding="utf-8", newline="\n")
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (d / "tool.yaml").write_text(TOOL_YAML, encoding="utf-8")

    # 没有 tool.yaml 的技能：**不是工具**，不该出现在工具市场里。
    plain = root / "protforge-plain"
    (plain / "scripts").mkdir(parents=True)
    (plain / "scripts" / "plain.sh").write_text(SCRIPT, encoding="utf-8", newline="\n")
    (plain / "SKILL.md").write_text(
        "---\nname: protforge-plain\ndescription: \"没有 tool.yaml\"\n---\n\n# p\n",
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


def _uid(token):
    import auth
    return int(auth.resolve_token(token)["id"])


# ---------------------------------------------------------------- 详细文档


def test_doc_is_the_skill_md_body(client, alice, fake_skills):
    """文档 = 技能仓里那份 SKILL.md。**平台不另写一份。**"""
    r = client.get("/api/tools/demo/doc", headers=_headers(alice))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "demo"
    assert body["skill"] == "protforge-demo"
    assert "把 pdb 路径丢进来就行" in body["doc"]
    assert body["description"], "frontmatter 的 description 要单独拿出来摆"
    assert "Demo 工具的说明" in body["description"]


def test_doc_does_not_repeat_the_frontmatter(client, alice, fake_skills):
    """frontmatter 已经在 description 里了，正文里不该再出现一遍。"""
    r = client.get("/api/tools/demo/doc", headers=_headers(alice))
    body = r.json()
    assert "description:" not in body["doc"]
    assert "name: protforge-demo" not in body["doc"]


def test_doc_404_for_unknown_tool(client, alice, fake_skills):
    r = client.get("/api/tools/nope/doc", headers=_headers(alice))
    assert r.status_code == 404, r.text


def test_doc_404_when_the_skill_has_no_skill_md(client, alice, fake_skills):
    """有 tool.yaml 但没有 SKILL.md → 没有文档，如实 404，不编一份出来。"""
    d = fake_skills / "protforge-nodoc"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "x.sh").write_text(SCRIPT, encoding="utf-8", newline="\n")
    (d / "tool.yaml").write_text("id: nodoc\nversion: \"1.0\"\n", encoding="utf-8")
    r = client.get("/api/tools/nodoc/doc", headers=_headers(alice))
    assert r.status_code == 404, r.text


def test_market_only_lists_skills_with_a_manifest(client, alice, fake_skills):
    """没有 tool.yaml 的不是「工具」——它连参数契约都没有。"""
    r = client.get("/api/tools", headers=_headers(alice))
    ids = [t["id"] for t in r.json()["tools"]]
    assert "demo" in ids
    assert "plain" not in ids


def test_kind_defaults_to_tool_and_rejects_junk(fake_skills):
    assert skills_index.get_tool("demo", str(fake_skills))["kind"] == "tool"
    bad = fake_skills / "protforge-badkind"
    (bad / "scripts").mkdir(parents=True)
    (bad / "scripts" / "x.sh").write_text(SCRIPT, encoding="utf-8", newline="\n")
    (bad / "SKILL.md").write_text(
        "---\nname: protforge-badkind\ndescription: \"x\"\n---\n\n# x\n", encoding="utf-8")
    (bad / "tool.yaml").write_text(
        "id: badkind\nversion: \"1.0\"\nkind: nonsense\n", encoding="utf-8")
    with pytest.raises(skills_index.SkillIndexError):
        skills_index.scan(str(fake_skills))


# ---------------------------------------------------------------- agent 填参数


def _fake_model(monkeypatch, reply):
    """把模型换成一个固定回话的桩。**记下它收到了什么**，便于断言。"""
    seen = {}

    def chat(prompt, *, system="", max_completion_tokens=None,
             reasoning_effort=None, timeout=None):
        seen["prompt"] = prompt
        seen["system"] = system
        seen["reasoning_effort"] = reasoning_effort
        return reply

    monkeypatch.setattr(llm, "chat_completion", chat)
    return seen


def test_fill_params_fills_the_contract(client, alice, fake_skills, monkeypatch):
    seen = _fake_model(monkeypatch, json.dumps({
        "input": "1ubq.pdb", "n": 8, "_notes": "n 取 8 是猜的",
    }))
    r = client.post("/api/tools/demo/fill-params", headers=_headers(alice),
                    json={"requirement": "算一下 1ubq 的表面积"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["params"]["input"] == "1ubq.pdb"
    assert body["params"]["n"] == 8, "int 字段要转成 int，不能是字符串"
    assert "8 是猜的" in body["notes"]
    assert body["missing"] == []
    assert seen["reasoning_effort"] == "low", "抽取/映射类的活不要让它推理"
    assert "1ubq" in seen["prompt"]


def test_fill_params_drops_names_outside_the_contract(client, alice, fake_skills,
                                                      monkeypatch):
    """契约外的参数名**丢掉**，并在 notes 里说明——不偷偷塞进 argv。"""
    _fake_model(monkeypatch, json.dumps({
        "input": "1ubq.pdb", "jbo": "typo", "n": 3,
    }))
    r = client.post("/api/tools/demo/fill-params", headers=_headers(alice),
                    json={"requirement": "算一下"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "jbo" not in body["params"]
    assert "jbo" in body["notes"]


def test_fill_params_lists_missing_required(client, alice, fake_skills, monkeypatch):
    """模型填不出的必填项要**列出来让人补**——不许编一个。"""
    _fake_model(monkeypatch, json.dumps({"n": 3}))
    r = client.post("/api/tools/demo/fill-params", headers=_headers(alice),
                    json={"requirement": "算一下"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["missing"] == ["input"]
    assert "input" not in body["params"]


def test_fill_params_ignores_wrong_types(client, alice, fake_skills, monkeypatch):
    _fake_model(monkeypatch, json.dumps({"input": "a.pdb", "n": "不是数字"}))
    r = client.post("/api/tools/demo/fill-params", headers=_headers(alice),
                    json={"requirement": "算一下"})
    assert r.status_code == 200, r.text
    assert "n" not in r.json()["params"]


def test_fill_params_needs_a_requirement(client, alice, fake_skills, monkeypatch):
    _fake_model(monkeypatch, "{}")
    r = client.post("/api/tools/demo/fill-params", headers=_headers(alice),
                    json={"requirement": "   "})
    assert r.status_code == 422, r.text


def test_fill_params_404_for_unknown_tool(client, alice, fake_skills, monkeypatch):
    _fake_model(monkeypatch, "{}")
    r = client.post("/api/tools/nope/fill-params", headers=_headers(alice),
                    json={"requirement": "算一下"})
    assert r.status_code == 404, r.text


def test_fill_params_refuses_a_tool_without_a_contract(client, alice, fake_skills,
                                                       monkeypatch):
    """没有入口脚本的说明书型技能：没有契约可填，如实拒。"""
    d = fake_skills / "protforge-router"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: protforge-router\ndescription: \"Router\"\n---\n\n# r\n",
        encoding="utf-8")
    (d / "tool.yaml").write_text("id: router\nversion: \"1.0\"\n", encoding="utf-8")
    _fake_model(monkeypatch, "{}")
    r = client.post("/api/tools/router/fill-params", headers=_headers(alice),
                    json={"requirement": "算一下"})
    assert r.status_code == 422, r.text


def test_fill_params_422_when_the_model_says_nonsense(client, alice, fake_skills,
                                                       monkeypatch):
    """模型回的不是 JSON → 422，**不许 500**，也不许瞎填一份。"""
    _fake_model(monkeypatch, "我算不出来")
    r = client.post("/api/tools/demo/fill-params", headers=_headers(alice),
                    json={"requirement": "算一下"})
    assert r.status_code == 422, r.text


def test_fill_params_accepts_a_fenced_json_object(client, alice, fake_skills,
                                                   monkeypatch):
    """模型爱加 ```json 围栏——那也是合法回话，不该因此失败。"""
    _fake_model(monkeypatch,
                "好的：\n```json\n{\"input\": \"1ubq.pdb\", \"n\": 5}\n```\n")
    r = client.post("/api/tools/demo/fill-params", headers=_headers(alice),
                    json={"requirement": "算一下"})
    assert r.status_code == 200, r.text
    assert r.json()["params"]["n"] == 5


def test_fill_params_never_runs_anything(client, alice, fake_skills, monkeypatch):
    """**agent 在这里没有执行权**：它只产出参数。"""
    _fake_model(monkeypatch, json.dumps({"input": "1ubq.pdb"}))
    before = len(store.list_tasks(_uid(alice)) or [])
    client.post("/api/tools/demo/fill-params", headers=_headers(alice),
                json={"requirement": "算一下并且把它跑掉"})
    after = len(store.list_tasks(_uid(alice)) or [])
    assert after == before, "填参数不许顺手动任务"


# ---------------------------------------------------------------- 用时复核登记

FAKE_AGENT = r'''
import json, os, sys, time

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

emit({"type": "event", "event": "hello",
      "task_id": os.environ.get("PROTFORGE_TASK_ID", "")})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        cmd = json.loads(line)
    except ValueError:
        continue
    emit({"type": "response", "id": cmd.get("id"), "ok": True,
          "result": {"echo": cmd}})
'''


@pytest.fixture()
def fake_agent(tmp_path):
    p = tmp_path / "fake_agent_rt.py"
    p.write_text(FAKE_AGENT, encoding="utf-8")
    return p


def test_spawn_registers_the_runtime_review(fake_agent, tmp_path, alice):
    """起进程之后必须登记**起跑时刻**和**首次复核时间**。

    少了这两条，`_task_started_at` 永远返回「当前时刻」，elapsed 恒为 0，
    复核机制在、但永远判不出「跑久了」——等于没有。
    """
    task = store.create_task("复核登记", "brief", owner_id=_uid(alice),
                             actor="human")
    tid = int(task["id"])
    d = dispatcher.Dispatcher(db, max_concurrency=1, node_exe=sys.executable,
                              agent_script=str(fake_agent),
                              gpu_probe=lambda: 2)
    try:
        ap = d._spawn(tid, "brief", tmp_path / "work")
        assert tid in d._rt_started, "起跑时刻没登记"
        assert tid in d._rt_next_review, "首次复核时间没登记"
        started = d._task_started_at(tid)
        assert started <= time.monotonic()
        assert d._rt_next_review[tid] > started, "首次复核必须排在起跑之后"
        d.abort(tid)
    finally:
        try:
            dispatcher.kill_tree(ap.proc)
        except Exception:                         # noqa: BLE001
            pass


def test_runtime_review_threshold_cache_does_not_touch_the_db(fake_agent):
    """**起进程那条路上一次库都不读**：阈值走缓存，缓存没热就先用默认值。"""
    d = dispatcher.Dispatcher(db, max_concurrency=1, node_exe=sys.executable,
                              agent_script=str(fake_agent),
                              gpu_probe=lambda: 2)
    assert d._rt_threshold_cache is None
    assert d._rt_threshold_cached() == dispatcher.runtime_reviewer.REVIEW_AFTER_S


def test_settled_task_drops_its_runtime_review_registration(fake_agent, tmp_path,
                                                            alice):
    """任务收口了就不该再被复核——登记要跟着任务一起走。"""
    task = store.create_task("收口", "brief", owner_id=_uid(alice), actor="human")
    tid = int(task["id"])
    d = dispatcher.Dispatcher(db, max_concurrency=1, node_exe=sys.executable,
                              agent_script=str(fake_agent),
                              gpu_probe=lambda: 2)
    try:
        ap = d._spawn(tid, "brief", tmp_path / "work")
        assert tid in d._rt_started
        d._settle(ap, exit_code=0)
        assert tid not in d._rt_started
        assert tid not in d._rt_next_review
    finally:
        try:
            dispatcher.kill_tree(ap.proc)
        except Exception:                         # noqa: BLE001
            pass
