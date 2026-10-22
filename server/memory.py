"""长期记忆与工具问题：数据层。

这里有两件事，各有一个落点，都在**数据根目录**下面（``db.data_root()``，
不写死任何本机路径）：

* **长期记忆** ``<数据根>/memory/long-term.md``
  可复用的做法。**第一手写入者是干活的计算进程**——它在收工前用
  ``write`` / ``edit`` 工具直接写进来。服务端只管三件事：给出路径、
  把它限量注入提示词、每天复核一遍。
* **工具问题** ``<数据根>/tool-issues/<YYYY-MM-DD>.md``
  工具本身不够用的地方（缺参数、只能靠猜、会误导人）。
  **只收集，不自动修**：用户每周用 minimax code 统一修一遍。
  所以这里只追加，**不生成 diff、不碰技能仓库、不提交 git**。

**没有「二分」这道关口了。** 以前是「模型提出 → 管理员在经验/工具意见之间
二选一 → 管理员点落地」，中间那道人工停顿是给「模型说的」兜底的。
现在经验的来源就是干活的那个计算进程，它自己知道哪条做法值得留下；
工具问题也不再需要人替它决定属于哪一类，只要按日期攒起来就够了。

**读长期记忆必须容错。** 文件不存在、编码坏了、格式坏了，都要读成
「没有内容」而不是抛异常——这个文件是**每个任务开工都要读**的，
它坏一次就等于整批任务全炸。

**注入必须限量。** 长期记忆无上限增长，而每个任务的上下文不是：
一份塞满几百条旧经验的提示词会把真正的需求挤到注意力之外，
而且那份税是从**所有**任务身上收的。全量留在文件里，提示词只带最近这些。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import db
import guards
import llm

log = logging.getLogger(__name__)

# --- 常量 ---------------------------------------------------------------------

MEMORY_DIRNAME = "memory"
LONG_TERM_FILENAME = "long-term.md"
TOOL_ISSUES_DIRNAME = "tool-issues"
REVIEW_DIRNAME = "review"
REVIEW_STATE_FILENAME = "review-state.json"
#: 复核进程跑一次的产物（它自己写的复核记录）。
REVIEW_REPORT_DIRNAME = "reviews"

# 旧布局。**只读**：迁移用，迁移之后也不再往里写。
LEGACY_LESSONS_FILENAME = "lessons.md"
LEGACY_REFLECTIONS_DIRNAME = "reflections"
LEGACY_PROPOSALS_DIRNAME = "proposals"

# 注入 agent 提示词的长期记忆块上限。理由见模块开头那段。
MEMORY_BLOCK_MAX = 2000
MEMORY_BLOCK_ITEMS = 12

# 复核进程一次最多跑多久（秒）。它要联网查证，比普通任务慢。
REVIEW_TIMEOUT = 1800.0

#: 一条经验的时效性标注。写了这一句的，复核进程**必须**去查证它现在还成不成立。
VOLATILE_MARK = "可能有时效性"
#: 稳定做法（不会随时间失效）的标注。
STABLE_MARK = "稳定"

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: 一条经验的行首：``- [技能 xxx] 正文``。
_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[技能\s*([^\]]*)\]\s*(.*)$")
#: 行尾的元信息：``（来源：任务 #12；时效性：稳定）``。
_SOURCE_RE = re.compile(r"来源[:：]\s*([^；;）)]*)")
_VOLATILE_RE = re.compile(r"时效性[:：]\s*([^；;）)]*)")
#: 行尾那一对括号（元信息就装在里面）。
_TAIL_RE = re.compile(r"（([^（）]*)）\s*$")

LONG_TERM_HEADER = """\
# 长期记忆

这一份由**干活的计算进程**直接写入：任务收工前，它用 write / edit 工具把
可复用的做法追加到下面。全量留在这里，每个任务开工时只带最近几条。

每条一行，格式固定：

- [技能 技能名] 具体做法，别的任务能照抄。（来源：任务 #12；时效性：稳定）

- `[技能 技能名]`：这条属于哪个技能；跨技能就写「通用」。
- `来源`：这条是从哪次任务、哪个技能里得到的，要能顺着它找到出处。
- `时效性`：`稳定`（不会随时间失效）或 `可能有时效性`
  （依赖当下的环境、版本、路径、外部服务，过一阵可能不成立）。
  标了「可能有时效性」的会被每天的复核进程查证。

