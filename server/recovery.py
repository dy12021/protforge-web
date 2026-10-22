"""自动恢复：失败任务的评估与自动重跑。

为什么要有这一层
----------------

2026-10-22 真机上，一个工作流行（任务 30）**跑完了却什么都没交**：
``deliverables/`` 是空的、``summary`` 是空的，而看板上的状态是「已完成」。

原因是一条链：agent 自己 OOM 死了（numpy 分配 42.6 MiB 失败，紧跟着模型请求
报 ``Data cannot be cloned, out of memory``）→ 它没调 ``finish`` → 平台的
「自行收口」路径给它补了一个收口标记 → 收尾分流只看标记不看**退出码** →
于是「计算进程正常退出（退出码 1）」被写进时间线，223 个文件被打成包，
这一行在结果表里是绿的。

这一层做的事就两件：

1. **把「失败」如实变成失败。** 见 :func:`server.dispatcher.Dispatcher._settle`
   里的退出码判据——那是根因修复，recovery 只是兜底。
2. **对真的失败做一次评估，然后自己决定重跑还是写清原因。** 评估是一次
   轻量模型调用（读 stderr 尾部、事件、审计摘要、产物情况），**不起完整的
   agent 会话**——那要建工作区、要装技能、要跑几十条命令，对「看一眼这个
   失败能不能自动救」这件事太贵。

**上限是硬的。** 每个任务最多自动重投 ``MAX_AUTO_RETRY`` 次。平台本身不
自动重试是对的：真崩的脚本会无限循环，而这里要放手的恰恰是「平台不肯
接、但换种做法可能成」的那类失败。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

import db
import llm
import store

log = logging.getLogger(__name__)

RECOVERY_DIRNAME = "recovery"
STATE_FILENAME = "state.json"
HINT_FILENAME = "recovery_hint.md"

#: 每个任务最多自动重投几次。硬上限——没有上限的话，一段注定失败的脚本
#: 能把整台机器的 GPU 占满，而且没人知道它在第几轮。
MAX_AUTO_RETRY = 2

#: 一轮巡检最多评估几个。评估要调模型，一轮里塞太多会把调度器的循环拖住。
SWEEP_LIMIT = 3

#: 送给评估模型的材料长度上限。stderr 可以有几十万行，全塞进去的话
#: 这次评估自己的上下文先炸掉——那就不是在评估这次失败了。
STDERR_TAIL_CHARS = 4000
EVENT_CHARS = 3000
BRIEF_CHARS = 1500
ERROR_CHARS = 1500
AUDIT_COMMANDS = 12

#: 明显是「人把它停掉的」而不是「它自己坏了」的失败，不自动重跑。
_NOT_AUTO = ("已由管理员中止", "用户中止", "中止")

_state_lock = threading.Lock()


class RecoveryError(RuntimeError):
    """评估这一条走不下去。调用方把记录标成失败，而不是让后台线程死掉。"""


# --- 路径 -------------------------------------------------------------------

def recovery_dir() -> Path:
    return db.data_root() / RECOVERY_DIRNAME


def state_path() -> Path:
    return recovery_dir() / STATE_FILENAME


def hint_path(task_id: int) -> Path:
    """写进工作目录的「上一轮为什么失败、重跑要注意什么」。"""
    return Path(store.get_task(task_id, is_admin=True)["output_dir"]) / HINT_FILENAME


def read_hint(task_id: int) -> str:
    """读上一轮留下的重跑提示。没有就读成空串——**不许抛异常**。

    这个函数在派活路径上：提示读不出来不能把整个任务带崩。
    """
    try:
        p = hint_path(task_id)
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:                                     # noqa: BLE001
        return ""


# --- 状态 -------------------------------------------------------------------

def load_state() -> dict:
    """读状态。**读不出来就当没有**——状态文件坏了不该让整个巡检停摆。"""
    p = state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        log.warning("恢复状态文件读不出来，当作没有：%s", p)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    p = state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        log.warning("恢复状态写不进去：%s", p)


def attempts_of(state: dict, task_id: int) -> int:
    rec = state.get(str(task_id))
    if not isinstance(rec, dict):
        return 0
    try:
        return int(rec.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


# --- 候选 -------------------------------------------------------------------

def _superseded_ids() -> set[int]:
    """已被新一轮顶替掉的行任务 id。

    一次重投会把旧行任务留在 failed 里，由新任务接管那一行。旧任务**不是**
    「这一行又失败了」——它早就跑完了（或早就放弃了），再评估它、尤其是判成
    「可恢复」并重投，等于把同一行跑第二遍：白烧几小时 GPU，结果还是那一个。

    判据：同一 (workflow_id, workflow_row) 下 id 更大的任务存在 → 旧的被顶替。
    """
    latest: dict[tuple, int] = {}
    try:
        rows = store.list_tasks(status=None, is_admin=True, limit=5000)
    except Exception:                                     # noqa: BLE001
        log.exception('读任务失败')
        return set()
    for row in rows:
        wid = row.get('workflow_id')
        wrow = row.get('workflow_row')
        if wid is None or wrow is None:
            continue
        key = (int(wid), int(wrow))
        tid = int(row['id'])
        if key not in latest or tid > latest[key]:
            latest[key] = tid
    superseded: set[int] = set()
    for row in rows:
        wid = row.get('workflow_id')
        wrow = row.get('workflow_row')
        if wid is None or wrow is None:
            continue
        tid = int(row['id'])
        if latest[(int(wid), int(wrow))] != tid:
            superseded.add(tid)
    return superseded

def candidates() -> list[dict]:
    """待评估的失败任务。

    排除三��：
      * 已经判过「不可恢复」的——再问一次模型只会得到同样的答案，白烧 token；
      * 已经自动重投满次数的；
      * **人把它停掉的**——那是决定，不是故障。
    """
    out: list[dict] = []
    state = load_state()
    superseded = _superseded_ids()
    try:
        rows = store.list_tasks(status="failed", is_admin=True, limit=200)
    except Exception:                                     # noqa: BLE001
        log.exception("读失败任务失败")
        return out
    for row in rows:
        tid = int(row["id"])
        rec = state.get(str(tid))
        if isinstance(rec, dict):
            if rec.get("verdict") == "unrecoverable":
                continue
            if rec.get("verdict") == "requeued" and rec.get("state") == "done":
                # 这一行重投过、也跑完了，还失败：那是新一轮的失败，
                # 但自动重投次数已经用完，不该再投。
                if attempts_of(state, tid) >= MAX_AUTO_RETRY:
                    continue
        if attempts_of(state, tid) >= MAX_AUTO_RETRY:
            continue
        if _aborted_by_human(row):
            continue
        if tid in superseded:
            continue
        out.append(row)
        if len(out) >= SWEEP_LIMIT:
            break
    return out


def _aborted_by_human(task: dict) -> bool:
    """这一条失败是**人把它停掉的**，而不是它自己坏了。

    人停的失败不该自动重跑：那是决定，不是故障。判据看两处——
    ``error`` 字段里的字样，以及时间线上那条 ``actor=admin`` 的中止记录
    （dispatcher.abort 记的是「中止：已杀掉进程树 pid=...」）。
    """
    err = (task.get("error") or "")
    if any(mark in err for mark in _NOT_AUTO):
        return True
    try:
        for e in store.list_events(int(task["id"]), limit=200):
            if e.get("actor") == "admin" and "中止" in (e.get("message") or ""):
                return True
    except Exception:                                     # noqa: BLE001
        return False
    return False


# --- 上下文 -----------------------------------------------------------------

def _tail(path: Path, limit: int) -> str:
    try:
        if not path.is_file():
            return ""
        raw = path.read_bytes()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    return text[-limit:]


def _audit_summary(task_id: int) -> str:
    """这个任务跑过的命令摘要：多少条、最慢的几条、各自的退出码。

    **不要试图去解析命令文本**——命令里什么都有（路径、heredoc、中文），
    正则去猜「这条命令在干什么」只会猜错。这里给模型的是**事实**：条数、
    耗时、退出码。判断「是不是同一类命令反复超时」模型自己看得出来。
    """
    import sqlite3

    db_path = db.data_root() / "web.db"
    if not db_path.is_file():
        return ""
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            rows = list(con.execute(
                "SELECT command, exit_code, elapsed_ms FROM audit_log "
                "WHERE task_id=? ORDER BY elapsed_ms DESC LIMIT ?",
                (task_id, AUDIT_COMMANDS)))
            total = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(elapsed_ms),0) FROM audit_log "
                "WHERE task_id=?", (task_id,)).fetchone()
        finally:
            con.close()
    except Exception:                                     # noqa: BLE001
        log.exception("读审计失败")
        return ""
    if not rows:
        return ""
    n = int(total[0] or 0)
    total_ms = int(total[1] or 0)
    lines = [f"这一轮共跑了 {n} 条命令，累计 {total_ms/1000:.0f} 秒。最慢的几条："]
    for r in rows:
        cmd = (r["command"] or "").replace("\n", " ")[:160]
        lines.append("- 耗时 %.0f s，退出码 %s：%s"
                     % ((r["elapsed_ms"] or 0) / 1000.0, r["exit_code"], cmd))
    return "\n".join(lines)


def _artifacts_summary(task: dict) -> str:
    """产物情况。**空产物是「没交货」，不是「没有要交的东西」。"""
    out_dir = (task.get("output_dir") or "").strip()
    if not out_dir:
        return "没有产物目录。"
    p = Path(out_dir)
    if not p.is_dir():
        return f"产物目录 {out_dir} 不存在。"
    files = []
    for root, _dirs, names in __import__("os").walk(p):
        for nm in names:
            files.append(Path(root) / nm)
    total = len(files)
    deliv = p / "deliverables"
    n_deliv = 0
    if deliv.is_dir():
        n_deliv = sum(1 for _ in deliv.rglob("*") if _.is_file())
    return (f"产物目录 {out_dir}：共 {total} 个文件，"
            f"其中 deliverables/ 下 {n_deliv} 个文件。"
            + ("**deliverables 是空的——这一轮什么都没交。**" if n_deliv == 0 else ""))


def collect_context(task: dict) -> str:
    """把一次失败拼成评估材料。"""
    tid = int(task["id"])
    parts: list[str] = []
    parts.append("## 需求\n" + (task.get("brief") or "")[:BRIEF_CHARS])
    err = (task.get("error") or "").strip()
    parts.append("## 平台记的失败原因\n" + (err[:ERROR_CHARS] if err else "（空）"))

    stderr_name = f"agent-{tid}.err.log"
    tail = _tail(db.data_root() / "logs" / stderr_name, STDERR_TAIL_CHARS)
    parts.append("## 计算进程的 stderr 末尾\n" + (tail if tail else "（没有 stderr）"))

    try:
        events = store.list_events(tid, limit=40)
    except Exception:                                     # noqa: BLE001
        events = []
    ev_lines = [f"{e.get('created_at')} [{e.get('kind')}/{e.get('actor')}] {e.get('message')}"
                for e in events[-25:]]
    parts.append("## 时间线末尾\n" + ("\n".join(ev_lines) if ev_lines else "（空）"))

    parts.append("## 命令审计\n" + (_audit_summary(tid) or "（没有审计记录）"))
    parts.append("## 产物\n" + _artifacts_summary(task))
    return "\n\n".join(parts)


# --- 评估 -------------------------------------------------------------------

ASSESS_SYSTEM = """\
你在为「自动化计算平台」判断一次失败的计算任务：它能不能自动重跑。

