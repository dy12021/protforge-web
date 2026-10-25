# -*- coding: utf-8 -*-
"""运行时用时复核：**跑得久了，起一个独立上下文判「这个用时合理吗」**。

为什么要有这个
--------------
一条计算跑了很久，有两种可能，处理方式**完全相反**：

- **合理**：就是个大计算（8 小时的 rfdiffusion、整夜的 MD）→ **该让它跑完**；
- **不合理**：撞进了病态算法（2026-10-24 那条 6126 原子 × 80 轮的 O(n²)
  碰撞消解，跑了 4 小时 42 分）→ **该判失败**。

机械地按时间砍会砍错第一种；放任不管会烧掉第二种。

**所以判据交给另一个上下文去判**，而不是平台自己猜：
这个复核进程和当初干活的计算进程**不是同一个上下文**，它没看过它怎么想的，
只看证据（跑了多久、在跑什么命令、文件有没有在长、日志有没有在动）。

裁决
----
- ``reasonable``   → **续跑**（再给一段时间），并在事件里写明理由；
- ``unreasonable`` → **判失败**，写明理由，**不产出交付**；
- 认不出来 / 复核进程没跑成 → ``error`` → **不裁决**（继续跑）。

「没跑成就不裁决」是硬规矩：复核挂了不等于用时不合理。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import db
import guards
import llm
import store
import task_reviewer

#: 任务跑过这么久就触发一次用时复核（秒）。默认 45 分钟——
#: 批次里大多数行 30–60 分钟，所以这个阈值只在「明显偏慢」时才响。
REVIEW_AFTER_S = 2700

#: 判「合理」之后**再给**多少时间（秒）。一次给 45 分钟，
#: 够它跑完剩下的，又不至于让一行无限拖下去（下次复核还会再判一次）。
GRANT_EXTRA_S = 2700

#: 复核进程自己最多跑多久（秒）。它是裁判，不该拖住调度器。
REVIEW_TIMEOUT_S = 600.0

#: 一个任务最多复核几次。判「不合理」就直接失败了，所以这个数只在
#: 「一直判合理」时起作用——防止一个病态任务靠"合理"一路续命。
MAX_REVIEWS_PER_TASK = 4

#: 结论词表。**认不出来就是 error，不猜**——猜一个 verdict 等于
#: 替一个没跑成的裁判做决定，而两种裁决的后果完全相反（续跑 vs 判失败）。
_VERDICT_MAP = {
    "reasonable": "reasonable", "ok": "reasonable", "fine": "reasonable",
    "continue": "reasonable", "合理": "reasonable", "继续": "reasonable",
    "合理，继续": "reasonable",
    "unreasonable": "unreasonable", "bad": "unreasonable",
    "timeout": "unreasonable", "不合理": "unreasonable",
    "超时": "unreasonable", "不合理/超时": "unreasonable",
}

#: 复核目录。与产物审核目录（``reviews/``）**分开**：
#: 那是「这一轮满不满足需求」，这是「这一轮跑得合不合理」，两回事。
def review_dir(task_id: int) -> Path:
    return db.data_root() / "runtime_reviews" / str(int(task_id))


def brief_path(task_id: int) -> Path:
    return review_dir(task_id) / "brief.md"


def verdict_path(task_id: int) -> Path:
    return review_dir(task_id) / "verdict.json"


# ---------------------------------------------------------------- 证据

def _recent_commands(task_id: int, limit: int = 12) -> list[dict]:
    """最近几条命令审计。**这是硬证据**：每条命令原文、退出码、耗时都在。"""
    try:
        rows = store.audit.list(task_id, after_id=0, limit=500)
    except Exception:                              # noqa: BLE001
        return []
    out = []
    for r in list(rows or [])[-limit:]:
        out.append({
            "command": (r.get("command") or "")[:400],
            "kind": r.get("kind") or "",
            "exit_code": r.get("exit_code"),
            "elapsed_s": round(float(r.get("elapsed_ms") or 0) / 1000.0, 1),
            "created_at": r.get("created_at") or "",
        })
    return out


def _log_tail(task_id: int, max_bytes: int = 6000) -> str:
    """计算进程的日志尾巴。"""
    logs = db.data_root() / "logs"
    for name in (f"agent-{task_id}.log", f"agent-{task_id}.err.log"):
        p = logs / name
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        return data[-max_bytes:].decode("utf-8", errors="replace")
    return ""


def _work_dir_stats(task_id: int) -> dict:
    """工作目录的「体量」统计：文件数、总字节、最新修改时间。

    **这是判断「在不在推进」最硬的证据**：
    一个真在算的进程会持续写中间产物（文件数/总字节在长），
    一个空转的进程工作目录会一动不动。
    """
    try:
        wd = guards.regions(task_id)[guards.WORK]
    except Exception:                              # noqa: BLE001
        return {}
    n = 0
    total = 0
    newest = 0.0
    try:
        for p in wd.rglob("*"):
            try:
                if p.is_file():
                    st = p.stat()
                    n += 1
                    total += int(st.st_size)
                    newest = max(newest, float(st.st_mtime))
            except OSError:
                continue
    except Exception:                              # noqa: BLE001
        pass
    return {"files": n, "bytes": total,
            "newest_age_s": round(time.time() - newest, 1) if newest else None}


def _gpu_busy() -> str:
    """GPU 上有没有在跑的东西。**读不到就说读不到**，不猜。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        import subprocess
        r = subprocess.run([exe, "--query-compute-apps=pid,used_memory",
                            "--format=csv,noheader"], capture_output=True,
                           text=True, timeout=20)
        return (r.stdout or "").strip()[:600]
    except Exception:                              # noqa: BLE001
        return ""


