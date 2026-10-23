"""验收与续跑。

两件事，缺一不可：

**验收**：算完 ≠ 交付完成。模型自己调 ``finish`` 只说明「它觉得算完了」，
平台原来就照单全收——不管交上来的是结果还是一堆空文件，一律打成 zip、
标绿、归档进结果表。这一层补上的是**交付**这一段的闸门：

- 算完、打完包 → ``acceptance='pending'``（**待验收**），不是「已完成」；
- 人看过产物与结论 → 点「验收通过」才算 ``accepted``；
- 平台能机械判定的硬条件（产物全是 0 字节、模型自报的产物一个都找不到、
  没有给出结论）→ 自动判 ``rejected``，**但人可以推翻**（自动不通过不是终局）。

**续跑**：同一个任务接着干，不从零重来。

- 失败的接着跑：新一轮的提示词里带上**失败原因**与上一轮已经做出的东西；
- 完成的补要求：带上**上一轮的结论**与产出清单，再加用户这一轮的要求。

续跑是**原地**的：同一个任务 id、同一批产物文件（工作目录不清），
只是把提示词换掉重跑一遍。理由：工作流是「一行一个任务」，另起一轮会让
那一行分裂成两个任务，归档进结果表时该写谁的结果就说不清了。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import db
import store

#: 硬条件的上限：一条任务最多列这么多条原因。理由写长了没人看，
#: 而「有 7 条硬条件没过」和「有 1 条」对验收的人是一回事。
MAX_FAILURES = 5

#: 自报产物名的清洗：只留路径的最后一段。模型可能写
#: ``outputs/result.pdb``，盘上登记的名字是 ``result.pdb``——
#: 按全串比会对不上，于是明明交了产物却被判「一个都没找到」。
_BASENAME_RE = re.compile(r"[^\\/]+$")


class ReviewError(store.Invalid):
    """验收/续跑动作上的问题。继承 store.Invalid，API 层照旧返 422。"""


# ---------------------------------------------------------------- 硬条件


def _declared(task: dict) -> list[str]:
    """计算进程自报的产物清单。"""
    raw = (task.get("declared") or "").strip()
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except ValueError:
        return []
    return [str(x).strip() for x in arr if str(x).strip()]


def _basename(path: str) -> str:
    m = _BASENAME_RE.search(str(path or ""))
    return m.group(0) if m else str(path or "")


def hard_check_failures(task: dict, artifacts=None) -> list[str]:
    """机械的硬条件。返回**不通过的原因**；空列表 = 通过。

    **只查能从库里判定的机械事实**，不判断「这个蛋白设计结果对不对」——
    那是人的事，这一层存在的意义是：明显不成立的东西别拿去让人验收。

    每条判据都要能被人一眼验证（「产物全是 0 字节」看一眼就知道），
    所以这里刻意不写任何需要模型判断的话。

    ``artifacts`` 可以传进来（调用方多半已经查过）——省一次往返。
    """
    tid = int(task["id"])
    fails: list[str] = []

    rows = artifacts if artifacts is not None else store.artifacts.list(tid)
    usable = [r for r in rows if int(r["verified"]) == 1]

    # ① 一个可下载产物都没有。
    if not rows:
        # 这一条其实在打包阶段就拦住了（没有产物 → 打包失败 → 任务 failed），
        # 到不了验收。这里仍然查一遍：产物可能在打包之后被清掉了。
        fails.append("没有可下载的产物：这一轮等于什么都没交")
        return fails

    # ② 产物全是 0 字节的空文件。有文件，但没有内容——那不叫交上来了。
    if all(int(r["size"] or 0) <= 0 for r in rows):
        fails.append("产物全是 0 字节的空文件：有文件名，没有内容")

    # ③ 模型自报的产物一个都找不到。
    declared = _declared(task)
    if declared:
        names = {_basename(r["name"]) for r in rows}
        names |= {_basename(r["path"]) for r in rows}
        if not any(_basename(d) in names for d in declared):
            fails.append("计算进程自报的产物在产物清单里一个都没找到："
                         + "、".join(declared[:3]))

    # ④ 没有结论。summary 是验收的人唯一能读到的「这一轮说了什么」，
    #    它是空的，验收就无从谈起。
    if not (task.get("summary") or "").strip():
        fails.append("没有给出结论（摘要为空）：无从验收")

    return fails[:MAX_FAILURES]


# ---------------------------------------------------------------- 状态流转


def mark_pending(task_id: int) -> dict:
    """算完 + 打完包之后调这个：进「待验收」，或者被硬条件判「不通过」。

    **自动不通过不是终局**——人可以再点「验收通过」把它放过去。
    之所以还要自动判，是因为「产物全是空文件」这种事不该让人替平台查。
    """
    task = store.get_task(task_id, is_admin=True)
    if str(task.get("status")) != "done":
        # 不是 done 的（还在打包、或中途失败了）不进验收。
        return task
    fails = hard_check_failures(task)
    if fails:
        store.set_acceptance(task_id, db.ACCEPTANCE_REJECTED)
        store.add_event(task_id, "note", "system",
                        "验收未通过（硬条件）：" + "；".join(fails))
        return store.get_task(task_id, is_admin=True)
    store.set_acceptance(task_id, db.ACCEPTANCE_PENDING)
    store.add_event(task_id, "note", "system",
                    "算完了，等人验收")
    return store.get_task(task_id, is_admin=True)


def accept(task_id: int, actor: str = "human") -> dict:
    """验收通过。"""
    task = store.get_task(task_id, is_admin=True)
    if str(task.get("status")) != "done":
        raise ReviewError("任务还没算完，验收不了")
    if str(task.get("acceptance")) == db.ACCEPTANCE_ACCEPTED:
        return task                    # 幂等：点两次不报错
    store.set_acceptance(task_id, db.ACCEPTANCE_ACCEPTED)
    store.add_event(task_id, "note", actor, "验收通过：这一轮的结果算交付完成")
    return store.get_task(task_id, is_admin=True)


def reject(task_id: int, note: str = "", actor: str = "human") -> dict:
    """打回：这一轮的结果不认。**不自动续跑**——要续跑请单独调 continue。"""
    task = store.get_task(task_id, is_admin=True)
    if str(task.get("status")) != "done":
        raise ReviewError("任务还没算完，打回不了")
    store.set_acceptance(task_id, db.ACCEPTANCE_REJECTED)
    msg = "验收没通过"
    if (note or "").strip():
        msg += "：" + note.strip()[:400]
    store.add_event(task_id, "note", actor, msg)
    return store.get_task(task_id, is_admin=True)


# ---------------------------------------------------------------- 续跑


def _round_no(task: dict) -> int:
    """这是第几轮续跑。数 followup 里「## 续跑」出现了几次。"""
    text = task.get("followup") or ""
    return text.count("## 续跑") + 1