你会看到：需求、平台记的失败原因、计算进程的 stderr 末尾、时间线、命令审计、
产物情况。**只依据这些材料说事**，材料里没有的事实不要编。

判断标准：
- **能自动恢复**：失败的原因是环境性的、一次性的、或者换个做法就能成的
  （内存不够、某个工具参数没给对、路径没找到、超时被砍、进程崩了）。
  这类重跑一次有意义，有时还需要一句「这次注意什么」的提示。
- **不能自动恢复**：失败的原因是需求本身做不了、输入数据有问题、
  或者同一个错已经连续出现两轮而没有任何变化。再跑一次只会同样地失败，
  这种要把**原因说清楚**给用户看。

**两种情况都要写清楚原因**——不能恢复时那句话就是要展示给用户的。

只回答一个 JSON 对象，不要有多余文字：
{"recoverable": true 或 false,
 "reason": "一句话说清失败原因，具体到能让人明白",
 "hint": "重跑时要注意什么；不能恢复时写空字符串",
 "user_message": "给用户看的一句话结论，40 字以内"}
"""


def _parse_verdict(text: str) -> dict:
    """从模型回复里抠出 JSON。**模型不总是听话**，所以要容错。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            return {
                "recoverable": bool(obj.get("recoverable")),
                "reason": str(obj.get("reason") or "").strip(),
                "hint": str(obj.get("hint") or "").strip(),
                "user_message": str(obj.get("user_message") or "").strip(),
            }
    # 抠不出 JSON：宁可当成「不能恢复」，也别拿半截话去重跑。
    log.warning("评估回复里没有可解析的 JSON：%r", raw[:200])
    return {
        "recoverable": False,
        "reason": "评估模型没有给出可解析的结论，按不可恢复处理",
        "hint": "",
        "user_message": "评估没有给出可解析的结论，已保留现场",
    }


