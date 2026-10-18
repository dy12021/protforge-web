"""本地预览与服务端渲染的**逐句一致性**（Minor-2）。

同一件事在这个项目里有两份实现：``web/workflow.js`` 的 ``renderSlots``（界面上
「改完立刻出预览」的那句话）与 ``server/workflow.py`` 的 ``render``（真正跑下去、
写进任务 brief 的那句话）。两边分家的后果不是报错，是**用户照着预览把模板配好，
跑出来的却是另一句话**，而界面上看不出任何异常。

上一轮复审把本地 ``SLOT_RE`` 改窄成 ``/\\{\\{([^{}]+)\\}\\}/g``（于是
``{{ seq }}`` 这种带空格的写法本地不再匹配、服务端仍然匹配），
``test_web_static`` **38 条全绿**、前端冒烟 **rc=0**——因为当时那条自称"一致性"
的断言只是「JS 里声明了一个以 ``/\\{\\{`` 开头的 SLOT_RE」+「服务端源码里有那个
正则字面量」，分辨不出两者是否等价。

所以这里换一种围法：**真跑两边**。

1. 用**服务端** ``workflow.render()`` 渲染一批用例，把结果写进临时 JSON；
2. 经 node 跑 ``tests/frontend_smoke.mjs``，它在 vm 里加载**真的**
   ``web/workflow.js``、调**真的** ``renderSlots``，逐句对比。

于是任何一边改了语法、或者改了一边的边界行为（没绑定的槽位、值里带 ``$``、
值里又出现 ``{{}}``……），这条就红。Node 不在 PATH 上时整条跳过。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import workflow as wfmod

REPO = Path(__file__).resolve().parent.parent
SMOKE = REPO / "tests" / "frontend_smoke.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="没装 node，跑不了前端脚本")

#: (用例名, 模板, 槽位取值)。**取值都是字符串**——真实路径上 openpyxl 单元格经
#: row_values 出来就是字符串，界面传过来的也是字符串，比这个再宽的形状两边没有
#: 共同的语义可比。
CASES = [
    ("最普通的一种", "请计算 {{seq}}", {"seq": "SEQ-001"}),
    # 下面这条就是上一轮复审用的那条变异：`{{ seq }}` 带空格时，本地那份正则
    # 曾经匹配不上，于是预览原样显示 {{ seq }}，跑出去的却是真值。
    ("花括号里带空格", "请计算 {{ seq }}", {"seq": "SEQ-001"}),
    ("花括号里带制表符", "请计算 {{\tseq\t}}", {"seq": "SEQ-001"}),
    ("同一个槽位用两次", "{{seq}} 与 {{seq}}", {"seq": "X"}),
    # 没绑定的槽位渲染成空串，不报错——空单元格是常态不是用户的错
    # （服务端 render 的 docstring 就是这么写的）。
    ("没绑定的槽位", "[{{missing}}]", {"seq": "SEQ-001"}),
    ("取到的是空串", "[{{seq}}]", {"seq": ""}),
    # 值里带 $&：JS 的 replace 用**回调**时 $& 不是特殊记号，用**替换串**时它会
    # 被展开成整段匹配。哪边写错这里立刻分家。
    ("值里带 $&", "[{{seq}}]", {"seq": "$&"}),
    ("值里带 $1", "[{{seq}}]", {"seq": "$1"}),
    ("值里带反斜杠", "[{{seq}}]", {"seq": r"a\\b"}),
    # 值里又出现 {{}}：两边都只替换一遍，不递归。
    ("值里又出现槽位", "[{{seq}}]", {"seq": "{{other}}", "other": "不该出现"}),
    # 三个花括号：两边必须给出同一句（不管那一句是什么）。
    ("三个花括号", "{{{seq}}}", {"seq": "V"}),
    ("单花括号不是槽位", "{seq} 与 { seq }", {"seq": "V"}),
    ("模板里一个槽位都没有", "就这一句话，没有变量。", {"seq": "V"}),
    ("多槽位混排", "{{a}}｜{{ b }}｜{{c}}",
     {"a": "甲", "b": "乙", "c": "丙"}),
    ("换行与多空格", "{{\n  seq  \n}}", {"seq": "SEQ-001"}),
]


def test_local_preview_renders_exactly_like_the_server(tmp_path):
    payload = tmp_path / "parity.json"
    cases = [{"name": name, "tpl": tpl, "slots": slots,
              "expected": wfmod.render(tpl, slots)}
             for name, tpl, slots in CASES]
    payload.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    # 夹具自证：这些用例必须真的替换掉了东西，否则「两边都原样返回」也会全绿；
    # 而且必须真的覆盖住带空格那种写法——上一轮那条围栏就是漏在这里。
    assert any(c["expected"] != c["tpl"] for c in cases), \
        "夹具自证：用例全都没替换掉任何东西，这条围栏会恒绿"
    spaced = [c for c in cases if c["name"] == "花括号里带空格"][0]
    assert "SEQ-001" in spaced["expected"] and "{{" not in spaced["expected"], \
        "夹具自证：带空格那种写法没有真的被替换掉"

    env = dict(os.environ, PROTFORGE_WEB_PARITY=str(payload))
    r = subprocess.run(
        ["node", str(SMOKE), "web/workflow.js"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env)
    assert r.returncode == 0, (
        "本地预览与服务端渲染给出的不是同一句话——用户照着预览配模板，"
        f"跑出来的是另一句：\n{r.stdout.strip()}\n{r.stderr.strip()}")


def test_the_parity_cases_are_not_trivially_equal(tmp_path):
    """反向围栏：把 expected 换成模板原文，上面那条必须红。

    为什么要单独一条：一致性围栏最常见的假绿，是「两边都崩了/都返回原样」。
    这里换掉一半的期望值，确认对比真的在逐句进行。
    """
    payload = tmp_path / "parity_broken.json"
    cases = [{"name": name, "tpl": tpl, "slots": slots,
              "expected": ("这不是服务端会渲染出来的句子"
                           if i % 2 == 0 else wfmod.render(tpl, slots))}
             for i, (name, tpl, slots) in enumerate(CASES)]
    payload.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, PROTFORGE_WEB_PARITY=str(payload))
    r = subprocess.run(
        ["node", str(SMOKE), "web/workflow.js"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env)
    assert r.returncode != 0, \
        "把一半的期望值换成错的，它照样绿——这条围栏没在逐句对比"
