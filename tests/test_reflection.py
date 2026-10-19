"""反思（rsi）的围栏。

**这些测试一个模型都不调。** `llm.chat_completion` 被换成一个返回固定文本的
桩：要验的是顺序、幂等与边界，不是模型答得好不好。真正调模型的路径一旦进了
测试套件，全量就会在离线机器上红，而且红的原因与被测代码无关。

**技能目录必须隔离。** ``conftest.py`` 只把数据库指向 tmp_path，没有动
``PROTFORGE_WEB_SKILLS_DIR``——而落地经验与应用方案都写那个目录。用默认值的话，
跑一次测试就在真实的 ``../protforge\\protforge-plugin`` 里留下 ``references/lessons.md``，
技能文件也可能被改掉。所以下面 ``_skills`` 夹具先建一个临时技能仓库再指过去。

规则本身是断言，行为本身是事实。改 ``server/reflection.py`` 请连这个文件一起读。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ADMIN_DIR = REPO / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

import auth        # noqa: E402
import db          # noqa: E402
import llm         # noqa: E402
import reflection  # noqa: E402
import store       # noqa: E402

SKILL = "protforge-demo"

GOOD_REPLY = """\
模型说的一段话。

## 这次实际做了什么
把 7cvw 的 128 条突变体跑了一遍。

## 顺利的地方
- 先跑野生型对照定标，后面排序不用反复校准

## 卡住的地方
- 中途换过两次参数口径

## 经验
- [技能 protforge-demo] 先跑一条野生型对照定标，再批量跑，口径才可比
- [技能 通用] 批量跑之前先把参数口径写下来

## 工具意见
- [技能 protforge-demo] 没有只改指定位点的开关，只能整链重跑