def assess(task: dict) -> dict:
    """评估一次失败能不能自动恢复。返回 :func:`_parse_verdict` 那种字典。"""
    ctx = collect_context(task)
    try:
        text = llm.chat_completion(ctx, system=ASSESS_SYSTEM,
                                   max_completion_tokens=4096,
                                   reasoning_effort="low")
    except llm.LLMError as exc:
        raise RecoveryError(f"评估模型调不通：{exc}") from exc
    return _parse_verdict(text)


# --- 执行 -------------------------------------------------------------------

def requeue(task_id: int, reason: str) -> None:
    """把一个终态任务放回队列。**产物不删**。

    这套字段的语义和管理员点「重跑」完全一样（``done_at`` / ``step`` /
    ``error`` / ``package_path`` / ``summary`` 是**本轮**的结果；收口标记是
    **本轮**已收口；``workflow_archived`` 是**本行**已写回表）——不清理的
    话，第二轮的 agent 调 finish 会被 422 拒掉，而且这一行永远写不回结果表。
    """
    store.update_task(task_id, done_at=None, step="", error="",
                      package_path="", summary="")
    store.clear_finished(task_id)
    store.update_task(task_id, workflow_archived=0)
    # allow_leave_terminal=True 是「重跑」这一条窄口子：failed 是终态，
    # 而重跑按定义就是把它放回队列。**不用 admin 身份去骗这道守卫**——
    # 时间线上要如实写「自动恢复」，写成「管理员把任务放回队列」是撒谎。
    store.set_status(task_id, "queued", actor="system",
                     message=f"自动恢复：{reason}",
                     allow_leave_terminal=True)