**不要写跟这一次任务只相关的东西**（这次的具体参数、这次的中间文件名），
也不要写套话（「要注意环境配置」这种话对下一个任务一点用都没有）。
"""

TOOL_ISSUE_HEADER = """\
# 工具问题 · {day}

这一份由**干活的计算进程**直接追加：发现工具缺参数、某一步只能靠猜、
或者某个工具会误导人时，就把问题写在这里。

**只收集，不自动修**：这些条目由用户每周用 minimax code 统一修一遍。
所以只写清楚「哪里不对」，不要自己去改技能仓库。
"""


# --- 路径 ---------------------------------------------------------------------

def memory_dir() -> Path:
    """长期记忆目录。**每次现读**数据根（测试会改环境变量）。"""
    return db.data_root() / MEMORY_DIRNAME


def long_term_path() -> Path:
    """长期记忆文件。绝对路径，会写进 agent 的提示词里。"""
    return memory_dir() / LONG_TERM_FILENAME


def tool_issues_dir() -> Path:
    """工具问题目录。"""
    return db.data_root() / TOOL_ISSUES_DIRNAME


def tool_issue_path(day: str) -> Path:
    """某一天的工具问题文件。文件名就是日期（``2026-10-22.md``）。"""
    d = (day or "").strip()
    if not _DAY_RE.match(d):
        raise ValueError(f"工具问题的日期格式不对：{day!r}（要 YYYY-MM-DD）")
    return tool_issues_dir() / f"{d}.md"


def review_dir() -> Path:
    """复核进程的工作目录（每次复核一个子目录，跟着日期走）。"""
    return db.data_root() / REVIEW_DIRNAME


def review_report_path(day: str) -> Path:
    """复核记录文件。复核进程自己写它（用 write 工具）。"""
    d = (day or "").strip()
    if not _DAY_RE.match(d):
        raise ValueError(f"复核记录的日期格式不对：{day!r}（要 YYYY-MM-DD）")
    return memory_dir() / REVIEW_REPORT_DIRNAME / f"{d}.md"


def review_state_path() -> Path:
    """"每天一次"的判定存这里。**落盘**，不靠进程内存。"""
    return memory_dir() / REVIEW_STATE_FILENAME


# 旧布局。迁移用，只读。
def legacy_lessons_path() -> Path:
    """旧的全局经验文件（``<数据根>/lessons.md``）。**只读，不再写。**"""
    return db.data_root() / LEGACY_LESSONS_FILENAME


def legacy_reflections_dir() -> Path:
    return db.data_root() / LEGACY_REFLECTIONS_DIRNAME


def legacy_proposals_dir() -> Path:
    return legacy_reflections_dir() / LEGACY_PROPOSALS_DIRNAME


# --- 读长期记忆 ---------------------------------------------------------------

def read_long_term() -> str:
    """读长期记忆全文。**读不出来就是没有内容**，不抛异常。

    这个文件是每个任务开工都要读的：它坏一次（编码坏了、被手工改坏了、
    正在被写）就等于整批任务全炸。而「没有经验」和「读不出经验」在效果上
    是一样的——都不注入任何东西。
    """
    p = long_term_path()
    try:
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("读长期记忆失败，本轮不带记忆：%s", p)
        return ""


def _split_tail(body: str) -> tuple[str, str, str]:
    """把行尾的元信息摘出来，返回（正文，来源，时效性）。

    约定：元信息永远装在**最后那一对圆括号**里。正文里若也有括号，
    只有最后一对被当成元信息——这条约定是写入端（:func:`format_entry`）
    与解析端共同遵守的，写出来和读回来必须对得上。

    没有元信息就整个当正文：解析不出来的东西宁可原样留着，
    也不要猜——猜出来的条目会被注入每个任务的提示词。
    """
    m = _TAIL_RE.search(body or "")
    if not m:
        return (body or "").strip(), "", ""
    inner = m.group(1)
    src = _SOURCE_RE.search(inner)
    vol = _VOLATILE_RE.search(inner)
    return ((body[:m.start()]).strip(),
            src.group(1).strip() if src else "",
            vol.group(1).strip() if vol else "")


def parse_entries(text: str) -> list[dict]:
    """把长期记忆正文切成一条条。

    **只认行首的 ``- [技能 x] …``**。解析不了的行直接丢掉，不猜——
    猜出来的条目会被注入提示词，而那是每个任务都要读的东西。

    ``text`` 是**去掉行尾元信息之后**的正文；``raw`` 是原样那一行
    （注入提示词时用原样那一行，好让人看得出它的来源和时效性）。
    """
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip().startswith(("-", "*")):
            continue
        m = _ENTRY_RE.match(line)
        if not m:
            continue
        skill = m.group(1).strip()
        body = m.group(2).strip()
        if not body or body == "无":
            continue
        core, src, vol = _split_tail(body)
        if not core or core == "无":
            continue
        out.append({
            "skill": skill,
            "text": core,
            "source": src,
            "volatility": vol,
            "volatile": VOLATILE_MARK in body,
            "raw": line,
        })
    return out


def read_entries() -> list[dict]:
    """读长期记忆并切成条目。容错同 :func:`read_long_term`。"""
    return parse_entries(read_long_term())


def memory_block() -> str:
    """要注入 agent 提示词的长期记忆块。文件不在或没内容时返回空串。

    **限量两重**：条数（最近的 ``MEMORY_BLOCK_ITEMS`` 条）与字数
    （``MEMORY_BLOCK_MAX``）。全量留在文件里，提示词只带最近这些——
    一份塞满几百条旧经验的提示词会把真正的需求挤到注意力之外。
    """
    items = read_entries()
    if not items:
        return ""
    items = items[-MEMORY_BLOCK_ITEMS:]
    block = "\n".join(i["raw"] for i in items)
    return block[:MEMORY_BLOCK_MAX]


# --- 写长期记忆 ---------------------------------------------------------------

def format_entry(text: str, *, skill: str = "", source: str = "",
                 volatile: bool = False) -> str:
    """把一条经验拼成规定格式的那一行。

    格式固定成 ``- [技能 x] 正文（来源：…；时效性：…）``：
    服务端解析、注入块截断、复核进程查证，全靠这几个固定的标记。
    """
    body = (text or "").strip()
    if not body:
        return ""
    tag = (skill or "").strip() or "通用"
    tail = []
    if source:
        tail.append(f"来源：{source.strip()}")
    tail.append(f"时效性：{VOLATILE_MARK if volatile else STABLE_MARK}")
    return f"- [技能 {tag}] {body}（{'；'.join(tail)}）"


def append_memory(text: str, *, skill: str = "", source: str = "",
                  volatile: bool = False) -> str:
    """服务端往长期记忆里追加一条，返回写进去的那一行。

    **日常写入不走这里**——日常是干活的计算进程自己用 write/edit 工具写。
    这个函数给迁移和测试用。
    """
    line = format_entry(text, skill=skill, source=source, volatile=volatile)
    if not line:
        return ""
    p = long_term_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.is_file():
        p.write_text(LONG_TERM_HEADER, encoding="utf-8")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


# --- 工具问题 -----------------------------------------------------------------

def append_tool_issue(text: str, *, day: str | None = None, skill: str = "",
                      source: str = "") -> str:
    """往当天的工具问题文件里**追加**一条，返回写进去的那一行。

    **同一天多次追加必须是追加而不是覆盖**：一天里可能撞见好几个工具问题，
    覆盖掉的话前面几条就永远没了，而用户是按文件逐条修的。
    """
    body = (text or "").strip()
    if not body:
        return ""
    d = (day or db.now_iso())[:10]
    p = tool_issue_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    tag = (skill or "").strip() or "通用"
    tail = []
    if source:
        tail.append(f"来源：{source.strip()}")
    tail.append(f"记录于 {d}")
    line = f"- [技能 {tag}] {body}（{'；'.join(tail)}）"
    if not p.is_file():
        p.write_text(TOOL_ISSUE_HEADER.format(day=d), encoding="utf-8")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


def list_tool_issue_files() -> list[dict]:
    """列出攒着的工具问题文件，按日期倒序。文件不在就返回空列表。"""
    d = tool_issues_dir()
    out: list[dict] = []
    try:
        if not d.is_dir():
            return out
        files = sorted(d.glob("*.md"))
    except OSError:
        return out
    for f in files:
        if not _DAY_RE.match(f.stem):
            continue
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        out.append({"date": f.stem, "path": str(f), "size": size,
                    "items": len(read_tool_issues(f.stem))})
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def read_tool_issues(day: str) -> str:
    """读某一天的工具问题文件。读不出来返回空串（不抛）。"""
    try:
        p = tool_issue_path(day)
    except ValueError:
        return ""
    try:
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("读工具问题文件失败：%s", p)
        return ""


# --- 复核状态（每天一次）------------------------------------------------------

def read_review_state() -> dict:
    """读复核状态。文件不存在或坏了就当没整理过。"""
    p = review_state_path()
    try:
        if not p.is_file():
            return {}
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("读复核状态失败，本轮按「没整理过」处理：%s", p)
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("复核状态文件坏了，本轮按「没整理过」处理：%s", p)
        return {}
    return data if isinstance(data, dict) else {}


def write_review_state(state: dict) -> None:
    """写复核状态。**先写临时文件再替换**：半截 JSON 会让下一次判定失效。"""
    p = review_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(p)


def _today(now: str | None = None) -> str:
    """今天是哪天。``now`` 可以传一个 ISO 时间串（测试用）。"""
    raw = (now or db.now_iso()).strip()
    d = raw[:10]
    if not _DAY_RE.match(d):
        raise ValueError(f"日期格式不对：{now!r}（要 YYYY-MM-DD 开头的时间串）")
    return d


# --- 复核提示词 ---------------------------------------------------------------

def _tool_issues_listing() -> str:
    """工具问题目录里已经攒了哪些：文件、条数、以及条目本身。

    **条目本身要列出来**：只给路径的话，复核进程得先自己去读一遍才知道
    「有哪些问题」；而「有哪些问题」正是它要判断的东西之一
    （某个问题可能早就被修好了）。
    """
    files = list_tool_issue_files()
    if not files:
        return "- （还没有攒下任何工具问题文件）"
    lines: list[str] = []
    shown = 0
    for f in files:
        lines.append(f"- {f['date']}（{f['items']} 条）：{f['path']}")
        for ln in read_tool_issues(f["date"]).splitlines():
            s = ln.strip()
            if not s.startswith("- "):
                continue
            if shown >= 40:
                lines.append("  （其余条目请自己读文件看）")
                break
            lines.append(f"  {s}")
            shown += 1
    return "\n".join(lines)


def build_review_brief(day: str | None = None) -> str:
    """构造复核进程的提示词（brief）。

    这份提示词要交代清楚四件事：**长期记忆在哪**、**怎么判断一条经验还有
    没有效**、**确认失效了怎么办**、**什么时候算整理完**。少一样，复核
    进程就会凭自己的理解乱改——而它改的是每个任务都要读的东西。
    """
    d = _today(day)
    listing = _tool_issues_listing()

    return f"""\