def collect_evidence(task_id: int, elapsed_s: float) -> dict:
    """把「这个任务在干什么、跑得动不动」的事实收集齐。"""
    return {
        "elapsed_s": round(float(elapsed_s), 1),
        "commands": _recent_commands(task_id),
        "work_dir": _work_dir_stats(task_id),
        "log_tail": _log_tail(task_id),
        "gpu": _gpu_busy(),
    }


# ---------------------------------------------------------------- brief

def build_brief(task: dict, evidence: dict) -> str:
    """给复核进程的提示词。**只给事实与判据，不给结论。**"""
    ev = evidence or {}
    wd = ev.get("work_dir") or {}
    cmds = ev.get("commands") or []

    lines = [
        "# 判断一个计算任务的用时是否合理",
        "",
        f"任务 #{task.get('id')}「{task.get('title')}」已经跑了 "
        f"**{_human(ev.get('elapsed_s'))}**。",
        "平台要在它继续占用机器之前，判一下**这个用时合不合理**。",
        "",
        "## 原始需求",
        "",
        (task.get("brief") or "（没有需求正文）")[:3000],
        "",
        "## 它最近在跑什么（命令审计，原文）",
        "",
    ]
    if cmds:
        for c in cmds:
            lines.append(
                f"- 耗时 {c.get('elapsed_s')}s，退出码 {c.get('exit_code')}，"
                f"类型 {c.get('kind')}")
            lines.append("  ```")
            lines.append("  " + (c.get("command") or "").replace("\n", "\n  "))
            lines.append("  ```")
    else:
        lines.append("（没有命令审计记录）")

    lines += [
        "",
        "## 工作目录的体量",
        "",
        f"- 文件数：{wd.get('files')}",
        f"- 总字节：{wd.get('bytes')}",
        f"- 最新文件是多久之前写的：{wd.get('newest_age_s')} 秒前",
        "",
        "## GPU 上在跑什么",
        "",
        "```",
        (ev.get("gpu") or "（读不到，或者 GPU 上没有计算进程）"),
        "```",
        "",
        "## 计算进程日志的尾巴",
        "",
        "```",
        (ev.get("log_tail") or "（没有日志）")[-4000:],
        "```",
        "",
        "## 怎么判",
        "",
        "**判「合理」（reasonable）**——这些都算合理：",
        "",
        "- 它在跑一个本来就慢的计算（大模型结构预测、扩散采样、分子动力学、"
        "多构象松弛、蒙特卡洛），工作目录里的中间产物**还在长**；",
        "- GPU 或 CPU 确实在满载干活，命令一次跑很久且**没有反复重来**；",
        "- 它已经把规模缩小到能跑完，正在一个一个把剩余的算完。",
        "",
        "**判「不合理」（unreasonable）**——这些都算不合理：",
        "",
        "- 同一条命令**反复超时重来**（尤其是同一份脚本改了改又跑）；",
        "- 工作目录**长时间一动不动**（文件数、总字节都不长，日志也不动）——"
        "它不是在算，是在原地打转；",
        "- 它在自己写的脚本里跑一个**明显病态的算法**（比如两两嵌套的原子对"
        "循环、随规模平方增长的距离矩阵、没有邻居表的全对比较）；",
        "- 它在同一个地方反复试错，且**每一次的规模还在变大**（而不是变小）。",
        "",
        "**判不出来就判不出来**：不要硬凑一个结论。拿不准就写 "
        "`unclear`，平台会当作「没裁决」，让任务继续跑——"
        "**误判成「不合理」的代价是白白判死一个正当的长计算**。",
        "",
        "## 结论怎么写",
        "",
        "把结论写成一个 JSON 文件：`" + str(verdict_path(int(task.get("id")))) + "`",
        "",
        "```json",
        '{',
        '  "verdict": "reasonable | unreasonable | unclear",',
        '  "reason": "一句话说清依据（要引用具体证据：哪条命令、哪个数字）"',
        '}',
        "```",
        "",
        "写完这个文件就结束，不要做别的事。",
    ]
    return "\n".join(lines)


