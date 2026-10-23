"""任务审核：**开一轮新的独立上下文**，去核这一轮满不满足原始需求。

为什么不是「调一次模型接口打个分」：

* 干活的那个计算进程**不能审自己**。它刚跑完，手上还握着「我当时是怎么想的、
  哪一步是凑合的」这些上下文；让它回头评价自己的产出，等于让它自己给自己签字。
* 一次性打分拿到的是**一个字符串**，不是「看过哪些文件、按什么看的」。
  出了错没法回看依据，也没法让人推翻。
* 产物可能有几千个（真实批次见过 3703 个）。没有独立上下文就没有地方
  一份份看过去——**它需要自己的工作环境**：自己的目录、自己的会话。

所以这里的审核是一个**真的进程**（``agent/run.mjs`` 的 task 复核模式）：

* **独立工作目录** ``<数据根>/reviews/<任务号>/``：brief、产物副本与结论
  都落在这里，与被审任务完全分开；
* **干净上下文**：新起的会话，看不到干活进程的思考过程；
* **无令牌**：没有 agent 令牌、没有任务号，碰不到平台的任何管理能力。

**它拿到的是产物的副本，不是原件。** 这一条不是洁癖：

* 工作流行任务的产物**不在**任务工作目录里，而在共享的 outputs 目录
  （真实环境是 ``E:\\oih\\data\\outputs\\t41_*``）——那正好落在 agent 路径
  守卫的允许根里，**守卫拦不住对它的写**。
* agent 侧的守卫本身有已知的缺口（管道、内联解释器、脚本文件都绕得过去），
  所以「靠守卫保证它不改原件」这件事站不住。
* 交付物是这一批唯一拿得出手的东西。**让审核进程看不到原件的路径**，
  才把这件事从「它大概不会乱动」变成「结构上碰不到」。

另外还有两层兜底：原件目录仍被声明为**只读根**（文件工具与破坏性命令层面
一律拒绝写改删），以及审核前后对原件做一次**完整性快照**——动过就作废结论。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import db
import guards
import llm
import store

#: 审核进程的超时（秒）。审核要读产物、可能要跑几条读文件的命令，
#: 比日常任务宽松；但也不能无限期挂着——它是后台任务，挂着就占着一台机器。
REVIEW_TIMEOUT = 900.0

#: 拷进审核环境的产物体积上限（字节）。真实的一行产物在 20–250 MB 之间，
#: 256 MB 能覆盖绝大多数行；超出的部分只进清单（名字 + 大小 + 文本片段）。
COPY_TOTAL_MAX = 256 * 1024 * 1024

#: 拷进审核环境的产物条目数上限。
COPY_ITEMS_MAX = 3000

#: brief 里最多列多少个产物。真实批次见过一行 3703 个产物，
#: 全列出来 brief 会爆，模型也读不完那么长的清单。
MAX_ARTIFACTS_IN_BRIEF = 200

#: brief 里最多内嵌多少个文本产物的内容片段。审核进程自己能读副本，
#: 这几段只是让它**先看一眼**，省得对着几千个文件名发呆。
MAX_SNIPPETS = 20

#: 单个片段最多多少字节。
SNIPPET_MAX = 1200

#: 判定为「文本类」的扩展名。二进制产物给文件名和大小就够了。
_TEXT_SUFFIXES = (".txt", ".md", ".csv", ".tsv", ".json", ".log", ".yaml",
                  ".yml", ".fasta", ".fa", ".pdb", ".cif", ".sdf", ".mol2",
                  ".gjf", ".xml", ".html", ".py", ".sh", ".ps1")

#: verdict.json 里能认的写法。模型不一定会严格照抄 pass/fail，
#: 常见的中英文与同义词都收进来；认不出来就算「没跑成」，**不猜**。
_VERDICT_MAP = {
    "pass": "pass", "passed": "pass", "ok": "pass", "通过": "pass",
    "fail": "fail", "failed": "fail", "reject": "fail", "rejected": "fail",
    "不通过": "fail", "未通过": "fail",
}


class ReviewerError(RuntimeError):
    """审核跑不起来（配置缺失、进程起不来、超时）。"""


# ---------------------------------------------------------------- 目录

def review_dir(task_id: int) -> Path:
    """这个任务的审核工作目录。**与被审任务完全分开**。"""
    return db.data_root() / "reviews" / str(int(task_id))


def copy_dir(task_id: int) -> Path:
    """产物副本目录（在审核自己的工作目录下面）。"""
    return review_dir(task_id) / "artifacts"


def brief_path(task_id: int) -> Path:
    return review_dir(task_id) / "brief.md"


def verdict_path(task_id: int) -> Path:
    return review_dir(task_id) / "verdict.json"


# ---------------------------------------------------------------- 拷副本

def _unique(dst: Path) -> Path:
    """产物名可能撞车（两个目录里各有一个 result.pdb）。撞了就加序号。"""
    if not dst.exists():
        return dst
    stem, suffix = dst.stem, dst.suffix
    i = 2
    while True:
        cand = dst.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def _copy_artifacts(artifacts: list[dict], dest: Path,
                    *, total_max: int = COPY_TOTAL_MAX,
                    items_max: int = COPY_ITEMS_MAX) -> dict:
    """把产物**副本**放进审核环境。

    返回 ``{"copied": [...], "skipped": [...], "bytes": int}``：
    ``copied`` 里每一项是 ``{"name", "rel", "size"}``（rel 是相对副本目录的路径），
    ``skipped`` 是没能拷的（超预算或源不在）。

    源路径**不告诉审核进程**：它只看得到副本，看不到原件在哪。
    """
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[dict] = []
    skipped: list[dict] = []
    budget = int(total_max)
    items = 0

    root = db.data_root()
    for row in artifacts:
        raw = str(row.get("path") or "")
        src = Path(raw)
        # 相对路径按**数据根**解析（产物表里存的是相对工作目录的路径）。
        if raw and not src.is_absolute():
            src = root / src
        name = str(row.get("name") or src.name or "")
        if not src.exists():
            skipped.append({"name": name, "why": "源不在了"})
            continue
        if items >= items_max:
            skipped.append({"name": name, "why": "超出条目上限"})
            continue
        is_dir = src.is_dir()
        if is_dir:
            size = _dir_size(src)
        else:
            try:
                size = src.stat().st_size
            except OSError:
                size = 0
        if size > budget:
            skipped.append({"name": name, "why": "单个就超出体积上限"})
            continue
        dst = _unique(dest / (name or "artifact"))
        try:
            if is_dir:
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        except OSError as exc:
            skipped.append({"name": name, "why": f"拷贝失败：{exc}"})
            continue
        budget -= size
        items += 1
        rel = dst.relative_to(dest).as_posix()
        copied.append({"name": name, "rel": rel, "size": size,
                       "dir": is_dir})

    return {"copied": copied, "skipped": skipped, "bytes": int(total_max - budget)}


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _snapshot(paths: list[str]) -> dict:
    """给一批路径拍个快照（大小 + 修改时间）。**审核前后各拍一次**。

    审核进程理论上碰不到原件（它拿到的是副本），但「理论上」三个字
    不够——交付物是这一批唯一拿得出手的东西。动过就作废结论。
    """
    out: dict[str, tuple[int, int]] = {}
    for raw in paths:
        p = Path(raw)
        try:
            st = p.stat()
            out[raw] = (int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            continue
    return out


def _changed(before: dict) -> list[str]:
    """哪些路径在审核期间变了（大小或修改时间不一样）。"""
    changed = []
    for raw, sig in before.items():
        try:
            st = Path(raw).stat()
            now = (int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            continue                       # 没了也算动过
        if now != sig:
            changed.append(raw)
    return changed


# ---------------------------------------------------------------- brief

def _is_text(name: str) -> bool:
    low = str(name or "").lower()
    return low.endswith(_TEXT_SUFFIXES)


def _snippet(path: Path) -> str:
    """读一小段文本内容。读不出来就返回空串（不猜）。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(SNIPPET_MAX)
    except OSError:
        return ""