def compose_followup(task: dict, note: str) -> str:
    """组装这一轮的补充要求文本。

    **组装发生在续跑这一刻**，因为「上一轮」的事实此刻是确定的：失败原因、
    上一轮的结论、上一轮产出了哪些文件。存原话等下一轮再组装是不行的——
    重跑会覆盖掉那些字段，到时候就不知道「上一轮」是哪一轮了。
    """
    note = (note or "").strip()
    if not note:
        raise ReviewError("补充要求不能为空")
    tid = int(task["id"])
    lines: list[str] = []
    lines.append(f"## 续跑（第 {_round_no(task)} 轮）")
    lines.append("")
    status = str(task.get("status") or "")
    if status == "failed":
        lines.append("上一轮**失败了**。")
        err = (task.get("error") or "").strip()
        if err:
            lines.append(f"失败原因：{err[:600]}")
    else:
        lines.append("上一轮算完了，但**结果没有通过验收**（或你就是要再改一版）。")
    summary = (task.get("summary") or "").strip()
    if summary:
        lines.append(f"上一轮的结论：{summary[:600]}")
    arts = [r for r in store.artifacts.list(tid) if int(r["verified"]) == 1]
    if arts:
        lines.append("上一轮产出的文件（**还在工作目录里**，可以直接用、可以直接改）：")
        for r in arts[:20]:
            lines.append(f"- {r['name']}")
        if len(arts) > 20:
            lines.append(f"- ……另外还有 {len(arts) - 20} 个")
    lines.append("")
    lines.append("### 本次补充要求")
    lines.append(note)
    lines.append("")
    lines.append("在上一轮的基础上接着做。**上一轮已经算对的部分不要重算**，"
                 "改掉不对的那部分；确实要重算的，说明为什么。")
    return "\n".join(lines)


def continue_task(task_id: int, note: str, actor: str = "human") -> dict:
    """续跑：同一个任务接着干。

    **保留上一轮的产物文件**（工作目录不清），只把提示词换掉重跑一遍。
    打包路径要清——不清的话打包扫描会看见 package_path 非空就跳过，
    新一轮的产物永远打不进包。
    """
    task = store.get_task(task_id, is_admin=True)
    if str(task.get("status")) not in db.TERMINAL_STATUSES:
        raise ReviewError("任务还在进行中，不用续跑")
    text = compose_followup(task, note)
    store.append_followup(task_id, text)
    store.add_event(task_id, "note", actor,
                    "续跑：" + (note or "").strip()[:200])

    # 上一轮的收口痕迹要清：finished 标记不清，第二轮的 finish 会被 422
    # 拒掉；workflow_archived 不清，结果表那一格不会被新一轮覆盖。
    store.clear_finished(task_id)
    store.update_task(task_id, done_at=None, step="", error="",
                      package_path="", summary="")
    store.update_task(task_id, workflow_archived=0)
    # 新一轮还没算完，验收状态回到 none——等它算完再进 pending。
    store.set_acceptance(task_id, db.ACCEPTANCE_NONE)
    return store.set_status(task_id, "queued", actor=actor,
                            message="续跑：带着上一轮的结果接着算")
