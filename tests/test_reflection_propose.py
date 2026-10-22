"""「工具意见 → 生成修改方案 → 看 diff → 应用」那条路的围栏：**它已经废了**。

以前这里是平台唯一一处会改别人仓库的代码：模型吐出一份完整的技能文件，
服务端算 diff，人看过 diff 才应用，还带 git 提交。为了让它跑通，
连调模型的档位与预算都调过好几轮（真机上默认档思考了 9,441 tokens、
花 165 秒，正文一个字都没吐出来）。

现在工具问题只按日期攒进 ``<数据根>/tool-issues/<YYYY-MM-DD>.md``，
由用户每周用 minimax code 统一修一遍：平台**不生成 diff、不自动改技能仓库、
不提交 git**。

所以这个文件现在钉的是**废掉的边界**：

* 生成方案**不调模型**（一次模型调用都不该发生）；
* 生成方案**不写任何文件**（连方案文件与 .new 都不写）；
* 应用方案**不碰技能仓库、不提交 git**；
* 这些接口被调用时**不抛异常**（管理端还有路由挂在它们上面）。

**技能仓库必须隔离。** ``conftest.py`` 只隔离了数据根，技能目录它没动——
用默认值的话，跑一次测试就会去改真实的 ``<技能仓>``。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
ADMIN_DIR = REPO / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

import app_admin as admin_svc       # noqa: E402
import auth                         # noqa: E402
import llm                          # noqa: E402
import memory                       # noqa: E402
import reflection                   # noqa: E402
import store                        # noqa: E402

SKILL = "protforge-demo"
TARGET = f"skills/{SKILL}/SKILL.md"

BODY = """\
# 反思 · 任务 #{tid} · 演示任务

- 终态：done
- 模型：stub

## 这次实际做了什么
演示。

## 经验
（无）

## 工具意见
- [技能 protforge-demo] 这个技能没留下调用记录，重启后查不到它跑过没有。