你在为「自动化计算平台」做长期记忆的每日复核。

**你的身份是普通计算进程，不是管理员。** 你没有管理员令牌：不许碰管理端点、
不许改技能仓库、不许删任务、不许提交 git。你只负责整理长期记忆这一件事。

## 长期记忆文件

绝对路径（用 read / write / edit 工具直接读写它）：
{long_term_path()}

工具问题目录（**只读不改**：这些条目由用户每周用 minimax code 统一修）：
{tool_issues_dir()}

工具问题目录里已经攒了这些：
{listing}

复核记录写到这里（整理完之前先写它）：
{review_report_path(d)}

## 一条经验长什么样

长期记忆一行一条，格式是：

- [技能 技能名] 具体做法，别的任务能照抄。（来源：任务 #12；时效性：稳定）

- `[技能 技能名]`：这条属于哪个技能；跨技能就写「通用」。
- `来源`：从哪次任务、哪个技能里得到的，要能顺着它找到出处。
- `时效性`：`稳定`（不会随时间失效）或 `可能有时效性`
  （依赖当下的环境、版本、路径、外部服务，过一阵可能不成立）。

## 怎么判断一条经验是否还有效

1. **标了「可能有时效性」的，必须去确认它现在还成不成立。**
   不要凭印象下结论——印象正是那种过期经验活下来的原因。
   用你的工具（read / write / edit / bash）去查证实际情况：
   路径还在不在、命令跑出来的还是不是那样、文件里那个参数是不是还存在；
   还要用**联网搜索**核对官方文档、版本变化、那个服务是不是还在。
