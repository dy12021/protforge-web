"""反思（rsi）：任务收工后让模型自己复盘，把可复用的东西沉淀下来。

一条反思的生命周期：

    任务进终态 → 巡检发现它还没有反思 → 调模型复盘 → 写成一个 md 文件 + 一行库记录
      → 管理员分类
          ├─ lesson    → 追加进全局经验文件（每任务都注入）+ 对应技能的 references
          └─ tool_gap  → 生成修改方案（真实 diff）→ 人工看过 → 应用 + git 提交

**正文住在文件里，库里只存指针与处理状态。** 读正文一律走 :func:`read_body`，
它是唯一入口——正文被抄一份进库的话，迟早有一次分类只改到其中一份，
界面上写着「已沉淀」而经验文件里根本没有那条。

**模型写的分类只是建议。** 落地一律由管理员分类之后触发：模型说这是经验，
不代表它就配进下一次任务——没审过的自动沉淀会把一条错的经验喂给后面每一个任务。
"""
from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path

import db
import llm
import skills_index
import store

log = logging.getLogger(__name__)

REFLECTIONS_DIRNAME = "reflections"
PROPOSALS_DIRNAME = "proposals"
LESSONS_FILENAME = "lessons.md"

# 注入 agent 提示词的经验块上限。经验是无上限增长的，而每个任务的上下文不是：
# 一份塞满几百条旧经验的提示词会把真正的需求挤到注意力之外，
# 而且那条路是从**所有**任务身上收的税。全量留在文件里，提示词只带最近这些。
LESSONS_BLOCK_MAX = 2000
LESSONS_BLOCK_ITEMS = 12

# 组装复盘上下文时每段的截断长度。给 agent 的 log 可能几万行，全塞进去
# 会让这次复盘自己的上下文先炸掉——那就不是在复盘这次任务了。
BRIEF_CHARS = 4000
SUMMARY_CHARS = 4000
LOG_TAIL_CHARS = 6000
TIMELINE_CHARS = 4000
EVENTS_MAX = 200


class ReflectionError(RuntimeError):
    """反思这一条走不下去。调用方据此把记录标成 failed。"""


def reflections_dir() -> Path:
    return db.data_root() / REFLECTIONS_DIRNAME


def proposals_dir() -> Path:
    return reflections_dir() / PROPOSALS_DIRNAME


def lessons_path() -> Path:
    return db.data_root() / LESSONS_FILENAME


def skills_root() -> Path:
    """技能目录。**从 skills_index 拿**，不自己写一份默认值。"""
    return Path(os.environ.get("PROTFORGE_WEB_SKILLS_DIR")
                or skills_index.PROTFORGE_SKILLS_DIR)


def plugin_root() -> Path:
    """技能仓库根（skills 的上一级）。修改方案落在这里，git 提交也在这里。"""
    return skills_root().parent


# --- 提示词 -------------------------------------------------------------------

REFLECT_SYSTEM = """\
你在为「自动化计算平台」做任务复盘。这个平台把你派出去干蛋白/抗体设计一类的
计算活，现在活干完了（或者干脆没干成），你要写下这次的经验。

只依据给你的材料说事。材料里没有的事实不要编，也不要写成通用的套话
（「要注意环境配置」这种话对下一个任务一点用都没有）。

严格按下面的格式回答，每节都留着，没内容就写「无」：

## 这次实际做了什么
一两段话。

## 顺利的地方
- 具体做法，别的任务可以直接照抄的那种

## 卡住的地方
- 具体的卡点、报错、反复试了几次

## 经验
- [技能 技能名] 一句话说清下次该怎么做的具体做法。
  没有就写「无」。技能名写实际用到的 protforge-* 技能，不知道就写 [技能 通用]。

## 工具意见
- [技能 技能名] 哪个工具缺了什么 / 哪里别扭 / 会导致什么后果。
  这是要拿去改工具的，所以必须具体到「缺哪个参数」「哪一步只能靠猜」。
  没有就写「无」。

## 建议分类
只写一个词：lesson 或 tool_gap。倾向写经验就写 lesson，
主要是工具本身不够用才写 tool_gap。"""

_SECTION_HEADS = ("这次实际做了什么", "顺利的地方", "卡住的地方",
                  "经验", "工具意见", "建议分类")