## 建议分类
tool_gap
"""


@pytest.fixture()
def skills(tmp_path, monkeypatch):
    """一个**临时**技能仓库（带 git），指给 PROTFORGE_WEB_SKILLS_DIR。"""
    root = tmp_path / "plugin"
    sk = root / "skills" / SKILL
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: 演示技能\n---\n\n## 用法\n跑一下。\n",
        encoding="utf-8")
    monkeypatch.setenv("PROTFORGE_WEB_SKILLS_DIR", str(root / "skills"))
    return root


@pytest.fixture()
def llm_box(monkeypatch):
    """模型桩。**记下调用参数**——调用次数只在这里看得见。"""
    box = {"kwargs": None, "text": "", "error": None, "calls": 0}

    def _fake(prompt, *, system="", max_completion_tokens=None,
              reasoning_effort=None, timeout=None):
        box["calls"] += 1
        box["kwargs"] = {"max_completion_tokens": max_completion_tokens,
                         "reasoning_effort": reasoning_effort, "system": system}
        if box["error"] is not None:
            raise box["error"]
        return box["text"]

    monkeypatch.setattr(llm, "chat_completion", _fake)
    return box


def _tool_gap_reflection() -> dict:
    """一条已分类为「工具意见」的反思。

    **是普通函数不是夹具**：夹具只能由测试签名请求，直接调用它 pytest 会
    报「Fixture called directly」——这条围栏本来就不需要任何夹具参数。
    """
    t = store.create_task("演示任务", "brief", owner_id=1)
    tid = int(t["id"])
    p = reflection.body_path(tid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(BODY.format(tid=tid), encoding="utf-8")
    rec = store.create_reflection(tid, str(p))
    return store.classify_reflection(rec["id"], "tool_gap", SKILL, "tester")


def _git_log(root: Path) -> str:
    import subprocess
    p = subprocess.run(["git", "log", "--oneline"], cwd=str(root), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    return p.stdout or ""


def test_generating_a_proposal_does_not_call_the_model(llm_box, skills):
    """生成方案**一次模型调用都不该发生**。

    以前这里要专门调思考档与预算，就为了让它吐出一份完整的技能文件。
    现在工具问题只是「攒起来等用户统一修」，没什么要模型生成的。
    """
    rec = _tool_gap_reflection()
    calls_before = llm_box["calls"]

    reflection.build_proposal(rec["id"], TARGET)

    assert llm_box["calls"] == calls_before, \
        f"生成方案还在调模型（{llm_box['calls'] - calls_before} 次）"


def test_a_failed_proposal_records_why_it_failed(llm_box, skills):
    """**失败原因必须落盘**，而且要说清新的去处。

    原来失败只把状态标成 failed，原因只进日志——而日志是管理员不会去看
    的地方。现在它要说清楚：工具问题该写进哪个目录的哪个文件。
    """
    rec = _tool_gap_reflection()

    reflection.build_proposal(rec["id"], TARGET)

    got = store.get_reflection(rec["id"])
    assert got["proposal_state"] == "failed", got
    assert "工具问题" in got["proposal_error"], \
        f"失败原因没留下来：{got!r}（界面上就只剩「生成失败」四个字）"
    assert str(memory.tool_issues_dir()) in got["proposal_error"], \
        f"失败原因没说清工具问题该写去哪：{got!r}"


def test_starting_a_new_proposal_clears_the_previous_reason(llm_box, skills):
    """重新生成时旧原因必须清掉。

    不清的话：这一轮还在跑，界面上却挂着上一轮的错；这一轮失败了，
    那个错还挂着——管理员看着一个已经不成立的失败。
    """
    rec = _tool_gap_reflection()
    store.set_proposal_state(rec["id"], "failed", "上一次被截断了")
    assert store.get_reflection(rec["id"])["proposal_error"]

    reflection.build_proposal(rec["id"], TARGET)

    got = store.get_reflection(rec["id"])
    assert got["proposal_state"] == "failed", got
    assert "截断" not in got["proposal_error"], \
        f"重新生成时上一轮的原因还挂着：{got!r}"
    assert got["proposal_error"], "新的原因也没写：界面上会什么都不说"


def test_a_successful_proposal_clears_the_previous_reason(llm_box, skills):
    """**不再有「生成成功」这一档**：这条路只有一个结局。

    界面上那行「原因：……」说的是上一轮的错，而这一轮压根没有方案可言。
    """
    rec = _tool_gap_reflection()
    store.set_proposal_state(rec["id"], "failed", "上一次被截断了")

    reflection.build_proposal(rec["id"], TARGET)

    got = store.get_reflection(rec["id"])
    assert got["proposal_state"] == "failed", \
        f"这条路上不该再有「生成成功」：{got!r}"
    assert got["proposal_error"], "没写原因：界面上就只剩「生成失败」四个字"


def test_hanging_a_new_proposal_clears_the_previous_reason(llm_box, skills):
    """**不再挂方案**：没有方案文件可挂。

    以前这里会把方案路径与原文哈希写进记录，等人点「应用」时用。
    现在没有方案，所以那几列永远是空的。
    """
    rec = _tool_gap_reflection()
    store.set_reflection_proposal(rec["id"], "x.patch", TARGET, "deadbeef")
    got = store.get_reflection(rec["id"])
    assert got["proposal_state"] == "ready", got

    reflection.build_proposal(rec["id"], TARGET)

    after = store.get_reflection(rec["id"])
    assert after["proposal_state"] == "failed", after
    assert after["proposal_error"], "没写原因"


def test_no_proposal_files_are_written(llm_box, skills):
    """生成方案**不写任何文件**：方案文件与 .new 都不写。

    以前这里会写 ``proposals/refl-N.patch`` 与 ``proposals/refl-N.new``，
    那是留给「看 diff 再决定」用的。现在没有 diff 可看。
    """
    rec = _tool_gap_reflection()

    reflection.build_proposal(rec["id"], TARGET)

    d = reflection.proposals_dir()
    assert not d.exists() or not list(d.glob("*.patch")), "仍然在写方案文件"
    assert not d.exists() or not list(d.glob("*.new")), "仍然在写方案正文"


def test_the_admin_api_hands_the_reason_to_the_panel(llm_box, skills):
    """面板要拿得到原因——**API 那一层不能把它吞了**。

    界面上那行「原因：……」读的是列表端点给的行，不是详情端点。
    """
    auth.create_user("propose-admin", "pw-for-tests", role="admin")
    row = auth.get_user("propose-admin")
    headers = {"Authorization": "Bearer " + auth.issue_token(int(row["id"]))}

    rec = _tool_gap_reflection()
    reflection.build_proposal(rec["id"], TARGET)

    client = TestClient(admin_svc.create_admin_app())
    detail = client.get(f"/api/admin/reflections/{rec['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert "工具问题" in detail.json().get("proposal_error", ""), \
        f"详情端点没带原因：{detail.text[:300]}"

    listing = client.get("/api/admin/reflections", headers=headers)
    assert listing.status_code == 200, listing.text
    row_ = next(r for r in listing.json()["reflections"] if r["id"] == rec["id"])
    assert "工具问题" in row_.get("proposal_error", ""), \
        f"列表端点没带原因（界面上那行读的就是它）：{row_}"


def test_a_reflowed_paragraph_is_left_alone(llm_box, skills):
    """技能文件**一个字节都不许动**。

    以前这里是「模型重排了没提到的段落 → 服务端还原回去」。现在模型根本不
    被叫来，自然也没有可还原的东西——但技能文件仍然必须原样。
    """
    target = skills / "skills" / SKILL / "SKILL.md"
    before = target.read_text(encoding="utf-8")
    rec = _tool_gap_reflection()

    reflection.build_proposal(rec["id"], TARGET)

    assert target.read_text(encoding="utf-8") == before, \
        "生成方案这一步动了技能文件"


def test_a_changed_emphasis_is_left_alone(llm_box, skills):
    """强调写法那一套也不需要了——但文件仍然不许动。

    以前这里要区分「`*not*` 写成 `**not**` 算折行」；现在没有模型输出，
    也就没有折行，技能文件原样就是对的。
    """
    target = skills / "skills" / SKILL / "SKILL.md"
    before = target.read_text(encoding="utf-8")
    rec = _tool_gap_reflection()

    reflection.build_proposal(rec["id"], TARGET)

    assert target.read_text(encoding="utf-8") == before, \
        "生成方案这一步动了技能文件"


def test_applying_a_proposal_does_not_touch_the_repo(llm_box, skills):
    """应用**不碰技能仓库、不提交 git**。

    平台不碰别人的仓库：这条边界现在由「什么都不做」来保证，而不是由
    「校验过哈希再写」来保证。
    """
    rec = _tool_gap_reflection()
    before = (skills / "skills" / SKILL / "SKILL.md").read_text(encoding="utf-8")
    commits_before = _git_log(skills)

    out = reflection.apply_proposal(rec["id"])

    assert out["ok"] is False and out["deprecated"] is True
    assert (skills / "skills" / SKILL / "SKILL.md").read_text(
        encoding="utf-8") == before, "应用方案动了技能文件"
    assert _git_log(skills) == commits_before, "应用方案竟然提交了 git"
    assert store.get_reflection(rec["id"])["status"] != "applied"