2. **标了「稳定」的，默认不动。** 除非你在文件里看到了明确的矛盾
   （同一条经验写了两遍且互相冲突，或者它和另一条明确冲突）。
3. **判据要落到具体的东西上**：说清楚你查了哪里、看到了什么。
   判不出来就**保留**这条，并在复核记录里写明「没查实」——宁可留一条
   过期经验，也不要误删一条还有用的做法。

## 确认失效了怎么处理

- 整条不成立了 → **删掉那一行**（不要留占位、不要留「已失效」的空壳）。
- 只是说法过时、做法本身还成立 → **改写**成现在成立的表述，保留来源，
  把 `时效性` 改成 `稳定`。
- 失效的原因本身值得留（那是工具的问题）→ 追加到当天的工具问题文件：
  {tool_issue_path(d)}
  格式跟那里已有的一样，一行一条。

## 什么时候算整理完

1. 每一条标了「可能有时效性」的，都已经查证过，并且落到了三种结果之一：
   确认仍成立（改成 `稳定`）／确认失效（删掉）／只是说法过时（改写）。
2. 长期记忆里没有互相矛盾的重复条目。
3. 复核记录已经写到 {review_report_path(d)}，
   里面写清楚：改了什么、为什么、哪几条没查实。
4. 最后用一句话总结这次整理的结果。

