"""「工具意见 → 生成修改方案 → 看 diff → 应用」那条写路径的围栏。

**这条路径在真机上从来没跑通过**，而且失败得毫无痕迹：点「生成修改方案」，
按钮转一圈，界面上变成「上一次生成没成功，可以改完再试」——没别的了。
管理员既分不清是模型调不通还是路径写错，也不知道自己该改什么再试。

真机上的根因（实测，数字见 server/llm.py 的注释）：

* 模型默认思考档把 8192 的预算全花在思考上，正文一个字都没吐出来
  （``finish_reason="length"``）；
* 代码直接下标取 content → KeyError → 报成「模型回的不是预期的 chat 结构」；
* 失败原因只进日志，**界面上一个字都没有**。

所以这里钉三件事：调模型时的档位与预算、失败原因落盘、重新生成时清掉旧原因。

**技能仓库必须隔离。** ``conftest.py`` 只隔离了数据根，技能目录它没动——
用默认值的话，跑一次测试就会去改真实的 ``../protforge\\protforge-plugin``。
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
        "---\nname: protforge-demo\ndescription: 演示技能\n---\n\n"
        "## 用法\n跑一下。\n", encoding="utf-8")
    monkeypatch.setenv("PROTFORGE_WEB_SKILLS_DIR", str(root / "skills"))
    return root


@pytest.fixture()
def llm_box(monkeypatch):
    """模型桩。**记下调用参数**——档位与预算只在这里看得见。"""
    box = {"kwargs": None, "text": "", "error": None}

    def _fake(prompt, *, system="", max_completion_tokens=None,
              reasoning_effort=None, timeout=None):
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


def test_the_proposal_call_asks_for_a_budget_that_fits_thinking(llm_box, skills):
    """**思考档 low + 预算 32k。** 这两项不是「调优」，是「能不能跑通」。

    默认思考档实测思考了 9,441 tokens（43k 字符）、花 165 秒；预算 8192 时
    思考正好把额度吃光 → finish_reason=length → 正文一个字都没有。
    """
    llm_box["text"] = "---\nname: protforge-demo\n---\n\n## 用法\n跑一下。\n\n留了记录。\n"
    rec = _tool_gap_reflection()
    reflection.build_proposal(rec["id"], TARGET)

    kw = llm_box["kwargs"]
    assert kw, "模型根本没被调用"
    assert kw["reasoning_effort"] == "low", \
        f"没设思考档：{kw}（默认档会把预算吃光，还顺手重写整个文件）"
    assert kw["max_completion_tokens"] >= 32768, \
        f"预算不够：{kw}（思考算在同一个额度里）"


def test_a_failed_proposal_records_why_it_failed(llm_box, skills):
    """**失败原因必须落盘。**

    原来失败只把状态标成 failed，原因只进日志——而日志是管理员不会去看的地方。
    界面上「生成失败」四个字，谁也分不清是模型调不通还是路径写错了。
    """
    llm_box["error"] = llm.LLMError(
        "模型输出在 8192 tokens 处被截断（finish_reason=length），只回了思考过程没回正文。")
    rec = _tool_gap_reflection()

    with pytest.raises(llm.LLMError):
        reflection.build_proposal(rec["id"], TARGET)

    got = store.get_reflection(rec["id"])
    assert got["proposal_state"] == "failed", got
    assert "截断" in got["proposal_error"], \
        f"失败原因没留下来：{got!r}（界面上就只剩「生成失败」四个字）"


def test_starting_a_new_proposal_clears_the_previous_reason(llm_box, skills):
    """**重新生成时旧原因必须清掉。**

    不清的话：这一轮还在跑，界面上却挂着上一轮的错；这一轮成功了，
    那个错还挂着——管理员看着一个已经不成立的失败。
    """
    llm_box["error"] = llm.LLMError("模型输出被截断了")
    rec = _tool_gap_reflection()
    with pytest.raises(llm.LLMError):
        reflection.build_proposal(rec["id"], TARGET)
    assert store.get_reflection(rec["id"])["proposal_error"]

    store.set_proposal_state(rec["id"], "running")
    assert store.get_reflection(rec["id"])["proposal_error"] == "", \
        "重新生成时上一轮的原因还挂着"


def test_a_successful_proposal_clears_the_previous_reason(llm_box, skills):
    """**成功的方案不留旧原因。**

    界面上那行「原因：……」说的是上一轮的错，这一轮成功了它还挂着的话，
    管理员看到的是「生成成功」加一句「原因：上次被截断」——两个互斥的结论
    摆在一张卡片上。
    """
    llm_box["error"] = llm.LLMError("上一次截断了")
    rec = _tool_gap_reflection()
    with pytest.raises(llm.LLMError):
        reflection.build_proposal(rec["id"], TARGET)

    llm_box["error"] = None
    llm_box["text"] = ("---\nname: protforge-demo\n---\n\n## 用法\n跑一下。\n\n"
                        "留了调用记录。\n")
    reflection.build_proposal(rec["id"], TARGET)

    got = store.get_reflection(rec["id"])
    assert got["proposal_state"] == "ready", got
    assert got["proposal_error"] == "", \
        f"成功了还挂着上一轮的原因：{got!r}"


def test_hanging_a_new_proposal_clears_the_previous_reason(llm_box, skills):
    """**挂上新方案那一刻，旧原因必须清掉。**

    正常流程里「running」那一次写入已经清了，所以这条是**冗余的那一道**：
    万一将来多一条不走 running 的调用路径（直接挂方案），旧原因就会留在
    一张「生成成功」的卡片旁边——两个互斥的结论摆在一起。
    """
    rec = _tool_gap_reflection()
    store.set_proposal_state(rec["id"], "failed", "上一次被截断了")
    assert store.get_reflection(rec["id"])["proposal_error"]

    store.set_reflection_proposal(rec["id"], "x.patch", TARGET, "deadbeef")
    got = store.get_reflection(rec["id"])
    assert got["proposal_state"] == "ready", got
    assert got["proposal_error"] == "", \
        f"方案挂上了，旧原因还留着：{got!r}"


def test_the_admin_api_hands_the_reason_to_the_panel(llm_box, skills):
    """面板要拿得到原因——**API 那一层不能把它吞了**。

    界面上那行「原因：……」读的是列表端点给的行，不是详情端点。
    """
    auth.create_user("propose-admin", "pw-for-tests", role="admin")
    row = auth.get_user("propose-admin")
    headers = {"Authorization": "Bearer " + auth.issue_token(int(row["id"]))}

    llm_box["error"] = llm.LLMError("模型输出在 8192 tokens 处被截断了")
    rec = _tool_gap_reflection()
    with pytest.raises(llm.LLMError):
        reflection.build_proposal(rec["id"], TARGET)

    client = TestClient(admin_svc.create_admin_app())
    detail = client.get(f"/api/admin/reflections/{rec['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert "截断" in detail.json().get("proposal_error", ""), \
        f"详情端点没带原因：{detail.text[:300]}"

    listing = client.get("/api/admin/reflections", headers=headers)
    assert listing.status_code == 200, listing.text
    row_ = next(r for r in listing.json()["reflections"] if r["id"] == rec["id"])
    assert "截断" in row_.get("proposal_error", ""), \
        f"列表端点没带原因（界面上那行读的就是它）：{row_}"
