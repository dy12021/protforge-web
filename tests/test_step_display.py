"""「当前步骤」这一行到底该显示什么（2026-10-17 真机暴露）。

用户真机上看到的是：

    当前步骤  正在执行：cd <数据盘根>/work/13 && python analyze_bdnf.py 2>&1 | tail

**这不是模型写的。** 模型写的是"候选表位已筛出，正在做交叉反应性/特异性核查"
这种话——真机 #13 的时间线里两者交替出现，一句人话出来几十秒就被命令盖掉。
盖掉它的是 agent 侧的 spawnHook：每条命令执行前把命令原文塞进 step，
心跳再每 60 秒重发一次。

所以提示词改得再好也没用：**旁路是在提示词之外生效的。** 这组测试分两侧钉：

- agent 侧：删掉旁路（``setCurrent`` 与 ``spawnHook`` 都不该再存在），
  提示词明写"只写用户视角的动宾短语、绝不要贴命令"；
- 服务端：相邻同文的 step 不重复写时间线。不去重的话就是拿一种噪音换另一种——
  原来每分钟一行**不同**的命令，现在每分钟一行**一模一样**的话。

去重只比**上一条**，不是全表：做A → 做B → 做A 时，那两条相隔很远的同文事件
都是真实变化，吞掉就丢了过程。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import store
import app as svc

AGENT = Path(__file__).resolve().parent.parent / "agent"
PWD = "password-123"


@pytest.fixture
def owner():
    return auth.create_user("owner1", PWD, role="user")


@pytest.fixture
def tid(owner):
    return store.create_task(title="分析 1ubq", brief="算一下表面暴露",
                            owner_id=owner["id"])["id"]


@pytest.fixture
def internal():
    with TestClient(svc.create_internal_app()) as ci:
        yield ci


def _step(ci, task_id, text):
    return ci.post(f"/api/internal/agent/{task_id}/step", json={"text": text},
                   headers={"X-Agent-Token": svc.issue_agent_token(task_id)})


def _steps(task_id):
    return [e["message"] for e in store.list_events(task_id)
            if e["kind"] == "step"]


# ---------------------------------------------------------------- agent 侧

def _read(name):
    return (AGENT / name).read_text(encoding="utf-8")


def _code_only(name):
    """剥掉 JS 注释，只留代码。

    断言不能被注释里的字眼满足：`run.mjs` 与 `tools.mjs` 的注释里正写着
    「早先这里有个 spawnHook」「每条命令执行前调 setCurrent」——那是解释为什么删，
    不是代码。直接对原文断言的话，这组测试自己就先红了。

    ``//`` 的负向断言避开 ``http://`` 这种协议里的双斜杠。
    """
    src = _read(name)
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"(?<![:'\"\\])//[^\n]*", " ", src)


def test_nothing_can_write_a_shell_command_into_step():
    """把命令塞进 step 的那个旁路必须**不存在**，而不只是"被提示词劝阻"。

    提示词是给模型看的建议，旁路是代码。旁路还在的话，模型写得再漂亮也会被盖掉——
    真机 #13 就是这么发生的：时间线里「候选表位已筛出，正在做交叉反应性核查」
    和「正在执行：cd ... && python pick_p...」交替出现。
    """
    run_js = _code_only("run.mjs")
    tools_js = _code_only("tools.mjs")

    assert "setCurrent" not in run_js, "run.mjs 里不该再有把命令写进 step 的旁路"
    assert "setCurrent" not in tools_js, "tools.mjs 的 setCurrent 已无人调用，应一并删掉"
    assert "spawnHook" not in run_js, "spawnHook 是那条旁路的宿主，必须删干净"

    # 命令原文的去处只有一个：审计。step 侧不许再引用 context.command。
    assert "context.command" not in run_js, \
        "run.mjs 不该再把 context.command 接进任何回传"


def test_the_prompt_forbids_pasting_commands_and_gives_both_examples():
    """提示词必须**同时**有禁令与反例。只写"说人话"这种软话，模型照样贴命令。

    覆盖边界要说清楚：这是对**提示词文本**的断言，测不了模型会不会照做。
    真要验模型行为得跑一次真任务（几分钟起步），那条留在真机验收里。
    这里能钉住的是"禁令不会被人顺手删掉"。
    """
    run_mjs = _read("run.mjs")
    m = re.search(r"const PLATFORM_RULES = `((?:[^`\\]|\\.)*)`;", run_mjs, re.S)
    assert m, "找不到 PLATFORM_RULES 模板"
    block = m.group(1)

    assert "正在搜寻蛋白质序列" in block, \
        "要给出正面例子，否则『用户视角的动宾短语』只是句空话"
    assert re.search(r"`[^`]*&&", block) or "2>&1" in block, \
        "要给出反面例子（带 && 或 2>&1 的那条），模型才知道边界在哪"
    assert "审计" in block, "得说清命令原文另有去处，否则模型以为不贴命令就丢了信息"
    assert "周期性重复上报" not in block, \
        "这条指引已被心跳覆盖：心跳每分钟重发最后一句，模型不必为续命反复上报"


def test_the_tool_description_agrees_with_the_prompt():
    """工具描述与系统提示词不能各说一套——模型看到的是两份，得一致。"""
    tools_mjs = _read("tools.mjs")
    seg = tools_mjs[tools_mjs.index("name: 'report_step'"):][:2000]
    assert "审计" in seg, "report_step 的描述里要点明命令归审计"
    assert "正在搜寻蛋白质序列" in seg, "描述里也要给正面例子"


# ---------------------------------------------------------------- 服务端

def test_the_same_step_again_does_not_stack_up_on_the_timeline(internal, tid):
    """心跳每 60 秒重发同一句话；重复的就不该再占一行时间线。

    但**去重的是时间线噪音，不是"还活着"**：`tasks.step` 照样要更新，
    否则长命令跑着的时候页面会看起来真的卡住了。
    """
    for _ in range(5):
        assert _step(internal, tid, "正在搜寻蛋白质序列").status_code == 200

    assert _steps(tid) == ["正在搜寻蛋白质序列"], f"时间线堆了重复行：{_steps(tid)}"
    assert store.get_task(tid, is_admin=True)["step"] == "正在搜寻蛋白质序列"


def test_a_changed_step_does_get_a_new_row(internal, tid):
    """同文才去重。换句话就是新进展，必须留痕——否则时间线回答不了"变了什么"。"""
    _step(internal, tid, "正在搜寻蛋白质序列")
    _step(internal, tid, "正在用 RFdiffusion 生成结构")
    assert _steps(tid) == ["正在搜寻蛋白质序列", "正在用 RFdiffusion 生成结构"]


def test_returning_to_an_earlier_step_is_not_swallowed(internal, tid):
    """只跟**上一条**比，不是跟全表比。

    做A → 做B → 做A 是真实的过程（回头返工），拿全表去重会把最后那条吞掉，
    时间线就只剩两句，看不出返工发生过。
    """
    for text in ("正在比对候选表位", "正在核查特异性", "正在比对候选表位"):
        _step(internal, tid, text)
    assert _steps(tid) == ["正在比对候选表位", "正在核查特异性", "正在比对候选表位"]


def test_dedup_does_not_swallow_other_kinds_that_happen_to_match(internal, tid):
    """去重只认 kind='step'。别的种类带着一模一样的文案，该写还得写。"""
    _step(internal, tid, "正在计算表面面积")
    store.add_event(tid, "note", "agent", "正在计算表面面积")
    kinds = [(e["kind"], e["message"]) for e in store.list_events(tid)]
    assert ("note", "正在计算表面面积") in kinds, \
        f"同文的其他种类被误吞了：{kinds}"
    assert kinds.count(("step", "正在计算表面面积")) == 1


def test_the_timeline_shows_exactly_what_the_card_shows(internal, tid):
    """卡片上的当前步骤和时间线上的那一行必须是同一句话。

    原来时间线存的是未截断的原文、卡片存的是截断后的——同一件事两副面孔，
    用户对不上账。去重键也必须用**显示值**，否则两条截断后一样的原文会各写一行。
    """
    long_text = "正在用 RFdiffusion 生成结构" + "补" * 120
    _step(internal, tid, long_text)

    shown = store.get_task(tid, is_admin=True)["step"]
    assert _steps(tid) == [shown], \
        f"时间线与卡片不一致：卡片={shown!r}，时间线={_steps(tid)}"
    assert len(shown) <= 80, f"截断没生效：{len(shown)} 字"