def build_prompt(task: dict) -> str:
    """把一次任务的材料拼成复盘用的输入。

    材料给得越全，复盘越可能落到具体做法而不是套话。这里刻意把 agent 的
    stderr 尾巴也带上：失败任务的原因基本只在那里面，而 events 里只有一句
    「计算进程异常结束」。
    """
    tid = int(task["id"])
    parts = [f"## 任务\nid: {tid}\n标题: {task.get('title') or ''}\n"
             f"终态: {task.get('status')}\n创建: {task.get('created_at')}\n"
             f"收工: {task.get('done_at') or ''}"]

    brief = (task.get("brief") or "").strip()
    if brief:
        parts.append(f"## 需求\n{brief[:BRIEF_CHARS]}")

    summary = (task.get("summary") or "").strip()
    if summary:
        parts.append(f"## 计算进程自述的结果\n{summary[:SUMMARY_CHARS]}")

    err = (task.get("error") or "").strip()
    if err:
        parts.append(f"## 失败原因\n{err}")

    try:
        events = store.list_events(tid, limit=EVENTS_MAX)
    except Exception:                                      # noqa: BLE001
        events = []
    if events:
        lines = [f"- [{e.get('kind')}/{e.get('actor')}] {e.get('message') or ''}"
                 for e in events]
        joined = "\n".join(lines)
        if len(joined) > TIMELINE_CHARS:
            joined = joined[-TIMELINE_CHARS:]                 # 留尾部：那里才是收尾
        parts.append(f"## 时间线\n{joined}")

    tail = _agent_log_tail(tid)
    if tail:
        parts.append(f"## 计算进程的 stderr（末尾）\n```\n{tail}\n```")

    parts.append("请按规定的格式复盘这次任务。")
    return "\n\n".join(parts)


def _agent_log_tail(task_id: int) -> str:
    """agent stderr 的末尾。读不到就返回空串——**不让读日志失败拖垮整条反思**。"""
    try:
        p = db.data_root() / "logs" / f"agent-{int(task_id)}.err.log"
        if not p.is_file():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:                                      # noqa: BLE001
        log.debug("读任务 %s 的 agent 日志失败，跳过这一段", task_id)
        return ""
    return text[-LOG_TAIL_CHARS:].strip()


# --- 解析 ---------------------------------------------------------------------

_ITEM_RE = re.compile(r"^\s*[-*]\s*\[技能\s*([^\]]*)\]\s*(.*)$")
_HEAD_RE = re.compile(r"^\s*#{1,3}\s*(.+?)\s*[:：]?\s*$")


