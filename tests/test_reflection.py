"""反思（rsi）的围栏。

**这些测试一个模型都不调。** `llm.chat_completion` 被换成一个返回固定文本的
桩：要验的是顺序、幂等与边界，不是模型答得好不好。真正调模型的路径一旦进了
测试套件，全量就会在离线机器上红，而且红的原因与被测代码无关。

**这里测的现在只剩两件事**：

1. **复盘留档**：任务进终态 → 生成一份正文文件 + 一行库记录。
   它不再喂给任何自动流程——注入提示词的是长期记忆（``server/memory.py``）。
2. **旧接口废得干净**：分类、落地、修改方案那几组函数保留着签名，
   但它们**什么都不做**：不写文件、不调模型、不碰技能仓库、不提交 git，
   而且**不抛异常**（管理端还有路由挂着它们）。

**技能目录必须隔离。** ``conftest.py`` 只把数据库指向 tmp_path，没有动
``PROTFORGE_WEB_SKILLS_DIR``——用默认值的话，跑一次测试就在真实的
``<技能仓>`` 里留下 ``references/lessons.md``，技能文件也可能被改掉。
所以下面 ``_skills`` 夹具先建一个临时技能仓库再指过去。

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
import memory      # noqa: E402
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

## 工具意见
- [技能 protforge-demo] 没有只改指定位点的开关，只能整链重跑
"""