## 建议分类
lesson
"""


@pytest.fixture
def skills(tmp_path, monkeypatch):
    """一个**临时**技能仓库（含 git），指给 PROTFORGE_WEB_SKILLS_DIR。

    带 git 是因为 apply_proposal 要提交。真实仓库绝对不能被这些测试碰到——
    那不只是脏，是把用户的技能文件改掉了。

    每条 git 都显式给 encoding：本机 ANSI 码页是 gbk，git 那边的 UTF-8 字节
    会在解码线程里抛 UnicodeDecodeError，而命令返回码照样是 0。
    """
    root = tmp_path / "plugin"
    sk = root / "skills" / SKILL
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: 演示技能\n---\n\n## 用法\n跑一下。\n",
        encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=120)

    git("init", "-q")
    git("config", "user.name", "t")
    git("config", "user.email", "t@t")
    git("add", "-A")
    git("commit", "-qm", "init")
    monkeypatch.setenv("PROTFORGE_WEB_SKILLS_DIR", str(root / "skills"))
    return root


def _git_log(root: Path) -> str:
    p = subprocess.run(["git", "log", "--oneline"], cwd=str(root), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    return p.stdout or ""


@pytest.fixture
def fake_llm(monkeypatch):
    """把模型换成一个可控的桩。返回 {text, calls}，``text`` 可随时改。"""
    box = {"text": GOOD_REPLY, "calls": 0}

    def _fake(prompt, *, system="", max_tokens=None, timeout=None):
        box["calls"] += 1
        return box["text"]

    monkeypatch.setattr(llm, "chat_completion", _fake)
    return box


@pytest.fixture
def users():
    return auth.create_user("rsi-owner", "password-12345", "user"), \
        auth.create_user("rsi-admin", "password-12345", "admin")


def _finished_task(owner_id, *, status="done", title="7cvw 批量排序", brief="跑 128 条"):
    t = store.create_task(title, brief, owner_id=owner_id)
    tid = int(t["id"])
    store.set_status(tid, "running", actor="system")
    store.add_event(tid, "step", "agent", "正在跑第 41 条")
    store.set_status(tid, status, actor="system", message="收工")
    store.update_task(tid, summary="产出前 20 条")
    return tid


# --- 生成 ---------------------------------------------------------------------


def test_reflection_refuses_before_the_task_reaches_a_terminal_state(users, fake_llm, skills):
    """没进终态就复盘，复盘的是半成品——而半成品的"经验"是最不该进记忆的东西。"""
    owner, _ = users
    tid = int(store.create_task("还在跑", "x", owner_id=owner["id"])["id"])
    with pytest.raises(reflection.ReflectionError):
        reflection.generate(tid)
    assert fake_llm["calls"] == 0, "还没到终态就去调模型了"
    assert store.get_reflection_by_task(tid) is None


def test_sweep_finds_terminal_tasks_and_stops_finding_them_afterwards(
        users, fake_llm, skills):
    """巡检的幂等性由两条一起断：先能扫到，生成之后扫不到了。

    只断前一半的话，「生成了两次」是绿的——而那是白烧一次模型调用。
    """
    owner, _ = users
    tid = _finished_task(owner["id"])
    assert [int(r["id"]) for r in store.terminal_tasks_without_reflection(limit=5)] == [tid]

    reflection.generate(tid)

    assert store.terminal_tasks_without_reflection(limit=5) == []


def test_generate_writes_a_standalone_file_and_keeps_the_raw_reply(
        users, fake_llm, skills):
    """正文必须是**一个独立文件**，且模型原话一并留着。

    留原话不是冗余：解析器只认得固定小节，模型哪天换了个说法，那一节之外
    的内容就只剩原话这一份。管理员看到「解析不出来」时唯一的线索就在这。
    """
    owner, _ = users
    tid = _finished_task(owner["id"])
    rec = reflection.generate(tid)

    assert rec["status"] == "draft"
    assert rec["suggested"] == "lesson"
    p = Path(rec["path"])
    assert p.is_file(), "反思正文没落成文件"
    assert p.parent == reflection.reflections_dir()
    body = p.read_text(encoding="utf-8")
    assert "先跑一条野生型对照定标" in body
    assert "## 模型原始输出" in body and "模型说的一段话" in body


def test_a_failed_generation_is_recorded_as_failed_not_as_an_empty_review(
        users, fake_llm, skills):
    """解析不出小节时如实标 failed。

    返回一堆空串然后照常建记录是最坏的一种：界面上看着像一条正常复盘，
    管理员分类之后却什么也沉淀不了——「看起来成功、实际是空的」。
    """
    owner, _ = users
    tid = _finished_task(owner["id"], status="failed")
    fake_llm["text"] = "我就随便说两句，没有小节标题。"
    with pytest.raises(reflection.ReflectionError):
        reflection.generate(tid)

    rec = store.get_reflection_by_task(tid)
    assert rec["status"] == "failed", rec
    assert rec["error"], "标了 failed 却没写原因，管理员看不出该重试还是该换模型"


def test_generate_is_idempotent_and_does_not_burn_a_second_model_call(
        users, fake_llm, skills):
    owner, _ = users
    tid = _finished_task(owner["id"])
    first = reflection.generate(tid)
    calls_after_first = fake_llm["calls"]

    second = reflection.generate(tid)

    assert int(second["id"]) == int(first["id"])
    assert fake_llm["calls"] == calls_after_first, \
        "同一个任务复盘了两次——白烧一次模型调用，而记录里看不出哪次是真的"


# --- 解析 ---------------------------------------------------------------------


def test_parser_rejects_a_reply_with_no_sections_at_all():
    with pytest.raises(reflection.ReflectionError):
        reflection.parse_reflection("模型今天心情不好，只回了一句话。")


def test_parser_tolerates_numbering_and_code_fences_in_the_headings():
    """模型很爱写「## 二、经验」或者把整段包在 ``` 里。

    认不出来就等于这次复盘白跑——而这只需要头尾各多认一次。
    """
    parsed = reflection.parse_reflection(
        "```\n## 一、这次实际做了什么\n跑了一遍\n\n## 经验\n"
        "- [技能 protforge-demo] 先定标再批量\n\n## 建议分类\nlesson\n```")
    assert [i["text"] for i in parsed["lessons"]] == ["先定标再批量"]
    assert parsed["suggested"] == "lesson"


def test_parser_treats_wu_as_no_items_rather_than_one_empty_item():
    """「无」是**没有这一节内容**，不是一条内容为空的条目。

    当成一条的话，它会被原样写进经验文件，下一个任务读到的是
    「- 某技能：无」。
    """
    parsed = reflection.parse_reflection(
        "## 这次实际做了什么\nx\n\n## 经验\n无\n\n## 建议分类\nlesson")
    assert parsed["lessons"] == []


def test_parser_normalizes_an_unknown_verdict_to_empty():
    """模型给了词表外的东西时**归一成空**，不硬塞进库里。

    库里那一列有 CHECK；硬塞会在写库那一刻炸掉，而那时模型的钱已经花了。
    """
    parsed = reflection.parse_reflection(
        "## 这次实际做了什么\nx\n\n## 经验\n- [技能 x] 一条\n\n## 建议分类\n随便写的")
    assert parsed["suggested"] == ""


# --- 分类 ---------------------------------------------------------------------


def test_classification_is_a_separate_step_from_landing(users, fake_llm, skills):
    """分类只推状态，**不落地任何东西**。

    合成一步的话，管理员「看着像就点」会把未经细看的内容写进记忆，
    而那份记忆会喂给后面每一个任务。中间必须有一次停顿。
    """
    owner, admin = users
    tid = _finished_task(owner["id"])
    rec = reflection.generate(tid)

    out = store.classify_reflection(rec["id"], "lesson", SKILL, admin["username"])

    assert out["status"] == "reviewed"
    assert out["kind"] == "lesson"
    assert not reflection.lessons_path().exists(), \
        "分类这一步就把经验写出去了——落地和分类被合成了一步"


def test_an_applied_reflection_cannot_be_reclassified(users, fake_llm, skills):
    """已落地的再改一次分类，等于把已经进了经验文件的内容悄悄换个身份，
    而没人去撤掉那一条。"""
    owner, admin = users
    tid = _finished_task(owner["id"])
    rec = reflection.generate(tid)
    rec = store.classify_reflection(rec["id"], "lesson", SKILL, admin["username"])
    import app_admin
    app_admin._land_lesson(rec, admin["username"])

    with pytest.raises(store.Invalid):
        store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])


def test_reject_is_available_before_landing(users, fake_llm, skills):
    """必须有否决这条路。反思会一直产出，没有否决口子的分类队列只会
    越积越长，最后没人看——那等于没做。"""
    owner, admin = users
    tid = _finished_task(owner["id"])
    rec = reflection.generate(tid)

    out = store.reject_reflection(rec["id"], admin["username"])
    assert out["status"] == "rejected"


# --- 落地 ---------------------------------------------------------------------


def test_landing_a_lesson_writes_both_the_global_file_and_the_skill(
        users, fake_llm, skills):
    """两层都要写：全局经验文件是**每任务都注入**的那一层，
    技能的 references 是「知识待在离工具最近的地方」的那一层。"""
    owner, admin = users
    import app_admin
    tid = _finished_task(owner["id"])
    rec = reflection.generate(tid)
    rec = store.classify_reflection(rec["id"], "lesson", SKILL, admin["username"])

    out = app_admin._land_lesson(rec, admin["username"])

    assert out["landed"] == 2, out
    assert out["landed_into_skill"] == 1, \
        "「通用」那条不该算进技能，只有 protforge-demo 那条算"
    global_text = reflection.lessons_path().read_text(encoding="utf-8")
    assert "先跑一条野生型对照定标" in global_text
    assert "把参数口径写下来" in global_text
    # 带来源：将来有人觉得某条不对，能顺着找到当时的复盘
    assert f"任务 #{tid}" in global_text
    skill_ref = skills / "skills" / SKILL / "references" / "lessons.md"
    assert skill_ref.is_file(), "没有沉淀进技能的 references"
    assert "先跑一条野生型对照定标" in skill_ref.read_text(encoding="utf-8")


def test_landing_refuses_when_the_review_has_no_lessons(users, fake_llm, skills):
    """「经验」一节里没有条目时**不许静悄悄地什么都不写**。

    返回成功而经验文件里没多东西，正是这个项目最熟的一种静默失效。
    """
    owner, admin = users
    import app_admin
    tid = _finished_task(owner["id"])
    fake_llm["text"] = (
        "## 这次实际做了什么\nx\n\n## 经验\n无\n\n## 建议分类\nlesson")
    rec = reflection.generate(tid)
    rec = store.classify_reflection(rec["id"], "lesson", SKILL, admin["username"])

    with pytest.raises(store.Invalid):
        app_admin._land_lesson(rec, admin["username"])


def test_the_lessons_block_is_capped_and_empty_when_there_are_none(skills):
    """没有经验时**不加这段**。

    给模型一个「## 已沉淀的经验（暂无）」的空壳，只会让它以为这里本来就
    没什么可参考，而实际上只是还没沉淀过。
    """
    assert reflection.lessons_block() == ""
    for i in range(40):
        reflection.append_lesson("", f"第 {i} 条经验，内容要足够长才能被上限砍掉" * 6,
                                  task_id=i, decided_by="admin")
    block = reflection.lessons_block()
    assert block.strip()
    assert len(block) <= reflection.LESSONS_BLOCK_MAX


def test_the_lessons_block_only_carries_the_most_recent_entries(skills):
    """全量留在文件里，提示词只带最近这些。

    一份塞满几百条旧经验的提示词会把真正的需求挤出注意力，
    而那条路是从**所有**任务身上收的税。
    """
    for i in range(reflection.LESSONS_BLOCK_ITEMS + 6):
        reflection.append_lesson("", f"经验-{i}", task_id=i, decided_by="admin")
    block = reflection.lessons_block()
    assert "经验-0" not in block
    assert f"经验-{reflection.LESSONS_BLOCK_ITEMS + 5}" in block


def test_dispatcher_injects_the_lessons_before_the_brief(skills):
    """经验块放在需求**前面**，且一条都没有时不加这一段。

    断的是**源码里的相对位置**，不是行为：`_dispatch_prompt` 是 Dispatcher 上的
    @staticmethod，构造一个 Dispatcher 去调它要拉起整套调度器，代价远大于这一条
    规则本身值得的；而它唯一要证明的就是「注入发生在拼 prompt 之前」。
    """
    src = (REPO / "server" / "dispatcher.py").read_text(encoding="utf-8")
    body = src.split("def _dispatch_prompt", 1)[1].split("\n    def ", 1)[0]
    assert "reflection.lessons_block()" in body, "派活时没取经验块"
    inject_at = body.index("reflection.lessons_block()")
    ask_at = body.index('"type": "prompt"')
    assert inject_at < ask_at, "经验块是在需求之后才拼的——长需求会把它挤出注意力"
    assert "if lessons:" in body, \
        "没有经验时也会拼一个空壳，那会让模型以为本来就没什么经验"


# --- 工具意见 → 修改方案 ------------------------------------------------------


def test_a_tool_gap_cannot_be_landed_in_one_step(users, fake_llm, skills):
    """工具意见的落地是**改代码 + git 提交**，必须走 propose/apply 两步，
    中间有一双眼睛看过 diff。一步落地等于系统自己改自己。"""
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    fake_llm["text"] = GOOD_REPLY.replace("建议分类\nlesson", "建议分类\ntool_gap")
    rec = reflection.generate(tid)
    assert rec["suggested"] == "tool_gap"
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])

    with pytest.raises(store.Invalid):
        reflection.apply_proposal(rec["id"])          # 还没生成方案
    import app_admin
    fresh = store.get_reflection(rec["id"])
    with pytest.raises(store.Invalid):
        app_admin._land_lesson(fresh, admin["username"])


@pytest.mark.parametrize("target", [
    "../outside.md", "/etc/passwd", "C:/Windows/x.md", "notskills/x.md",
    "skills/../../escape.md", "",
])
def test_proposal_targets_cannot_escape_the_plugin_repo(users, fake_llm, skills,
                                                        target):
    """这是唯一一处会写进别人仓库的代码，路径一旦能逃出去就等于任意写。"""
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    fake_llm["text"] = GOOD_REPLY.replace("建议分类\nlesson", "建议分类\ntool_gap")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])

    with pytest.raises(store.Invalid):
        reflection.build_proposal(rec["id"], target)


def test_the_diff_is_computed_by_us_not_written_by_the_model(
        users, fake_llm, skills):
    """模型只产出**完整新内容**，diff 由 difflib 算。

    让模型自己写 unified diff，它几乎总会漏 hunk 头或对错行号，而那种 diff
    打补丁时的表现是「部分应用、剩下几块静默不动」。
    """
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    fake_llm["text"] = GOOD_REPLY.replace("建议分类\nlesson", "建议分类\ntool_gap")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])

    target = skills / "skills" / SKILL / "SKILL.md"
    before = target.read_text(encoding="utf-8")
    fake_llm["text"] = before.replace("跑一下。", "跑之前先做野生型对照定标。")
    out = reflection.build_proposal(rec["id"], f"skills/{SKILL}/SKILL.md")

    assert out["diff"].startswith("--- a/")
    assert "@@" in out["diff"]
    assert "野生型对照定标" in out["diff"]
    # difflib 的删除/新增行是「-内容」「+内容」，**不带** git diff 那种前导空格
    assert "-跑一下。" in out["diff"], out["diff"]
    assert "+跑之前先做野生型对照定标。" in out["diff"], out["diff"]
    assert target.read_text(encoding="utf-8") == before, \
        "生成方案这一步就把技能文件改了——它只该产出方案，不该动文件"
    assert store.get_reflection(rec["id"])["proposal_state"] == "ready"


def test_a_proposal_that_changes_nothing_is_not_a_proposal(
        users, fake_llm, skills):
    """模型说不用改时如实失败，让管理员去否决，而不是记一笔"已生成方案"。"""
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    fake_llm["text"] = GOOD_REPLY.replace("建议分类\nlesson", "建议分类\ntool_gap")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])

    same = (skills / "skills" / SKILL / "SKILL.md").read_text(encoding="utf-8")
    fake_llm["text"] = "```\n" + same + "\n```"
    with pytest.raises(reflection.ReflectionError):
        reflection.build_proposal(rec["id"], f"skills/{SKILL}/SKILL.md")
    assert store.get_reflection(rec["id"])["proposal_state"] == "failed"


def test_applying_writes_the_file_and_commits(users, fake_llm, skills):
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    fake_llm["text"] = GOOD_REPLY.replace("建议分类\nlesson", "建议分类\ntool_gap")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])
    before = (skills / "skills" / SKILL / "SKILL.md").read_text(encoding="utf-8")
    fake_llm["text"] = before.replace("跑一下。", "跑之前先做野生型对照定标。")
    reflection.build_proposal(rec["id"], f"skills/{SKILL}/SKILL.md")

    out = reflection.apply_proposal(rec["id"])

    assert out["status"] == "applied"
    assert out["applied_at"]
    assert "野生型对照定标" in (skills / "skills" / SKILL / "SKILL.md").read_text(
        encoding="utf-8")
    log = _git_log(skills)
    assert f"反思 #{rec['id']}" in log, f"改了技能文件却没有提交：{log}"


def test_a_stale_proposal_is_refused_instead_of_clobbering_newer_changes(
        users, fake_llm, skills):
    """方案是基于某一版原文生成的，看 diff 到点确认之间文件可能被人改过。

    不比对哈希就写，等于拿旧方案覆盖掉别人的新改动，而 git 里看不出来是谁干的。
    """
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    fake_llm["text"] = GOOD_REPLY.replace("建议分类\nlesson", "建议分类\ntool_gap")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])
    before = (skills / "skills" / SKILL / "SKILL.md").read_text(encoding="utf-8")
    fake_llm["text"] = before.replace("跑一下。", "跑之前先做野生型对照定标。")
    reflection.build_proposal(rec["id"], f"skills/{SKILL}/SKILL.md")

    # 有人在管理员看 diff 的同时改了同一个文件
    target = skills / "skills" / SKILL / "SKILL.md"
    target.write_text(before + "\n别人新加的一行。\n", encoding="utf-8")

    with pytest.raises(store.Invalid):
        reflection.apply_proposal(rec["id"])
    assert "别人新加的一行" in target.read_text(encoding="utf-8"), \
        "方案过期时竟然还是写下去了"
    assert store.get_reflection(rec["id"])["status"] != "applied"


def test_an_unknown_skill_name_does_not_create_a_stray_directory(skills):
    """模型很可能写「protforge 蛋白设计」这种泛称。

    原样当目录名会写出一个不存在的技能目录，而这件事不会立刻暴露——
    下次任务加载技能时它静静地不在列表里。
    """
    assert reflection.normalize_skill("protforge-demo") == SKILL
    assert reflection.normalize_skill("Protforge-Demo") == SKILL
    assert reflection.normalize_skill("protforge 蛋白设计") == ""
    assert reflection.normalize_skill("") == ""
    assert not (skills / "skills" / "protforge 蛋白设计").exists()