def parse_reflection(text: str) -> dict:
    """把模型那段文本切成结构化的一节节。

    **一节都认不出来就抛异常**，而不是返回一堆空串然后照常建记录：那种记录在
    管理员列表里看着像一条正常复盘，分类之后却什么也沉淀不了——
    「看起来成功、实际是空的」是本项目最熟的一种静默失效。

    认出来了但「经验」「工具意见」两节都为空是**合法的**：这次确实没什么
    可沉淀的，那也是一条有价值的记录，管理员看一眼就能否决。
    """
    sections: dict[str, list[str]] = {}
    current = None
    in_fence = False
    # **先拆整体围栏，再跳中途围栏。** 两件事方向相反，别混成一条：
    #
    #   · 模型爱给整段回复套一个 ``` ——那种要剥开照常解析，
    #     否则这次复盘白跑（模型的通病，不是内容问题）。
    #   · 落盘文件末尾那段「模型原始输出」也是围栏，但它是**引用**：
    #     渲染过的小节已经被读了一遍，再把围栏里的原话读一遍，
    #     界面上每一条就都出现两次。而文件本身完全正确，
    #     所以这个缺陷藏在「GET 详情时把渲染结果再解析一次」这一步里，
    #     光看落盘文件永远看不出来。
    #
    # 所以顺序是：整体围栏先拆（strip_fence 只在整段被包住时才真的剥），
    # 之后行走时才按围栏状态跳过**内层**的围栏。
    for raw_line in strip_fence(text).splitlines():
        if raw_line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEAD_RE.match(raw_line)
        if m:
            head = m.group(1).strip()
            # 模型可能写成「## 经验」也可能写「## 二、经验」，只认前缀对上那几个
            for known in _SECTION_HEADS:
                if head == known or head.endswith(known):
                    current = known
                    sections.setdefault(current, [])
                    break
            else:
                current = None
            continue
        if current is None:
            continue
        line = raw_line.rstrip()
        if line.strip():
            sections[current].append(line)

    if not sections:
        raise ReflectionError(
            "模型回的内容里一节标题都没认出来（要 "
            + "、".join(f"## {h}" for h in _SECTION_HEADS) + "）")

    def _para(name: str) -> str:
        return "\n".join(sections.get(name, [])).strip()

    def _bullets(name: str) -> list[str]:
        """没有 ``[技能 x]`` 前缀的普通条目。「顺利的地方」「卡住的地方」用这一种。

        **剥掉行首的 ``-``**：留着的话 render_md 再拼一个 ``- `` 就成了 ``- - ``。
        """
        out = []
        for line in sections.get(name, []):
            t = line.strip()
            t = re.sub(r"^[-*]\s*", "", t).strip()
            if t and t != "无":
                out.append(t)
        return out

    def _items(name: str) -> list[dict]:
        """带 ``[技能 x]`` 前缀的条目。「经验」「工具意见」用这一种。"""
        out = []
        for line in sections.get(name, []):
            if not line.strip().startswith(("-", "*")):
                continue                                   # 「无」这类说明不算一条
            m = _ITEM_RE.match(line)
            if m:
                skill, body = m.group(1).strip(), m.group(2).strip()
            else:
                skill, body = "", line.lstrip("-* ").strip()
            if body in ("", "无", "none", "None"):
                continue
            out.append({"skill": skill, "text": body})
        return out

    verdict = _para("建议分类").strip().split()
    suggested = verdict[0].strip() if verdict else ""
    if suggested not in db.REFLECTION_KINDS:
        suggested = ""

    return {
        "did": _para("这次实际做了什么"),
        "went_well": _bullets("顺利的地方"),
        "stuck": _bullets("卡住的地方"),
        "lessons": _items("经验"),
        "tool_gaps": _items("工具意见"),
        "suggested": suggested,
    }


def parse_or_none(text: str) -> dict | None:
    """解析不出来时返回 None 而不是抛。

    **只给「读旧记录」用**（比如管理员打开一条几个月前的复盘）。生成那条
    路径上必须用会抛的 :func:`parse_reflection`：静默的空结果会让一条没解析
    出来的记录看起来像一条正常复盘。
    """
    try:
        return parse_reflection(text)
    except ReflectionError:
        return None


def suggested_skill(parsed: dict | None) -> str:
    """从模型给的两节里挑一个技能名出来，**只当默认值用**。

    优先「工具意见」那节：反馈指向的工具通常就是该改的那个技能，
    而「经验」那节有时是跨技能的通用做法。
    """
    if not parsed:
        return ""
    for key in ("tool_gaps", "lessons"):
        for item in parsed.get(key) or []:
            name = normalize_skill(item.get("skill"))
            if name:
                return name
    return ""


# --- 落文件 -------------------------------------------------------------------

def render_md(task: dict, parsed: dict, raw: str) -> str:
    """渲染成那份独立文件。人能直接读，管理员在界面上看到的就是它。"""
    tid = int(task["id"])
    lines = [
        f"# 反思 · 任务 #{tid} · {task.get('title') or ''}",
        "",
        f"- 终态：{task.get('status')}",
        f"- 收工时间：{task.get('done_at') or ''}",
        f"- 模型：{llm.model_id()}",
        f"- 生成时间：{db.now_iso()}",
        "",
        "## 这次实际做了什么",
        parsed["did"] or "（模型没写）",
        "",
        "## 顺利的地方",
    ]
    lines += [f"- {t}" for t in parsed["went_well"]] or ["（无）"]
    lines += ["", "## 卡住的地方"]
    lines += [f"- {t}" for t in parsed["stuck"]] or ["（无）"]
    lines += ["", "## 经验"]
    # 条目一律写成 ``- [技能 x] …``：**与模型自己的输出、以及 _land_lesson
    # 读的那套格式完全一致**。这里写成 `- [x]`，落地那一步的正则就一条都配不上，
    # 表现是「管理员点了落地，经验一条也没写进去」。
    lines += [f"- [技能 {_skill_tag(i['skill'])}] {i['text']}" for i in parsed["lessons"]] \
        or ["（无）"]
    lines += ["", "## 工具意见"]
    lines += [f"- [技能 {_skill_tag(i['skill'])}] {i['text']}" for i in parsed["tool_gaps"]] \
        or ["（无）"]
    lines += ["", "## 建议分类", parsed["suggested"] or "（模型没给建议，等管理员定）", ""]
    lines += ["---", "", "## 模型原始输出", "", "```", (raw or "").strip(), "```", ""]
    return "\n".join(lines)