def write_hint(task_id: int, hint: str) -> None:
    """把「这次要注意什么」写进工作目录，下一轮派活时带上。"""
    if not hint:
        return
    try:
        p = hint_path(task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "# 上一轮失败了，这一轮请注意\n\n"
            "（这是平台自动恢复时评估出来的，**先看它再动手**。）\n\n"
            + hint.strip() + "\n", encoding="utf-8")
    except (OSError, store.StoreError):
        log.warning("任务 %s 的重跑提示写不进去", task_id, exc_info=True)


def handle(task: dict, state: dict) -> str:
    """处理一个失败任务。返回 'requeued' / 'unrecoverable' / 'error'。"""
    tid = int(task["id"])
    try:
        verdict = assess(task)
    except RecoveryError as exc:
        state[str(tid)] = {"verdict": "error", "reason": str(exc),
                           "attempts": attempts_of(state, tid), "state": "done"}
        save_state(state)
        store.add_event(tid, "note", "system",
                        f"自动恢复评估没跑成：{exc}")
        return "error"

    n = attempts_of(state, tid)
    rec = {
        "reason": verdict["reason"],
        "hint": verdict["hint"],
        "attempts": n,
        "evaluated_at": db.now_iso(),
    }
    if verdict["recoverable"] and n < MAX_AUTO_RETRY:
        rec["verdict"] = "requeued"
        rec["state"] = "done"
        rec["attempts"] = n + 1
        state[str(tid)] = rec
        save_state(state)
        write_hint(tid, verdict["hint"])
        store.add_event(tid, "note", "system",
                        f"自动恢复评估：判定为可恢复，已自动重投"
                        f"（第 {n+1}/{MAX_AUTO_RETRY} 次）。原因：{verdict['reason']}")
        requeue(tid, f"评估为可恢复，重新排队（第 {n+1}/{MAX_AUTO_RETRY} 次）")
        return "requeued"

    rec["verdict"] = "unrecoverable"
    rec["state"] = "done"
    state[str(tid)] = rec
    save_state(state)
    msg = verdict["user_message"] or verdict["reason"]
    store.update_task(tid, summary=msg)
    store.add_event(tid, "note", "system",
                    f"自动恢复评估：判定为不可自动恢复，已保留现场。原因：{verdict['reason']}")
    log.info("任务 %s 不可自动恢复：%s", tid, verdict["reason"])
    return "unrecoverable"


def process(task_id: int) -> str:
    """评估并处理一个失败任务。返回 'requeued' / 'unrecoverable' / 'error'。

    这是给后台线程调用的入口：状态自己读自己写，调用方不用管。
    """
    with _state_lock:
        st = load_state()
        try:
            task = store.get_task(int(task_id), is_admin=True)
        except Exception:                         # noqa: BLE001
            log.exception("读任务 %s 失败", task_id)
            return "error"
        return handle(task, st)


def sweep_once(state: dict | None = None) -> int:
    """扫一轮：评估并处理待恢复的失败任务。返回处理条数。

    **同步版**，给测试用；线上走 :func:`process`（后台线程里逐条调）。
    """
    with _state_lock:
        st = state if state is not None else load_state()
        done = 0
        for task in candidates():
            try:
                handle(task, st)
                done += 1
            except Exception:                             # noqa: BLE001
                log.exception("处理任务 %s 的自动恢复失败", task.get("id"))
        return done