def build_review_brief(task: dict, artifacts: list[dict] | None = None,
                       copied: dict | None = None) -> str:
    """组装给审核进程的提示词。

    **判据就是「原始需求 + 产物 + 结论」三样**，不另设「验收标准」输入项：
    需求本身写得很具体时，再要人填一遍验收标准只是多一道手续，
    而且填出来的东西大概率还是需求那句话的复述。
    """
    tid = int(task["id"])
    rows = artifacts if artifacts is not None else store.artifacts.list(tid)
    brief = (task.get("brief") or "").strip()
    summary = (task.get("summary") or "").strip()
    followup = (task.get("followup") or "").strip()
    cdir = copy_dir(tid)

    lines: list[str] = []
    lines.append(f"# 审核任务 #{tid}")
    lines.append("")
    lines.append("## 原始需求（用户提的）")
    lines.append("")
    lines.append(brief if brief else "（这一轮没有需求正文）")
    if followup:
        lines.append("")
        lines.append("### 用户在这一轮追加的要求")
        lines.append("")
        lines.append(followup)
    lines.append("")
    lines.append("## 这一轮计算进程自己写的结论")
    lines.append("")
    lines.append(summary if summary else "（它没有写结论）")
    lines.append("")
    lines.append("## 产物清单")
    lines.append("")
    if not rows:
        lines.append("（没有产物）")
    else:
        total = sum(int(r.get("size") or 0) for r in rows)
        lines.append(f"这一轮一共登记了 {len(rows)} 项产物，合计 {total} 字节。")
        if copied is not None and copied.get("copied"):
            lines.append("")
            lines.append(f"**你看到的是副本**，在 `{cdir}` 下面"
                         f"（共 {len(copied['copied'])} 项，"
                         f"{round(copied['bytes'] / 1048576, 1)} MB）。"
                         "原件在哪你不需要知道，也不要去找。")
            lines.append("")
            for it in copied["copied"][:MAX_ARTIFACTS_IN_BRIEF]:
                tag = "（目录）" if it.get("dir") else ""
                lines.append(f"- `{it['rel']}`{tag}（{it['size']} 字节）")
            if len(copied["copied"]) > MAX_ARTIFACTS_IN_BRIEF:
                lines.append(f"- ……另外还有 "
                             f"{len(copied['copied']) - MAX_ARTIFACTS_IN_BRIEF} 项")
            if copied.get("skipped"):
                lines.append("")
                lines.append("下面这些**没有**副本，你只能从清单上判断：")
                for it in copied["skipped"][:20]:
                    lines.append(f"- `{it['name']}`（{it['why']}）")
                if len(copied["skipped"]) > 20:
                    lines.append(f"- ……另外还有 {len(copied['skipped']) - 20} 项")
        else:
            lines.append("")
            lines.append("产物清单：")
            for r in rows[:MAX_ARTIFACTS_IN_BRIEF]:
                name = r.get("name") or r.get("path") or "?"
                size = int(r.get("size") or 0)
                lines.append(f"- `{name}`（{size} 字节）")
            if len(rows) > MAX_ARTIFACTS_IN_BRIEF:
                lines.append(f"- ……另外还有 {len(rows) - MAX_ARTIFACTS_IN_BRIEF} 个")
    lines.append("")

    # 文本产物给几段内容：审核要能一眼看到「这是不是真东西」，
    # 而不是只对着文件名猜。
    snippets = 0
    src_root = cdir if copied is not None and copied.get("copied") else None
    for it in (copied or {}).get("copied", []) if src_root else []:
        if snippets >= MAX_SNIPPETS:
            break
        if it.get("dir") or not _is_text(it["rel"]):
            continue
        p = src_root / it["rel"]
        if not p.is_file():
            continue
        if it.get("size", 0) > 64 * 1024:
            continue
        body = _snippet(p)
        if not body.strip():
            continue
        snippets += 1
        lines.append(f"### `{it['rel']}` 的开头")
        lines.append("")
        lines.append("```")
        lines.append(body.strip())
        lines.append("```")
        lines.append("")
    if snippets == 0:
        lines.append("（没有可直接内嵌的文本片段；你自己去副本目录里看。）")
        lines.append("")

    lines.append("## 机械硬条件的判定")
    lines.append("")
    lines.append("平台已经查过机械条件并放行了（产物不是空的、自报的产物找得到、"
                 "有结论）。**所以现在问的是需求层面的问题**，"
                 "机械条件判不了的那部分由你来看。")
    lines.append("")
    lines.append("## 你要回答的问题")
    lines.append("")
    lines.append("1. 原始需求里的每一件事，这一轮的产物里有没有对应的东西？")
    lines.append("2. 产物像不像真的算出来的（而不是空壳、占位、明显跑了一半）？")
    lines.append("3. 计算进程自己写的结论，跟产物对得上吗？")
    lines.append("")
    lines.append("## 输出")
    lines.append("")
    lines.append(f"把结论写成一个 JSON 文件：`{verdict_path(tid)}`")
    lines.append("")
    lines.append("```json")
    lines.append('{')
    lines.append('  "verdict": "pass 或 fail",')
    lines.append('  "reason": "一句话说明依据（pass 说清凭什么过，fail 说清哪里不行）",')
    lines.append('  "checks": ["你实际看了哪几样东西"]')
    lines.append('}')
    lines.append("```")
    lines.append("")
    lines.append("你**只能写你自己的工作目录**和上面那个结论文件。"
                 "产物副本随便看、随便读，但**不要去改它们**——"
                 "你看到的就是交付给人的那份。")
    lines.append("写完这个文件就结束，不要做别的事。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 起进程

def _spawn_review_agent(brief_path: Path, work_dir: Path, timeout: float,
                        readonly_roots: list[Path]) -> dict:
    """起一个审核进程。

    **不给任何令牌**：审核进程不是管理员，也不是某个任务的计算进程。
    它能读副本、能联网查判据，别的什么都干不了。
    """
    node = shutil.which("node") or "node"
    script = Path(__file__).resolve().parent.parent / "agent" / "run.mjs"
    env = dict(os.environ)
    env["PROTFORGE_WEB_REVIEW_MODE"] = "task"
    env["PROTFORGE_WEB_REVIEW_BRIEF_PATH"] = str(brief_path)
    env["PROTFORGE_WORK_DIR"] = str(work_dir)
    env["PROTFORGE_WEB_DATA_ROOT"] = str(db.data_root())
    # 只读根：原件所在的目录。能读、不许写。多个用 ';' 分隔（Windows 的分隔符）。
    env["PROTFORGE_WEB_REVIEW_READONLY_ROOTS"] = ";".join(
        str(p) for p in readonly_roots if p)
    # agent 侧的路径守卫要跟服务端用同一个 ProtForge 根（与调度器一致）。
    env.setdefault("PROTFORGE_DATA", guards.DEFAULT_PROTFORGE_ROOT)
    # 模型配置跟着这次审核走：写在审核自己的工作目录里。
    agent_dir, have_key = llm.materialize_model_config(work_dir)
    env["PROTFORGE_AGENT_DIR"] = str(agent_dir)
    env["PROTFORGE_LLM_PROVIDER"] = llm.LLM_PROVIDER_ID
    env["PROTFORGE_LLM_MODEL"] = llm.model_id()
    if not have_key:
        env["PROTFORGE_LLM_KEY_ENV"] = llm.key_env()
    env["PYTHONIOENCODING"] = "utf-8"
    # **不给任何令牌**：审核进程不是管理员，也不是某个任务的计算进程。
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
        return {"ok": False, "returncode": None, "output": "",
                "error": f"审核进程超过 {timeout:.0f} 秒还没结束"}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "returncode": None, "output": "",
                "error": f"审核进程起不来：{exc}"}
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "output": (out + ("\n" + err if err else ""))[-4000:],
            "error": "" if proc.returncode == 0 else f"退出码 {proc.returncode}"}