**不要**因为「看起来像经验」就往长期记忆里加新条目——
第一手写入者是干活的计算进程，不是你。你的活是**整理**，不是**积累**。
"""


# --- 跑一次复核 ---------------------------------------------------------------

def _spawn_review_agent(brief_path: Path, work_dir: Path,
                        timeout: float) -> dict:
    """起一个复核进程（普通计算进程的身份）。

    **不拿任何管理员令牌，也不拿 agent 令牌**：它只靠文件系统与联网搜索
    干活，碰不到平台的管理能力。模型配置走 :mod:`llm` 那一套，
    和调度器给普通任务物化的配置是同一份来源。
    """
    node = shutil.which("node") or "node"
    script = Path(__file__).resolve().parent.parent / "agent" / "run.mjs"
    env = dict(os.environ)
    env["PROTFORGE_WEB_REVIEW_MODE"] = "1"
    env["PROTFORGE_WEB_REVIEW_BRIEF_PATH"] = str(brief_path)
    env["PROTFORGE_WORK_DIR"] = str(work_dir)
    env["PROTFORGE_WEB_DATA_ROOT"] = str(db.data_root())
    # agent 侧的路径守卫要跟服务端用同一个 ProtForge 根（与调度器一致）。
    env.setdefault("PROTFORGE_DATA", guards.DEFAULT_PROTFORGE_ROOT)
    # 模型配置跟着这次复核走：写在工作目录里。
    agent_dir, have_key = llm.materialize_model_config(work_dir)
    env["PROTFORGE_AGENT_DIR"] = str(agent_dir)
    env["PROTFORGE_LLM_PROVIDER"] = llm.LLM_PROVIDER_ID
    env["PROTFORGE_LLM_MODEL"] = llm.model_id()
    if not have_key:
        env["PROTFORGE_LLM_KEY_ENV"] = llm.key_env()
        log.warning("环境变量 %s 没值，复核进程大概率发不出模型请求", llm.key_env())
    # 计算进程这一整棵子树的 stdout/stderr 一律用 UTF-8（与调度器一致）。
    env["PYTHONIOENCODING"] = "utf-8"
    # **不给任何令牌**：复核进程不是管理员，也不是某个任务的计算进程。
    env.pop("PROTFORGE_AGENT_TOKEN", None)
    env.pop("PROTFORGE_TASK_ID", None)

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [node, str(script)], env=env, cwd=str(script.parent),
            capture_output=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None,
                "output": "", "error": f"复核进程超过 {timeout:.0f} 秒还没结束"}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "returncode": None, "output": "",
                "error": f"复核进程起不来：{exc}"}
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "output": (out + ("\n" + err if err else ""))[-4000:],
            "error": "" if proc.returncode == 0 else f"退出码 {proc.returncode}"}


def run_daily_review(*, now: str | None = None, force: bool = False,
                     runner=None, timeout: float = REVIEW_TIMEOUT) -> dict:
    """跑一次长期记忆复核。**同一天第二次调用不会再整理一次。**

    参数：

    * ``now``：当作「现在」的 ISO 时间串（``2026-10-22T09:00:00``）。
      缺省用系统时间。**只取前 10 位当日期**。
    * ``force``：``True`` 时忽略「今天已经整理过」的判定，强制再跑一次。
    * ``runner``：跑复核的可替换入口，签名 ``runner(brief, work_dir) -> dict``。
      缺省起一个真的复核进程（``agent/run.mjs`` 的复核模式）。
    * ``timeout``：复核进程的超时秒数。

    返回：

    .. code-block:: python

        {
            "ok": bool,          # 这一次是否真的整理成功了
            "skipped": bool,     # True = 今天已经整理过，什么都没做
            "date": "2026-10-22",
            "reason": str,       # 跳过或失败的原因
            "brief_path": str,   # 写给复核进程的提示词落在哪
            "returncode": int | None,
            "output": str,       # 复核进程的输出（截断）
            "error": str,
        }

    **「每天一次」是落盘的**（``<数据根>/memory/review-state.json``），
    不靠进程内存——平台有 public / admin / internal 三个进程，
    靠内存的话每个进程都会以为「今天还没整理过」。
    """
    d = _today(now)
    state = read_review_state()
    if not force and state.get("last_review_date") == d:
        return {"ok": True, "skipped": True, "date": d,
                "reason": f"{d} 已经整理过了，不重复整理",
                "brief_path": "", "returncode": None, "output": "", "error": ""}

    brief = build_review_brief(d)
    work_dir = review_dir() / d
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        brief_path = work_dir / "brief.md"
        brief_path.write_text(brief, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "skipped": False, "date": d,
                "reason": f"复核提示词写不下去：{exc}",
                "brief_path": "", "returncode": None, "output": "", "error": str(exc)}

    if runner is None:
        result = _spawn_review_agent(brief_path, work_dir, timeout)
    else:
        try:
            result = runner(brief, work_dir)
        except Exception as exc:                         # noqa: BLE001
            result = {"ok": False, "returncode": None, "output": "", "error": str(exc)}
    if not isinstance(result, dict):
        result = {"ok": True, "returncode": None,
                  "output": str(result or ""), "error": ""}
    ok = bool(result.get("ok"))
    error = str(result.get("error") or "")

    new_state = dict(state)
    new_state["last_attempt_date"] = d
    new_state["last_run_at"] = db.now_iso()
    new_state["last_ok"] = ok
    new_state["last_error"] = error
    if ok:
        # **只有整理成功了才记「今天整理过」**：失败的那次不算，
        # 下一轮还能重试——不然一次偶发失败会把这个月的复核都卡掉。
        new_state["last_review_date"] = d
    try:
        write_review_state(new_state)
    except OSError as exc:
        log.warning("复核状态写不下去：%s", exc)

    return {"ok": ok, "skipped": False, "date": d,
            "reason": "整理完成" if ok else (error or "复核没跑成功"),
            "brief_path": str(brief_path),
            "returncode": result.get("returncode"),
            "output": str(result.get("output") or ""),
            "error": error}


# --- 迁移（旧布局 → 新布局）---------------------------------------------------

def _legacy_lesson_lines() -> list[str]:
    """旧经验文件里的条目行（``- …`` 开头）。读不出来就当没有。"""
    p = legacy_lessons_path()
    try:
        if not p.is_file():
            return []
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- ") and s != "- 无":
            out.append(s)
    return out


def _legacy_lesson_body(line: str) -> tuple[str, str, str]:
    """把旧的一行拆成（正文，来源，时效性）。

    旧格式是 ``- 正文（来自任务 #12，admin 于 2026-10-19 归档）``。
    拆不出来就整个当正文，来源留空——**宁可信息少，不要丢内容**。
    """
    m = _ENTRY_RE.match(line)
    body = m.group(2).strip() if m else line.lstrip("- ").strip()
    src = ""
    s = _SOURCE_RE.search(body)
    if s:
        src = s.group(1).strip()
    else:
        old = re.search(r"来自任务\s*#?(\d+)", body)
        if old:
            src = f"任务 #{old.group(1)}"
    vol = _VOLATILE_RE.search(body)
    volatility = vol.group(1).strip() if vol else ""
    # 旧文件里的「归档于 <日期>」不算时效性标注：那是归档时间，不是有效期。
    body = re.sub(r"（来自任务[^）]*）", "", body).strip()
    return body, src, volatility


def _legacy_tool_gap_sections() -> list[tuple[str, list[tuple[str, str]]]]:
    """旧反思正文里的「工具意见」一节。返回 [(日期, [(技能, 条目), ...])]。

    旧流程里工具意见走的是「生成修改方案 → 看 diff → 应用」，
    那条路已经废了：工具问题现在只按日期攒起来，等用户每周统一修。
    迁移把它们从反思正文里搬进当天的工具问题文件。
    """
    out: list[tuple[str, list[tuple[str, str]]]] = []
    d = legacy_reflections_dir()
    try:
        if not d.is_dir():
            return out
        files = sorted(d.glob("task-*.md"))
    except OSError:
        return out
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        items: list[tuple[str, str]] = []
        on = False
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("#"):
                on = s.lstrip("# ").strip() == "工具意见"
                continue
            if on and s.startswith("- "):
                body = s[2:].strip()
                if not body or body == "无":
                    continue
                m = _ENTRY_RE.match(s)
                if m:
                    items.append((m.group(1).strip(), m.group(2).strip()))
                else:
                    items.append(("", body))
        if not items:
            continue
        # 日期取文件里写的收工时间；取不到就用文件自己的修改时间。
        day = ""
        m = re.search(r"收工时间：\s*(\d{4}-\d{2}-\d{2})", text)
        if m:
            day = m.group(1)
        if not _DAY_RE.match(day):
            day = db.now_iso()[:10]
        out.append((day, items))
    return out


def migrate_legacy(*, dry_run: bool = False) -> dict:
    """把旧布局里的东西搬进新布局。**幂等**：跑两次结果一样。

    搬两样：

    * 旧的全局经验文件 ``<数据根>/lessons.md`` → 长期记忆
      ``<数据根>/memory/long-term.md``。每条经验带上来源；
      来源里没有时效性标注的，按「稳定」记（旧流程里没有这个概念）。
    * 旧反思正文里的「工具意见」一节 → 工具问题文件
      ``<数据根>/tool-issues/<收工日期>.md``。

    **幂等靠内容去重，不靠状态文件**：已经搬过去的条目不会再来一遍。
    这样即使状态文件丢了、或者有人手工改过长期记忆，重跑也不会写出重复条目。

    **不删旧文件**：删数据这件事不该由一个迁移函数顺手做掉。
    确认新文件没问题之后，由人自己把旧的移走。

    返回::

        {
            "dry_run": bool,
            "lessons": {"added": int, "skipped": int},
            "tool_issues": {"added": int, "skipped": int, "files": [str, ...]},
        }
    """
    # 去重按**正文**（去掉行尾元信息之后的那部分）：同一条经验换了来源、
    # 换了归档日期，它还是同一条，不该被搬第二遍。
    existing_texts = {i["text"].strip() for i in read_entries()}

    added = skipped = 0
    for line in _legacy_lesson_lines():
        body, src, _ = _legacy_lesson_body(line)
        if not body:
            continue
        if body in existing_texts:
            skipped += 1
            continue
        if not dry_run:
            append_memory(body, skill="", source=src, volatile=False)
        existing_texts.add(body.strip())
        added += 1

    t_added = t_skipped = 0
    touched: list[str] = []
    for day, items in _legacy_tool_gap_sections():
        if not day:
            continue
        current = read_tool_issues(day)
        for skill, body in items:
            if body in current:
                t_skipped += 1
                continue
            if not dry_run:
                append_tool_issue(body, day=day, skill=skill, source="旧反思归档")
            t_added += 1
            touched.append(day)

    return {
        "dry_run": bool(dry_run),
        "lessons": {"added": added, "skipped": skipped},
        "tool_issues": {"added": t_added, "skipped": t_skipped,
                        "files": sorted(set(touched))},
    }


__all__ = [
    "MEMORY_BLOCK_MAX",
    "MEMORY_BLOCK_ITEMS",
    "VOLATILE_MARK",
    "STABLE_MARK",
    "append_memory",
    "append_tool_issue",
    "build_review_brief",
    "format_entry",
    "legacy_lessons_path",
    "legacy_proposals_dir",
    "legacy_reflections_dir",
    "list_tool_issue_files",
    "long_term_path",
    "memory_block",
    "memory_dir",
    "migrate_legacy",
    "parse_entries",
    "read_entries",
    "read_long_term",
    "read_review_state",
    "read_tool_issues",
    "review_dir",
    "review_report_path",
    "review_state_path",
    "run_daily_review",
    "tool_issue_path",
    "tool_issues_dir",
    "write_review_state",
]