def _skill_tag(name: str) -> str:
    return (name or "").strip() or "通用"


def body_path(task_id: int) -> Path:
    return reflections_dir() / f"task-{int(task_id)}.md"


def read_body(path: str) -> str:
    """读反思正文。读不出来抛 Invalid——**不返回空串**：
    空串会让界面上那块显示成「（正文丢失）」和「模型什么都没写」一样，
    而这两种情况的处置完全不同（前者要重跑，后者正常否决）。"""
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        raise store.Invalid(f"读不到反思正文：{p.name}（{exc}）") from exc


def discard_body(path: str) -> bool:
    """删掉一份反思正文。**重跑时用**：那份存根写的是「生成失败」，留着只会
    让下一个人以为这条还是坏的。删不掉不算错——下一次生成会写同一个路径。"""
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        log.warning("删不掉反思正文 %s，下次生成会覆盖它", path)
        return False


# --- 生成 ---------------------------------------------------------------------

def generate(task_id: int) -> dict:
    """给一条已进终态的任务做复盘。返回那条反思记录。

    幂等：已经有记录就直接返回，不重复调模型。巡检每 tick 都可能扫到同一条。
    """
    existing = store.get_reflection_by_task(task_id)
    if existing is not None:
        return existing

    try:
        task = store.get_task(int(task_id), is_admin=True)
    except store.StoreError as exc:
        raise ReflectionError(f"读不到任务：{exc}") from exc
    if task.get("status") not in db.TERMINAL_STATUSES:
        raise ReflectionError(
            f"任务还是「{task.get('status')}」，没到终态就复盘等于复盘半成品")

    # 数据根在这里**钉死**，不是等到写的时候才现读。
    #
    # 一次模型调用要几十秒，而这条反思是跑在**后台线程**里的：几十秒里进程里的
    # 环境变量完全可能被人换掉——pytest 的 monkeypatch 在用例结束时撤销就是
    # 最典型的一种。那时候 `db_path()` 指向临时库（于是"这条还没反思过"），
    # 而 `data_root()` 指向真实数据盘，于是正文被写进**线上目录**。
    # 实测就是这样：跑一次全量测试，`data\reflections\task-1.md`
    # 被重写了一遍，而逐个文件跑又一条都不写——因为要整个套件一起跑，
    # 前一个用例起的后台线程才会活到后一个用例里去。
    #
    # 所以：入口钉一次，写之前再核一次。变了就当这条失败——
    # 宁可少一条复盘，也不要写到一半换了地方。
    root = db.data_root()

    p = body_path(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 先建记录再调模型：模型要跑几十秒，这段时间里这条任务已经被别的巡检
    # 扫到过好几次，UNIQUE(task_id) 挡住第二条，但先落记录能让「正在做」
    # 在界面上看得见，而不是看着像漏了。
    rec = store.create_reflection(int(task_id), str(p))

    try:
        raw = llm.chat_completion(build_prompt(task), system=REFLECT_SYSTEM,
                                  max_completion_tokens=8192)
        parsed = parse_reflection(raw)
    except (llm.LLMError, ReflectionError) as exc:
        try:
            p.write_text(
                f"# 反思 · 任务 #{int(task_id)}（生成失败）\n\n"
                f"- 失败原因：{exc}\n- 时间：{db.now_iso()}\n",
                encoding="utf-8")
        except OSError:
            log.warning("任务 %s 的反思正文都写不下去", task_id)
        store.mark_reflection_failed(rec["id"], str(exc))
        raise

    # 见上：飞行途中换了数据根就整条作废，不落盘。
    if db.data_root() != root:
        msg = (f"生成期间数据根从 {root} 变成了 {db.data_root()}，"
               "为免写到别处去，这条反思作废")
        log.warning("任务 %s 的%s", task_id, msg)
        try:
            store.mark_reflection_failed(rec["id"], msg)
        except Exception:                                  # noqa: BLE001
            log.exception("把任务 %s 的反思标成 failed 失败", task_id)
        raise ReflectionError(msg)

    try:
        p.write_text(render_md(task, parsed, raw), encoding="utf-8")
    except OSError as exc:
        store.mark_reflection_failed(rec["id"], f"写反思正文失败：{exc}")
        raise ReflectionError(f"写反思正文失败：{exc}") from exc

    store.set_reflection_content(rec["id"], parsed["suggested"])
    return store.get_reflection(rec["id"])


# --- 经验落地 -----------------------------------------------------------------

def normalize_skill(name: str) -> str:
    """把模型给的技能名对到真实存在的技能上。

    对不上就退回「通用」而不是报错：模型很可能写「protforge 蛋白设计」
    这种泛称，把它原样当目录名会写出一个不存在的技能目录，
    而这件事不会立刻暴露——下次任务加载技能时它静静地不在列表里。
    """
    raw = (name or "").strip()
    if not raw or raw in ("通用", "general", "none"):
        return ""
    known = skills_index.scan()
    if raw in known:
        return raw
    # 去掉前缀/大小写差异再试一次
    for k in known:
        if k.lower() == raw.lower() or k.lower() == raw.lower().replace("protforge-", ""):
            return k
    return ""


def lessons_block() -> str:
    """要注入 agent 提示词的经验块。文件不在或没内容时返回空串。"""
    p = lessons_path()
    if not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        log.warning("读经验文件失败，本轮不带经验")
        return ""
    items = [ln.strip() for ln in text.splitlines()
             if ln.strip().startswith("- ")]
    if not items:
        return ""
    items = items[-LESSONS_BLOCK_ITEMS:]
    block = "\n".join(items)
    return block[:LESSONS_BLOCK_MAX]


def append_lesson(skill: str, text: str, task_id: int, decided_by: str) -> str:
    """把一条经验写进全局经验文件，返回写进去的那一行。

    全局文件是**唯一每任务都注入**的那一份，所以它必须自己站得住：
    每条都带来源（哪个任务、第几条），将来有人觉得这条不对，能顺着它找到当时的复盘。
    """
    line = f"- {text.strip()}（来自任务 #{int(task_id)}，{decided_by} 于 {db.now_iso()[:10]} 归档）"
    p = lessons_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.is_file():
        p.write_text(
            "# 已沉淀的经验\n\n"
            "**这一份由管理员分类「经验」后自动追加，不要手工改。**\n"
            "每条来自哪次任务复盘都写在行尾，删条目请连同对应反思一起处理。\n\n",
            encoding="utf-8")
    with p.open("a", encoding="utf-8") as fh:
        if not line.startswith("- "):
            line = "- " + line
        fh.write(line + "\n")
    return line


def append_skill_lesson(skill: str, text: str, task_id: int) -> str:
    """同时沉淀进技能自己的 references。**返回实际写到的路径，没写成功返回空串。**

    这一层是「知识待在该在的地方」的那一层，不是每任务都读的那一层——
    技能没被加载时它就不在上下文里。它写失败不许影响全局经验那一步：
    那一步才是保证生效的那一步。
    """
    name = normalize_skill(skill)
    if not name:
        return ""
    ref_dir = skills_root() / name / "references"
    try:
        ref_dir.mkdir(parents=True, exist_ok=True)
        p = ref_dir / "lessons.md"
        if not p.is_file():
            p.write_text(
                "# 经验沉淀\n\n"
                "由自动化计算平台的反思流程写入（管理员分类为「经验」之后）。\n"
                "来源见每条行尾的任务号，要撤销请回到对应反思处理。\n\n",
                encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"- {text.strip()}（来自任务 #{int(task_id)}）\n")
        return str(p)
    except OSError:
        log.warning("沉淀经验到技能 %s 失败，全局经验那一步已写入，不受影响", skill)
        return ""


# --- 修改方案（工具意见 → 真 diff → 人工应用）-------------------------------

PROPOSE_SYSTEM = """\
你在按一条「工具不够用」的反馈修改一个技能文件。

规则：
1. 只输出修改后的**完整文件内容**，不要输出任何解释、不要用 ``` 包裹、
   不要只给片段。输出会被拿去和原文逐行 diff。
2. 改动只针对反馈里说的那一点，不要顺手重构、不要改格式、不要加别的功能。
3. 不确定的地方保持原样。宁可少改，不要把一个能用的技能改坏。
"""

_PROPOSE_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", re.S)


def strip_fence(text: str) -> str:
    """模型很爱给整段加 ``` 包裹。剥掉，否则 diff 会显示成「整份文件换掉了」。"""
    s = (text or "").strip()
    m = _PROPOSE_FENCE.match(s)
    if m:
        return m.group(1)
    if s.startswith("```"):
        # 只包了一半（常见的收尾被 max_tokens 截断）：去掉开头那行 fences
        s = re.sub(r"^```[a-zA-Z]*\s*\n", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def diff_for(target: str, before: str, after: str) -> str:
    """真 diff。用 difflib 算，不采信模型自己写的 diff。

    模型直接产出 unified diff 时几乎总会漏 hunk 头或对错行号，
    而那种 diff 打补丁时表现是「部分应用、剩下几块静默不动」。
    让它产出**完整新内容**、我们自己算 diff，看到的和落盘的就一定是同一份。
    """
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{target}", tofile=f"b/{target}", n=3))


def build_proposal(reflection_id: int, target: str) -> dict:
    """为一条「工具意见」生成修改方案。

    ``target`` 是**技能仓库内的相对路径**，例如
    ``skills/protforge-proteinmpnn/SKILL.md``。不接受绝对路径也不接受 ``..``：
    这是唯一一处会写进别人仓库的代码，路径一旦能逃出去就等于任意写。
    """
    rec = store.get_reflection(reflection_id)
    if rec["kind"] != "tool_gap":
        raise store.Invalid("这条还没被分类为「工具意见」，生成什么方案？")
    rel = (target or "").strip().replace("\\", "/")
    if not rel.startswith("skills/"):
        raise store.Invalid("要改的必须是 skills/ 下的文件")
    root = plugin_root().resolve()
    abs_target = (root / rel).resolve()
    if not str(abs_target).startswith(str(root) + "\\") and \
            not str(abs_target).startswith(str(root) + "/"):
        raise store.Invalid("要改的路径跑出技能仓库了")
    if not abs_target.is_file():
        raise store.Invalid(f"技能仓库里没有这个文件：{rel}")

    before = abs_target.read_text(encoding="utf-8")
    body = read_body(rec["path"])
    feedback = _tool_gap_section(body)

    prompt = (
        f"技能仓库：{root}\n要改的文件：{rel}\n\n"
        f"## 这条反馈\n{feedback}\n\n"
        f"## 文件现在的内容\n```\n{before}\n```\n\n"
        "请输出修改后的完整文件内容。")
    # 先标「正在生成」再调模型：这一次可能要一两分钟，管理员那边看到的是
    # 「正在生成」而不是一个转不出来的按钮。
    store.set_proposal_state(reflection_id, "running")
    try:
        # **思考档位 low + 预算 32k。** 这两项都不是"调优"，是"能不能跑通"：
        # 默认档实测思考了 9,441 tokens（43k 字符）、花 165 秒，而 8192 的预算
        # 正好被思考吃光 → finish_reason=length → 正文一个字都没有 →
        # 「模型回的不是预期的 chat 结构」。改档位还顺带把改动收住了：
        # 同一份反馈，默认档改出 246 行（把 46 行的文件重写了一遍），
        # low 档 79 行——提示词里「不要顺手重构」那条守住了。
        raw = llm.chat_completion(prompt, system=PROPOSE_SYSTEM,
                                  max_completion_tokens=32768,
                                  reasoning_effort="low")
    except Exception as exc:
        # **失败原因必须留下来**：原来只把状态标成 failed，原因只进日志。
        # 界面上管理员看到的是「生成失败」四个字，没有别的——他连
        # 「模型调不通」还是「路径写错了」都分不出来。
        store.set_proposal_state(reflection_id, "failed", str(exc))
        raise
    after = strip_fence(raw)
    if not after.strip():
        store.set_proposal_state(reflection_id, "failed", "模型回了空的修改后内容")
        raise ReflectionError("模型回了空的修改后内容")
    if after == before:
        store.set_proposal_state(
            reflection_id, "failed",
            "模型说不用改——那这条就不是「工具不够用」，该否决")
        raise ReflectionError("模型说不用改——那这条就不是「工具不够用」，该否决")

    patch = diff_for(rel, before, after)
    try:
        proposals_dir().mkdir(parents=True, exist_ok=True)
        pf = proposals_dir() / f"refl-{int(reflection_id)}.patch"
        body_out = proposals_dir() / f"refl-{int(reflection_id)}.new"
        pf.write_text(patch, encoding="utf-8")
        body_out.write_text(after, encoding="utf-8")
    except OSError as exc:
        store.set_proposal_state(reflection_id, "failed")
        raise ReflectionError(f"写方案文件失败：{exc}") from exc

    store.set_reflection_proposal(reflection_id, str(pf), rel, sha256_text(before))
    return {"diff": patch, "patch_path": str(pf), "target": rel,
            "base": sha256_text(before), "new_path": str(body_out)}


def _tool_gap_section(body: str) -> str:
    """从反思正文里抠出「工具意见」那一节。抠不到就把全文给模型看——
    总比让它对着空字符串编一个方案强。"""
    lines = body.splitlines()
    out, on = [], False
    for ln in lines:
        s = ln.strip()
        if s.startswith("#"):
            on = s.lstrip("# ").strip() == "工具意见"
            continue
        if on and s:
            out.append(ln)
    return "\n".join(out).strip() or body


def apply_proposal(reflection_id: int) -> dict:
    """把方案落进技能仓库，并 git 提交。

    **落盘前校验原文哈希**：方案是基于某一版原文生成的，管理员看diff到点确认
    中间技能文件可能被人改过。不校验就写，等于拿一份旧方案覆盖掉别人的新改动，
    而 git 里看不出来是谁干的。
    """
    rec = store.get_reflection(reflection_id)
    if rec["kind"] != "tool_gap":
        raise store.Invalid("只有「工具意见」才有修改方案可应用")
    if not rec["proposal"]:
        raise store.Invalid("还没有生成修改方案")
    if rec["status"] == "applied":
        raise store.Invalid("这条已经应用过了")

    root = plugin_root()
    abs_target = (root / rec["proposal_target"]).resolve()
    if not str(abs_target).startswith(str(root) + "\\") and \
            not str(abs_target).startswith(str(root) + "/"):
        raise store.Invalid("方案的目标路径跑出技能仓库了")

    before = abs_target.read_text(encoding="utf-8")
    if sha256_text(before) != rec["proposal_base"]:
        raise store.Invalid(
            "技能文件在方案生成之后被改过了。方案是基于旧版本生成的，"
            "直接应用会把别人的改动覆盖掉——请重新生成方案。")
    after = Path(
        Path(rec["proposal"]).with_suffix(".new")).read_text(encoding="utf-8")

    before_text = before
    abs_target.write_text(after, encoding="utf-8")
    try:
        commit = _git_commit(root, rec["proposal_target"], int(reflection_id))
    except Exception:
        abs_target.write_text(before_text, encoding="utf-8")   # 提交失败要还原
        raise
    out = store.mark_reflection_applied(reflection_id)
    out["commit"] = commit
    return out


def _git_commit(root: Path, rel: str, reflection_id: int) -> str:
    """在技能仓库里提交这一处改动。**不是仓库就明说失败**，不假装提交过了。

    ``encoding="utf-8"`` 不能省：commit message 是中文，而 ``text=True`` 不给
    encoding 时走的是本机 ANSI 码页（这台机是 gbk），git 那边的 UTF-8 字节会在
    解码线程里抛 UnicodeDecodeError——而那个异常发生在 reader 线程里，
    提交命令本身的返回码照样是 0，于是**提交失败被报成提交成功**。
    """
    if not (root / ".git").exists():
        raise store.Invalid(f"{root} 不是 git 仓库，应用了改动但没法提交——"
                            "请手工确认并提交")
    run_git = dict(cwd=str(root), capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=120)
    subprocess.run(["git", "add", "--", rel], **run_git)
    msg = f"反思 #{reflection_id}：按工具意见修改 {rel}"
    p = subprocess.run(["git", "commit", "-m", msg], **run_git)
    if p.returncode != 0:
        raise store.Invalid(f"git 提交失败：{(p.stderr or p.stdout or '').strip()[:300]}")
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], **run_git)
    return (head.stdout or "").strip()