def _read_verdict(task_id: int) -> tuple[str, str]:
    """读审核进程写的结论。返回 (verdict, reason)。

    读不出来 / 认不出 = ``error``，**不猜**。机器挂了就该说机器挂了，
    猜一个 pass 等于把坏结果当成交付。
    """
    p = verdict_path(task_id)
    if not p.is_file():
        return "error", "审核进程没有写出结论文件"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "error", f"结论文件读不出来：{exc}"
    if not isinstance(data, dict):
        return "error", "结论文件的格式不对（不是一个 JSON 对象）"
    raw = str(data.get("verdict") or "").strip().lower()
    verdict = _VERDICT_MAP.get(raw)
    if verdict is None:
        return "error", f"结论里的 verdict 认不出来：{raw!r}"
    reason = str(data.get("reason") or "").strip()
    return verdict, reason[:4000]


# ---------------------------------------------------------------- 跑一轮

def run_llm_review(task_id: int, *, timeout: float = REVIEW_TIMEOUT,
                   actor: str = "system", keep_copy: bool = False) -> dict:
    """审一个任务。**同步跑**（会阻塞调用方几分钟），后台调用请自己开线程。

    返回一个 dict：``{"verdict": ..., "reason": ..., "applied": bool, ...}``。
    """
    task = store.get_task(task_id, is_admin=True)
    if str(task.get("status")) != "done":
        raise ReviewerError("任务还没算完，审不了")
    tid = int(task_id)
    arts = store.artifacts.list(tid)

    work_dir = review_dir(tid)
    work_dir.mkdir(parents=True, exist_ok=True)
    cdir = copy_dir(tid)
    # 上一次的副本与结论要先清掉：否则这次审核会对着旧的看。
    if cdir.exists():
        shutil.rmtree(cdir, ignore_errors=True)
    vp = verdict_path(tid)
    if vp.exists():
        try:
            vp.unlink()
        except OSError:
            pass

    copied = _copy_artifacts(arts, cdir)
    bp = brief_path(tid)
    bp.write_text(build_review_brief(task, arts, copied), encoding="utf-8")

    # 只读根：原件所在的地方。审核进程拿到的是副本，但把原件也声明成
    # 只读，等于给它再加一道「就算猜到路径也动不了」。
    # **统一成字符串**：这个集合里混过 Path 与 str，排序的时候直接
    # 「'<' not supported between instances of 'str' and 'WindowsPath'」。
    readonly = {str(guards.regions(tid)[guards.WORK])}
    for row in arts:
        raw = str(row.get("path") or "")
        if raw:
            src = Path(raw)
            if not src.is_absolute():
                src = db.data_root() / src
            readonly.add(str(src.parent))
    before = _snapshot([str(r.get("path") or "") for r in arts
                        if r.get("path")])

    try:
        res = _spawn_review_agent(bp, work_dir, timeout, sorted(readonly))
        verdict, reason = _read_verdict(tid)
        if verdict == "error" and res.get("error"):
            reason = f"{reason}；{res['error']}"

        # 完整性核对：审核期间原件被动过 → 结论作废。
        touched = _changed(before)
        if touched:
            verdict = "error"
            reason = (f"审核过程改动了被审任务的产物（{len(touched)} 处），"
                      "结论作废")
    finally:
        if not keep_copy:
            shutil.rmtree(cdir, ignore_errors=True)

    applied = False
    if verdict == "pass":
        store.set_acceptance(tid, db.ACCEPTANCE_ACCEPTED)
        store.add_event(tid, "note", actor,
                        "机器审核通过：" + (reason or "（没写理由）"))
        applied = True
    elif verdict == "fail":
        store.set_acceptance(tid, db.ACCEPTANCE_REJECTED)
        store.add_event(tid, "note", actor,
                        "机器审核没通过：" + (reason or "（没写理由）"))
        applied = True
    else:
        # 审核没跑成：**不动验收状态**。任务留在 pending 等人。
        store.add_event(tid, "note", actor,
                        "机器审核没跑成：" + (reason or "未知原因"))

    store.add_task_review(tid, verdict, reason, reviewer="llm",
                          auto_applied=1 if applied else 0)
    return {"verdict": verdict, "reason": reason, "applied": applied,
            "output": res.get("output", ""), "error": res.get("error", ""),
            "brief_path": str(bp),
            "copied": len(copied.get("copied", [])),
            "skipped": len(copied.get("skipped", []))}