def _human(sec) -> str:
    try:
        s = int(float(sec))
    except (TypeError, ValueError):
        return "未知"
    h, rem = divmod(max(0, s), 3600)
    m = rem // 60
    return f"{h} 小时 {m} 分" if h else f"{m} 分钟"


# ---------------------------------------------------------------- 跑一轮

def review_runtime(task_id: int, *, timeout: float = REVIEW_TIMEOUT_S,
                   elapsed_s: float = 0.0) -> dict:
    """复核一次用时是否合理。**同步跑**（会阻塞调用方几分钟）。

    返回 ``{"verdict": "reasonable"|"unreasonable"|"error", "reason": ...}``。
    """
    task = store.get_task(task_id, is_admin=True)
    if str(task.get("status")) != "running":
        return {"verdict": "error", "reason": f"任务已经不在跑了（{task.get('status')}）"}

    d = review_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    vp = verdict_path(task_id)
    if vp.exists():
        try:
            vp.unlink()
        except OSError:
            pass

    evidence = collect_evidence(task_id, elapsed_s)
    bp = brief_path(task_id)
    bp.write_text(build_brief(task, evidence), encoding="utf-8")

    # **只读根**：被审任务的工作目录。复核进程能看，不许动——
    # 它是在旁观的裁判，改了被看的东西，结论就没意义了。
    readonly: list[Path] = []
    try:
        readonly.append(guards.regions(task_id)[guards.WORK])
    except Exception:                              # noqa: BLE001
        pass

    res = task_reviewer._spawn_review_agent(          # noqa: SLF001
        bp, d, timeout, readonly, mode="runtime")
    verdict, reason = _read_verdict(task_id)
    return {"verdict": verdict, "reason": reason,
            "spawn": res, "brief_path": str(bp)}


def _read_verdict(task_id: int) -> tuple[str, str]:
    """读复核进程写的结论。返回 (verdict, reason)。

    **认不出来 = ``error``，不猜。** 两种裁决的后果完全相反
    （续跑 vs 判失败），猜错的代价比不裁决大得多。
    """
    p = verdict_path(task_id)
    if not p.is_file():
        return "error", "复核进程没有写出结论文件"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "error", f"结论文件读不出来：{exc}"
    if not isinstance(data, dict):
        return "error", "结论文件的格式不对"
    raw = str(data.get("verdict") or "").strip().lower()
    if raw in ("unclear", "unknown", "不确定", "看不清"):
        return "error", "复核进程判不出来"
    verdict = _VERDICT_MAP.get(raw)
    if verdict is None:
        return "error", f"结论里的 verdict 认不出来：{raw!r}"
    reason = str(data.get("reason") or "").strip()
    return verdict, reason[:2000]
