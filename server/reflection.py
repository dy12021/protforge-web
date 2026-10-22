"""反思（rsi）：任务收工后让模型自己复盘，把可复用的东西沉淀下来。

**这条链路已经改过两次。** 以前是：

    任务进终态 → 巡检发现它还没有反思 → 调模型复盘 → 写成一个 md 文件 + 一行库记录
      → 管理员分类
          ├─ lesson    → 追加进全局经验文件（每任务都注入）+ 对应技能的 references
          └─ tool_gap  → 生成修改方案（真实 diff）→ 人工看过 → 应用 + git 提交

现在**经验的第一手写入者是干活的计算进程**：它收工前用 write / edit 工具
直接把做法写进长期记忆（:mod:`memory`），工具问题写进当天的日期文件。
「模型提出 → 管理员二选一 → 管理员点落地」那道关口没有了。

这里剩下的只有两件事：

1. **复盘留档**：任务进终态 → 巡检发现它还没复盘 → 调模型复盘 →
   写成一个 md 文件 + 一行库记录。**这份正文只是给人读的存档**，
   它不再喂给任何自动流程——注入提示词的是长期记忆，不是它。
2. **旧接口的弃用壳**：分类、落地、修改方案那几组函数**保留签名**
   （管理端的路由还挂在它们上面，删路由是主线的事），但它们什么都不做，
   调用它们不会抛异常、也不会写任何文件。

**正文住在文件里，库里只存指针与处理状态。** 读正文一律走 :func:`read_body`，
它是唯一入口——正文被抄一份进库的话，迟早有一次改只改到其中一份，
界面上写着「已沉淀」而文件里根本没有那条。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import db
import llm
import memory
import skills_index
import store

log = logging.getLogger(__name__)

REFLECTIONS_DIRNAME = "reflections"
PROPOSALS_DIRNAME = "proposals"
LESSONS_FILENAME = "lessons.md"

# 注入 agent 提示词的经验块上限。**这两个常量已经搬到 :mod:`memory`**：
# 现在注入的是长期记忆，不是这个模块里那份旧经验文件。
# 这里留着是为了不让引用它们的地方断掉（测试、别的模块）。
LESSONS_BLOCK_MAX = memory.MEMORY_BLOCK_MAX
LESSONS_BLOCK_ITEMS = memory.MEMORY_BLOCK_ITEMS

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
    """**旧的**全局经验文件（``<数据根>/lessons.md``）。

    **只读，不再写。** 新的长期记忆在 :func:`memory.long_term_path`。
    这里留着是因为迁移要读它（见 :func:`memory.migrate_legacy`）。
    """
    return memory.legacy_lessons_path()


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
计算活，现在活干完了（或者干脆没干成），你要写下这次的经过。

只依据给你的材料说事。材料里没有的事实不要编，也不要写成通用的套话
（「要注意环境配置」这种话对下一个任务一点用都没有）。

**这份复盘只是存档**：不会再有「建议分类」这一步，也不会有人来挑拣。
真正被下一个任务读到的经验，是干活的计算进程自己写进长期记忆的那一份。

严格按下面的格式回答，每节都留着，没内容就写「无」：

## 这次实际做了什么
一两段话。

## 顺利的地方
- 具体做法，别的任务可以直接照抄的那种

## 卡住的地方
- 具体的卡点、报错、反复试了几次

## 工具意见
- [技能 技能名] 哪个工具缺了什么 / 哪里别扭 / 会导致什么后果。
  写清楚到「缺哪个参数」「哪一步只能靠猜」「它给的输出会让人误解成什么」。
  没有就写「无」。"""

# 认识的小节标题。**「经验」和「建议分类」是旧流程留下的**：
# 现在的提示词不再要求这两节，但老正文的解析还得认得它们——
# 解析器要能读几个月前写下的那些存档。
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
    # 条目一律写成 ``- [技能 x] …``。**这一节现在只是存档**：
    # 真正被下一个任务读到的经验，是计算进程自己写进长期记忆的那一份。
    lines += [f"- [技能 {_skill_tag(i['skill'])}] {i['text']}" for i in parsed["lessons"]] \
        or ["（无）"]
    lines += ["", "## 工具意见"]
    lines += [f"- [技能 {_skill_tag(i['skill'])}] {i['text']}" for i in parsed["tool_gaps"]] \
        or ["（无）"]
    lines += ["", "## 建议分类",
              parsed["suggested"] or
              "（不再需要分类：经验由计算进程自己写进长期记忆，"
              "工具问题写进当天的日期文件）", ""]
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

    **这份复盘现在是存档**：它不再喂给任何自动流程——注入提示词的是长期记忆
    （:func:`lessons_block`），工具问题走按日期命名的文件。留着它是因为
    「这次到底发生了什么」仍然值得人读。
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
    # 实测就是这样：跑一次全量测试，`<数据盘根>\reflections\task-1.md`
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
    """要注入 agent 提示词的长期记忆块。**读的是新的长期记忆**，不是旧经验文件。

    调度器（``dispatcher._dispatch_prompt``）在拼需求之前调它，把这块放在
    需求前面——它是背景条件，不是补充说明。一条都没有时返回空串，
    调用方据此不拼那一段（给模型一个空壳只会让它以为这里本来没什么可参考）。

    容错在 :mod:`memory` 那一层做：文件不存在、编码坏了、格式坏了，
    都读成「没有内容」，不抛异常——这个块是**每个任务开工都要读**的，
    它坏一次就等于整批任务全炸。
    """
    return memory.memory_block()