#: 旧流程留下的那种正文：**带「经验」一节**。
#: 落地那条路（虽然已经废了）是从正文里读经验条目的，所以还留着它。
LESSON_REPLY = """\
## 这次实际做了什么
x

## 经验
- [技能 protforge-demo] 先跑一条野生型对照定标，再批量跑

## 工具意见
无
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

    def _fake(prompt, *, system="", max_completion_tokens=None,
              reasoning_effort=None, timeout=None):
        # 签名**显式写全**：chat_completion 改过参数名（max_tokens →
        # max_completion_tokens，还多了 reasoning_effort），桩写成 **kwargs
        # 就再也发现不了调用方用错了参数名——而那种错在真机上表现为
        # TypeError，整个反思悄无声息地标成 failed。
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
    # **不再有「建议分类」**：那份复盘只是存档，没有人再按它二选一。
    assert rec["suggested"] == "", "复盘记录里还带着一个分类决定"
    p = Path(rec["path"])
    assert p.is_file(), "反思正文没落成文件"
    assert p.parent == reflection.reflections_dir()
    body = p.read_text(encoding="utf-8")
    assert "先跑野生型对照定标" in body
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


def test_generation_refuses_to_write_when_the_data_root_moves_mid_flight(
        users, fake_llm, skills, tmp_path, monkeypatch):
    """模型调用期间换了数据根 → 整条作废，不落盘。

    这条是被真机抓出来的：跑一次**全量**测试之后，真实数据目录里的
    `task-1.md` 被重写了一遍，而逐个测试文件跑又一条都不写。

    机制是后台线程活过了用例：反思跑在 dispatch 的后台 worker 里，一次模型调用
    几十秒；等它真正写盘时，pytest 已经把 `PROTFORGE_WEB_DATA_ROOT` 撤销了。
    于是「查记录」看到的是临时库（这条没反思过），「写正文」写的是真实数据盘。

    所以入口要钉一次数据根，写之前再核一次。核的动作放在**模型调用之后、
    落盘之前**才有意义——早核的话那时候根还没变。
    """
    owner, _ = users
    tid = _finished_task(owner["id"])
    root = db.data_root()                       # 用例自己的临时根
    moved = tmp_path / "别的地方"

    def _swap_then_call(*a, **k):
        fake_llm["calls"] += 1
        monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(moved))
        return GOOD_REPLY

    monkeypatch.setattr(llm, "chat_completion", _swap_then_call)

    with pytest.raises(reflection.ReflectionError, match="数据根"):
        reflection.generate(tid)

    assert not (root / "reflections" / f"task-{tid}.md").exists(), \
        "数据根换掉之后仍然落了盘：写到了别的地方"
    assert not (moved / "reflections" / f"task-{tid}.md").exists(), \
        "换过去的那个根里也不该出现正文：它压根不该被写"
    # 但记录要留下，而且要标成 failed——否则界面上看着像「正在做」。
    rec = store.get_reflection_by_task(tid)
    assert rec is not None and rec["status"] == "failed", \
        "作废的反思没留记录/没标 failed：界面上会永远显示「正在做」"


def test_generation_is_idempotent_for_a_task_that_already_has_one(users, fake_llm, skills):
    """已有记录就直接返回，**正文一个字节都不许改**。

    幂等不只省一次模型调用：反思正文是管理员正在读的东西，
    重写一遍等于把人家读到一半的内容换掉，而界面上看不出发生过。
    """
    owner, _ = users
    tid = _finished_task(owner["id"])
    first = reflection.generate(tid)
    body = (db.data_root() / "reflections" / f"task-{tid}.md").read_bytes()

    second = reflection.generate(tid)
    assert second["id"] == first["id"], "第二次不该新建一条记录"
    assert (db.data_root() / "reflections" / f"task-{tid}.md").read_bytes() == body, \
        "已经有记录了还被重写了一遍"


def test_parser_ignores_the_quoted_raw_output_at_the_end_of_the_file():
    """落盘文件末尾那段「模型原始输出」是**引用**，不能再解析一遍。

    这条是被真机抓出来的：界面上每一条经验、每一条工具意见都出现了两遍，
    而落盘的 `task-N.md` 完全正确——重复是 GET 详情时把**渲染后的文件**
    再解析一次造成的。那份文件末尾带着一段代码围栏包着的模型原话，
    围栏里的小节标题被当成了真标题，于是内容被读了两遍。

    光看落盘文件永远看不出来，所以这里直接拿「渲染后的文件」当输入验。
    """
    raw = ("## 这次实际做了什么\n跑了一遍\n\n## 经验\n"
           "- [技能 protforge-demo] 先定标再批量\n\n## 建议分类\nlesson")
    rendered = reflection.render_md(
        {"id": 1, "title": "x", "status": "failed",
         "done_at": "2026-10-17T11:33:01"}, reflection.parse_reflection(raw), raw)
    assert "模型原始输出" in rendered, "渲染结果里没有原话附录：这条围栏失去意义"

    parsed = reflection.parse_reflection(rendered)
    texts = [i["text"] for i in parsed["lessons"]]
    assert texts == ["先定标再批量"], f"经验被读了两遍：{texts}"
    assert parsed["did"].count("跑了一遍") == 1, "「这次实际做了什么」被读了两遍"
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


def test_classification_no_longer_lands_anything(users, fake_llm, skills):
    """分类**不再落地任何东西**——它连「二选一」这道关口都不是了。

    以前分类只推状态（不写文件），落地是另一步。现在两步都没了：
    经验由干活的计算进程自己写进长期记忆，工具问题写进当天的日期文件。
    所以分类这一步既不该写长期记忆，也不该写工具问题文件。
    """
    owner, admin = users
    tid = _finished_task(owner["id"])
    rec = reflection.generate(tid)

    out = store.classify_reflection(rec["id"], "lesson", SKILL, admin["username"])

    assert out["status"] == "reviewed"
    assert out["kind"] == "lesson"
    assert not memory.long_term_path().exists(), \
        "分类这一步就把经验写出去了：经验的第一手写入者该是计算进程"
    assert not memory.tool_issues_dir().exists(), \
        "分类这一步把工具问题写进日期文件了"


def test_an_applied_reflection_cannot_be_reclassified(users, fake_llm, skills):
    """已落地的再改一次分类，等于把已经进了经验文件的内容悄悄换个身份，
    而没人去撤掉那一条。"""
    owner, admin = users
    tid = _finished_task(owner["id"])
    fake_llm["text"] = LESSON_REPLY
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


def test_landing_a_lesson_writes_nothing_any_more(users, fake_llm, skills):
    """落地这条路**整个废了**：既不写长期记忆，也不写技能的 references。

    经验的第一手写入者现在是干活的计算进程（它收工前用 write/edit 工具
    直接写）。这条旧接口还挂在管理端的路由上，所以它必须是**安全的空操作**：
    不抛异常（否则那些路由 500），但一个字都不写。
    """
    owner, admin = users
    import app_admin
    tid = _finished_task(owner["id"])
    fake_llm["text"] = LESSON_REPLY
    rec = reflection.generate(tid)
    rec = store.classify_reflection(rec["id"], "lesson", SKILL, admin["username"])

    out = app_admin._land_lesson(rec, admin["username"])

    assert not memory.long_term_path().exists(), \
        "落地把经验写进长期记忆了：这条路的权限被留下来了"
    skill_ref = skills / "skills" / SKILL / "references" / "lessons.md"
    assert not skill_ref.exists(), "落地还往技能目录里写东西"
    assert not reflection.lessons_path().exists(), "落地还在写旧的经验文件"
    # **空操作也要给个说法**：返回体里得有「这条已经不落地了」的痕迹。
    assert out.get("landed") is not None


def test_landing_refuses_when_the_review_has_no_lessons(users, fake_llm, skills):
    """「经验」一节里没有条目时**不许静悄悄地什么都不写**。

    返回成功而经验文件里没多东西，正是这个项目最熟的一种静默失效。
    """
    owner, admin = users
    import app_admin
    tid = _finished_task(owner["id"])
    fake_llm["text"] = (
        "## 这次实际做了什么\nx\n\n## 工具意见\n无")
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
        memory.append_memory(f"第 {i} 条经验，内容要足够长才能被上限砍掉" * 6,
                             skill=SKILL, source=f"任务 #{i}")
    block = reflection.lessons_block()
    assert block.strip()
    assert len(block) <= memory.MEMORY_BLOCK_MAX


def test_the_lessons_block_only_carries_the_most_recent_entries(skills):
    """全量留在文件里，提示词只带最近这些。

    一份塞满几百条旧经验的提示词会把真正的需求挤出注意力，
    而那条路是从**所有**任务身上收的税。
    """
    for i in range(memory.MEMORY_BLOCK_ITEMS + 6):
        memory.append_memory(f"经验-{i}", source=f"任务 #{i}")
    block = reflection.lessons_block()
    assert "经验-0" not in block
    assert f"经验-{memory.MEMORY_BLOCK_ITEMS + 5}" in block


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


# --- 工具意见 → 当日日期文件 --------------------------------------------------
#
# 以前这里是「工具意见 → 生成修改方案 → 看 diff → 应用 + git 提交」。
# 现在这条路**整个没了**：工具问题只按日期攒进
# ``<数据根>/tool-issues/<YYYY-MM-DD>.md``，由用户每周用 minimax code 统一修。
# 平台不生成 diff、不自动改技能仓库、不提交 git。


def test_a_tool_gap_is_not_landed_anywhere(users, fake_llm, skills):
    """工具意见**不再落地**：既不写进经验，也不改技能文件。"""
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])

    out = reflection.apply_proposal(rec["id"])

    assert out["ok"] is False and out["deprecated"] is True
    assert not memory.long_term_path().exists(), "工具意见被写进长期记忆了"
    assert not memory.tool_issues_dir().exists(), \
        "复盘这条路不该写工具问题文件：那是干活的计算进程自己写的"
    import app_admin
    fresh = store.get_reflection(rec["id"])
    with pytest.raises(store.Invalid):
        app_admin._land_lesson(fresh, admin["username"])


@pytest.mark.parametrize("target", [
    "../outside.md", "/etc/passwd", "C:/Windows/x.md", "notskills/x.md",
    "skills/../../escape.md", "",
])
def test_no_proposal_target_can_reach_the_plugin_repo(users, fake_llm, skills,
                                                     target):
    """**没有方案可生成**：不管传什么路径，都不碰技能仓库。

    以前这里是一处会写进别人仓库的代码，路径一旦能逃出去就等于任意写。
    现在这条路整个没了，所以传什么都是同一个结果：什么都不做。
    """
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])

    before = (skills / "skills" / SKILL / "SKILL.md").read_text(encoding="utf-8")
    out = reflection.build_proposal(rec["id"], target)

    assert out["ok"] is False and out["deprecated"] is True
    assert (skills / "skills" / SKILL / "SKILL.md").read_text(
        encoding="utf-8") == before, "生成方案这一步动了技能文件"
    assert not reflection.proposals_dir().exists() or \
        not list(reflection.proposals_dir().glob("*.patch")), \
        "生成方案这一步写了方案文件"


def test_generating_a_proposal_does_not_call_the_model(users, fake_llm, skills):
    """生成方案**不调模型**：这条路上一次模型调用都不该发生。

    以前这里要改预算、改思考档，就为了让它吐出一份完整的技能文件。
    现在工具问题只是「攒起来等用户统一修」，没什么要模型生成的。
    """
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])
    calls_after_generate = fake_llm["calls"]

    reflection.build_proposal(rec["id"], f"skills/{SKILL}/SKILL.md")

    assert fake_llm["calls"] == calls_after_generate, "生成方案还在调模型"


def test_a_failed_proposal_says_where_the_problems_go_now(users, fake_llm, skills):
    """失败原因要**说清新的去处**：去当天的工具问题文件。

    管理端那个按钮还在（删端点是主线的事），点下去要看到一条说得清原因的话，
    而不是「转一圈什么也没发生」。
    """
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])

    reflection.build_proposal(rec["id"], f"skills/{SKILL}/SKILL.md")

    got = store.get_reflection(rec["id"])
    assert got["proposal_state"] == "failed", got
    assert "工具问题" in got["proposal_error"], got
    assert str(memory.tool_issues_dir()) in got["proposal_error"], got


def test_applying_a_proposal_writes_nothing_and_commits_nothing(
        users, fake_llm, skills):
    """应用是**安全的空操作**：不改技能文件、不提交 git。

    平台不碰别人的仓库——这条边界现在由「什么都不做」来保证。
    """
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])
    before = (skills / "skills" / SKILL / "SKILL.md").read_text(encoding="utf-8")
    commits_before = _git_log(skills)

    out = reflection.apply_proposal(rec["id"])

    assert out["ok"] is False and out["deprecated"] is True
    assert (skills / "skills" / SKILL / "SKILL.md").read_text(
        encoding="utf-8") == before, "应用方案这一步改了技能文件"
    assert _git_log(skills) == commits_before, "应用方案竟然提交了 git"
    assert store.get_reflection(rec["id"])["status"] != "applied"


def test_the_retired_landing_and_proposal_helpers_are_safe_no_ops(
        users, fake_llm, skills):
    """旧接口被调用时**不抛异常**：管理端还有路由挂在它们上面。

    抛异常会让那些路由 500；留一个什么都不做的空壳，旧路由最多是
    「什么都没发生」，不会把整条链路带崩。
    """
    owner, admin = users
    tid = _finished_task(owner["id"], status="failed")
    rec = reflection.generate(tid)
    store.classify_reflection(rec["id"], "tool_gap", SKILL, admin["username"])

    assert reflection.append_lesson(SKILL, "一条经验", tid, admin["username"]) == ""
    assert reflection.append_skill_lesson(SKILL, "一条经验", tid) == ""
    assert reflection.build_proposal(rec["id"], f"skills/{SKILL}/SKILL.md")["ok"] is False
    assert reflection.apply_proposal(rec["id"])["ok"] is False

    assert not memory.long_term_path().exists(), "旧接口还在写长期记忆"
    assert not (skills / "skills" / SKILL / "references").exists(), \
        "旧接口还在往技能目录里写"


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