def append_lesson(skill: str, text: str, task_id: int, decided_by: str) -> str:
    """**已弃用：什么都不写。** 返回空串。

    经验的第一手写入者现在是干活的计算进程：它收工前用 write / edit 工具
    直接写进长期记忆（:func:`memory.long_term_path`）。
    「管理员分类 → 点落地」那条路已经拆掉了。

    **为什么留着一个空壳而不是删掉**：管理端还有路由挂在它上面，
    删函数会让那些路由 500。留一个不抛异常、也不写任何东西的函数，
    旧路由最多是「什么都没发生」，不会把整条链路带崩。

    **遗留**：管理端那个「落地」端点仍会返回「已落地 N 条」，
    而实际上一个字都没写。那个端点该被删掉（清单在交付报告里）。
    """
    log.warning("append_lesson 已弃用：经验改由计算进程自己写进长期记忆，"
                "这条没有落地（任务 #%s）", task_id)
    return ""


def append_skill_lesson(skill: str, text: str, task_id: int) -> str:
    """**已弃用：什么都不写。** 返回空串。

    同 :func:`append_lesson`：技能的 references 不再由这条路径写。
    """
    log.warning("append_skill_lesson 已弃用：经验改由计算进程自己写进长期记忆，"
                "这条没有沉淀进技能目录（任务 #%s）", task_id)
    return ""


# --- 旧流程的弃用壳 ------------------------------------------------------------
#
# 以前这里是「工具意见 → 真 diff → 人工应用」：模型改一份技能文件，
# 服务端算 diff，人看过 diff 才应用，还带 git 提交。
#
# **这条路整个废掉了。** 工具问题现在只写进按日期命名的文件
# （``<数据根>/tool-issues/<YYYY-MM-DD>.md``），由用户每周用 minimax code
# 统一修一遍：平台不生成 diff、不自动改技能仓库、不提交 git。
#
# 下面的 build_proposal / apply_proposal **保留签名**（管理端还有路由挂着
# 它们，删函数会让那些路由 500），但它们什么都不做：不调模型、不写文件、
# 不碰技能仓库、不提交 git。

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", re.S)


def strip_fence(text: str) -> str:
    """模型很爱给整段加 ``` 包裹。剥掉，否则解析时小节标题认不出来。"""
    s = (text or "").strip()
    m = _FENCE_RE.match(s)
    if m:
        return m.group(1)
    if s.startswith("```"):
        # 只包了一半（常见的收尾被 max_tokens 截断）：去掉开头那行 fences
        s = re.sub(r"^```[a-zA-Z]*\s*\n", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s


def build_proposal(reflection_id: int, target: str) -> dict:
    """**已弃用：什么都不做。**

    工具问题不再生成修改方案：它写进按日期命名的文件
    （``<数据根>/tool-issues/<YYYY-MM-DD>.md``），由用户每周用 minimax code
    统一修一遍。

    **这里不调模型、不写方案文件、不碰技能仓库、不提交 git。**
    只把状态标成 failed 并写清原因——管理端那个「生成修改方案」的按钮
    还在（删端点是主线的事），点下去会看到一条说得清原因的话，
    而不是「转一圈什么也没发生」。

    **不抛异常**：旧路由还挂在它上面，抛异常会让那些路由 500。
    """
    msg = ("工具意见不再生成修改方案：工具问题请写进 "
           f"{memory.tool_issue_path(db.now_iso()[:10])}"
           "（按日期命名），由用户每周统一修。")
    log.warning("build_proposal 已弃用：%s", msg)
    try:
        store.set_proposal_state(reflection_id, "failed", msg)
    except Exception:                                     # noqa: BLE001
        log.exception("把反思 %s 的方案状态标成 failed 失败", reflection_id)
    return {"ok": False, "deprecated": True, "message": msg,
            "patch_path": "", "target": target, "diff": ""}


def _tool_gap_section(body: str) -> str:
    """从反思正文里抠出「工具意见」那一节。抠不到就把全文给模型看——
    总比让它对着空字符串编一个方案强。

    **只给迁移用**（把旧正文里的工具意见搬进按日期命名的文件）。
    """
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
    """**已弃用：什么都不做。**

    不写技能仓库、不提交 git、不改记录状态。工具问题改由用户每周用
    minimax code 统一修（见 :func:`build_proposal` 的说明）。

    **不抛异常**：旧路由还挂在它上面，抛异常会让那些路由 500。
    """
    msg = ("工具意见不再有「应用方案」这一步：平台不生成 diff、不改技能仓库、"
           "不提交 git。请把问题写进按日期命名的工具问题文件，"
           "由用户每周统一修。")
    log.warning("apply_proposal 已弃用：%s", msg)
    status = ""
    try:
        status = str(store.get_reflection(reflection_id).get("status") or "")
    except Exception:                                     # noqa: BLE001
        log.exception("读反思 %s 的状态失败", reflection_id)
    return {"ok": False, "deprecated": True, "message": msg,
            "status": status, "commit": ""}


def migrate_legacy_memory(*, dry_run: bool = False) -> dict:
    """把旧布局里的东西搬进新布局（幂等）。**入口在这里，实际活儿在
    :func:`memory.migrate_legacy`。**

    搬两样：旧的全局经验文件 ``<数据根>/lessons.md`` → 长期记忆
    ``<数据根>/memory/long-term.md``；旧反思正文里的「工具意见」一节 →
    工具问题文件 ``<数据根>/tool-issues/<收工日期>.md``。

    **不会在 import 时偷偷跑**：迁移要人明确调用一次（管理端或者命令行）。
    """
    return memory.migrate_legacy(dry_run=dry_run)
