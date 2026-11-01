# -*- coding: utf-8 -*-
"""工作流的**运行台账**：一行一行把「这一批到底跑成什么样」摊成一张表。

为什么要有它
------------
结果表（``result.xlsx``）是**科学结果**——源表 + 回写的输出列，回答
「这个靶标算出来什么」。它不回答**运行层面**的事：

- 哪些行崩了、崩在哪一步、为什么；
- 哪些行算完了却没人审、哪些被机器审核判了不通过、理由是什么；
- 每行实际跑了多久、产出了多少东西、有没有产物包。

真实批次里这些信息分散在三个地方（``tasks`` / ``events`` / ``task_reviews``），
而交付给别人时**只有一个结果表是不够的**：对方看到「第 42 行失败」，
却看不到「它是额度耗尽，不是靶点难」，也不知道整批的成功率与用时分布。

台账把这三处合到一张表里，**行号与结果表对齐**——同一个「第 42 行」在
两张表里说的是同一件事。

耗时用的是**真实耗时**：起点是「状态变成 running」那条事件，不是
``created_at``。工作流把整批行任务先建出来再串行跑，``created_at`` 是
「整批建好的时刻」，拿它当起点会把「排队等前面的行」也算进去
（实测某批 61 行的表：单行中位 8.4 小时，其中大半是在等前面的行）。
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import db
import store
import status_api
import workflow as wfmod

#: 台账的列。**顺序就是表头顺序**，也是逐行索引 CSV 的列顺序。
COLUMNS: list[tuple[str, str]] = [
    ("行号", "第几个数据行（与结果表对齐，从 1 起）"),
    ("编号", "源表的关键列值"),
    ("靶标", "源表里这一行填的 slot 值"),
    ("状态", "完成 / 失败 / 排队中 / 运行中 / 已跳过"),
    ("验收", "未审 / 待验收 / 通过 / 打回"),
    ("机器审核", "pass / fail / error（没审过则空）"),
    ("审核理由", "机器审核的理由（截断）"),
    ("审核时间", "最近一次机器审核的时间——**对不上就说明那是上一轮的结论**"),
    ("耗时(分钟)", "真实耗时：认领那一刻到结束"),
    ("产物数", "这一行登记了多少个产物"),
    ("产物包(MB)", "产物包的大小"),
    ("结论摘要", "计算进程自己写的结论"),
    ("失败原因", "这一行为什么失败"),
]

#: 状态 → 人话。
STATUS_LABEL = {
    "done": "完成",
    "failed": "失败",
    "queued": "排队中",
    "running": "运行中",
    "packaging": "打包中",
}

#: 验收状态 → 人话。
ACCEPTANCE_LABEL = {
    "none": "未审",
    "pending": "待验收",
    "accepted": "通过",
    "rejected": "打回",
}


def _all_row_values(path: Path, view) -> list[dict[int, str]]:
    """一次读出**所有**数据行的 {0 基列号: 文本}。

    **不要逐行调 ``row_values``**：它每次都开一次工作簿，61 行就是 61 次
    openpyxl 读盘——台账是给人导出用的，几秒钟的卡顿是白挨的。
    """
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        wbr = load_workbook(path, data_only=False, read_only=True)
    except Exception:                                # noqa: BLE001
        wbr = None
    try:
        wsv = wb[view.sheet]
        wsr = wbr[view.sheet] if wbr else wsv
        out: list[dict[int, str]] = []
        for r in view.row_numbers:
            vals = wfmod._row_at(wsv, r, view.col_count)
            raws = wfmod._row_at(wsr, r, view.col_count)
            texts = wfmod._row_texts(vals, raws, view.col_count)
            out.append(dict(enumerate(texts)))
        return out
    finally:
        wb.close()
        if wbr is not None:
            wbr.close()


def _elapsed_minutes(workflow_id: int, tasks: list[dict]) -> dict[int, float]:
    """每行真实耗时（分钟）。没有 running 事件的工作流行任务**不计时**。

    它压根没跑过，``created_at`` 不是它的起点——拿它算出来的数是假的
    （会把「排队等前面的行」算成「这一行跑了这么久」）。
    """
    terminal = [t for t in tasks if str(t.get("status")) in db.TERMINAL_STATUSES]
    running = status_api._running_events([int(t["id"]) for t in terminal])
    out: dict[int, float] = {}
    for t in terminal:
        end = status_api._parse_ts(t.get("done_at"))
        if end is None:
            continue
        begin = status_api._pick_start(running.get(int(t["id"])) or [], end)
        if begin is None:
            continue
        secs = (end - begin).total_seconds()
        if secs >= 0:
            out[int(t["id"])] = round(secs / 60.0, 1)
    return out


def source_labels(workflow_id: int) -> dict[int, dict]:
    """每行的 ``{"key": 关键列值, "slots": [slot 值...]}``。

    **一次读表**：不要逐行调 ``workflow.row_values``——它每次都开一次工作簿，
    61 行就是 61 次 openpyxl 读盘。台账和交付包的条目名都要用它。
    读不到源表就退化成空 dict（台账不能因为源表没了就出不来）。
    """
    wf = store.get_workflow(int(workflow_id))
    wid = int(wf["id"])
    key_col = int(wf["key_col"] or 0)
    slot_cols = sorted(
        (c for c in store.list_workflow_columns(wid)
         if c["role"] in ("input", "context") and c.get("slot")),
        key=lambda c: int(c["col_index"]))
    out: dict[int, dict] = {}
    try:
        source = Path(wf["source_path"])
        view = wfmod.read_source(source, header_row=int(wf["header_row"]))
        values = _all_row_values(source, view)
    except Exception:                                # noqa: BLE001
        values = []
    for idx in range(int(wf["total_rows"] or 0)):
        vals = values[idx] if idx < len(values) else {}
        slots = [str(vals.get(int(c["col_index"]), "") or "").strip()
                 for c in slot_cols]
        out[idx] = {
            "key": str(vals.get(key_col, "") or "").strip(),
            "slots": [s for s in slots if s],
        }
    return out


def build_rows(workflow_id: int) -> list[dict]:
    """台账数据（每行一个 dict，键是 :data:`COLUMNS` 里的列名）。

    **行号与结果表对齐**：第 N 行就是工作流的第 N 个数据行，无论它跑过没有。
    没跑过的行也在表里——「哪些行还没跑」本身就是交付的一部分。
    """
    wf = store.get_workflow(int(workflow_id))
    wid = int(wf["id"])
    skips = {int(x) for x in store.list_workflow_skips(wid)}

    tasks = store.list_workflow_rows(wid)
    tasks = [dict(t) for t in tasks]
    by_row = {int(t.get("workflow_row") or 0): t for t in tasks}

    elapsed = _elapsed_minutes(wid, tasks)
    labels = source_labels(wid)

    out: list[dict] = []
    for idx in range(int(wf["total_rows"] or 0)):
        t = by_row.get(idx)
        lab = labels.get(idx) or {"key": "", "slots": []}
        key = lab["key"]
        target = " ".join(lab["slots"])

        if idx in skips:
            state = "已跳过"
        elif t is None:
            state = "未开始"
        else:
            state = STATUS_LABEL.get(str(t.get("status") or ""), str(t.get("status") or ""))

        acceptance = ""
        verdict = ""
        reason = ""
        reviewed_at = ""
        arts = 0
        pkg_mb = 0.0
        summary = ""
        error = ""
        minutes = None
        if t is not None:
            tid = int(t["id"])
            acceptance = ACCEPTANCE_LABEL.get(str(t.get("acceptance") or ""), "")
            try:
                latest = store.latest_task_review(tid)
            except Exception:                        # noqa: BLE001
                latest = None
            if latest:
                verdict = str(latest.get("verdict") or "")
                reason = str(latest.get("reason") or "").strip()[:600]
                # **审核时间**：这一行后来又重跑过的话，最近一次审核是**上一轮**
                # 的结论——台账里「机器审核 fail / 验收 未审」看着像自相矛盾，
                # 有了时间才看得出那是哪一轮的事。
                reviewed_at = str(latest.get("created_at") or "").strip()
            try:
                arts = len(store.artifacts.list(tid))
            except Exception:                        # noqa: BLE001
                arts = 0
            pkg = str(t.get("package_path") or "")
            if pkg:
                p = Path(pkg)
                if not p.is_absolute():
                    p = db.data_root() / pkg
                try:
                    pkg_mb = round(p.stat().st_size / 1048576.0, 2)
                except OSError:
                    pkg_mb = 0.0
            summary = str(t.get("summary") or "").strip()[:800]
            error = str(t.get("error") or "").strip()[:400]
            minutes = elapsed.get(tid)

        out.append({
            "行号": idx + 1,
            "编号": key,
            "靶标": target,
            "状态": state,
            "验收": acceptance,
            "机器审核": verdict,
            "审核理由": reason,
            "审核时间": reviewed_at,
            "耗时(分钟)": "" if minutes is None else minutes,
            "产物数": arts,
            "产物包(MB)": pkg_mb,
            "结论摘要": summary,
            "失败原因": error,
        })
    return out


def build_xlsx_bytes(rows: list[dict], title: str = "运行台账") -> bytes:
    """把台账写成 xlsx 的**字节**。

    **不直接写文件**：openpyxl 存盘失败时它自己那个 ZipFile 不会关闭，
    文件被锁着——Windows 上锁住的文件删不掉，而临时目录的清理用的正是
    ``shutil.rmtree(..., ignore_errors=True)``，它会把「删不掉」这个失败
    **静默吞掉**，于是临时目录留在磁盘上（几十 GB 的包没人会知道）。
    先写成字节再落盘就没有这个问题。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append([c[0] for c in COLUMNS])
    head_fill = PatternFill("solid", fgColor="E8EDF3")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([r.get(c[0], "") for c in COLUMNS])
    widths = {"行号": 6, "编号": 14, "靶标": 22, "状态": 9, "验收": 9,
              "机器审核": 10, "审核理由": 46, "审核时间": 21, "耗时(分钟)": 11,
              "产物数": 8, "产物包(MB)": 11, "结论摘要": 46, "失败原因": 34}
    for i, (name, _) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = \
            widths.get(name, 14)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_xlsx(rows: list[dict], path: Path, title: str = "运行台账") -> Path:
    """把台账写成 xlsx 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_xlsx_bytes(rows, title))
    return path


def build_index_csv(rows: list[dict]) -> str:
    """逐行索引（CSV）。**UTF-8 带 BOM**——Excel 直接双击打开不乱码。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([c[0] for c in COLUMNS])
    for r in rows:
        w.writerow([r.get(c[0], "") for c in COLUMNS])
    return buf.getvalue()


def build_review_conclusions(workflow_id: int, rows: list[dict]) -> str:
    """机器审核结论（Markdown）。**没审过的行也要写出来**——
    「21 行算完了但没人审」本身就是要交付的事实。"""
    wf = store.get_workflow(int(workflow_id))
    out: list[str] = []
    out.append("# 机器审核结论")
    out.append("")
    out.append("工作流：%s（%s）" % (wf.get("name") or "", wf.get("sheet") or ""))
    out.append("")
    reviewed = [r for r in rows if r.get("机器审核")]
    passed = [r for r in reviewed if r.get("机器审核") == "pass"]
    failed = [r for r in reviewed if r.get("机器审核") == "fail"]
    errored = [r for r in reviewed if r.get("机器审核") == "error"]
    unreviewed = [r for r in rows if not r.get("机器审核")]
    out.append("审过 %d 行：通过 %d、不通过 %d、没跑成 %d；**没审过 %d 行**。"
               % (len(reviewed), len(passed), len(failed), len(errored),
                  len(unreviewed)))
    out.append("")
    out.append("> 「没跑成」不等于「不通过」：审核进程没跑起来时，平台**不改**"
               "验收状态，这一行留在待验收等人。")
    out.append("")
    for title, group in (("不通过", failed), ("没跑成", errored),
                         ("通过", passed)):
        if not group:
            continue
        out.append("## %s（%d 行）" % (title, len(group)))
        out.append("")
        for r in group:
            out.append("### 第 %s 行 · %s" % (r.get("行号"), r.get("靶标") or "—"))
            out.append("")
            out.append("- 机器结论：%s" % (r.get("机器审核") or "—"))
            out.append("- 验收状态：%s" % (r.get("验收") or "—"))
            if r.get("审核时间"):
                out.append("- 审核时间：%s" % r.get("审核时间"))
            reason = str(r.get("审核理由") or "").strip()
            if reason:
                out.append("- 理由：%s" % reason)
            out.append("")
    if unreviewed:
        out.append("## 没审过的行（%d 行）" % len(unreviewed))
        out.append("")
        out.append("| 行号 | 靶标 | 状态 | 耗时(分钟) |")
        out.append("| --- | --- | --- | --- |")
        for r in unreviewed:
            out.append("| %s | %s | %s | %s |"
                       % (r.get("行号"), r.get("靶标") or "—",
                          r.get("状态"), r.get("耗时(分钟)")))
        out.append("")
    return "\n".join(out)


def build_readme(workflow_id: int, rows: list[dict]) -> str:
    """交付说明。**写清楚这个包是什么、里面有什么、有什么没解决。**"""
    wf = store.get_workflow(int(workflow_id))
    out: list[str] = []
    out.append("# 交付说明")
    out.append("")
    out.append("工作流：**%s**（工作表 %s）" % (wf.get("name") or "",
                                          wf.get("sheet") or ""))
    out.append("")
    out.append("本包由自动化计算平台生成，包含这一批 %d 个数据行的**逐行交付物**"
               "与运行情况。" % len(rows))
    out.append("")
    out.append("## 里面有什么")
    out.append("")
    out.append("| 文件 | 是什么 |")
    out.append("| --- | --- |")
    out.append("| `00-交付说明.md` | 本文件 |")
    out.append("| `01-逐行索引.csv` | 每一行的状态、验收、耗时、产物数（UTF-8 带 BOM，"
               "Excel 双击可开） |")
    out.append("| `02-运行台账.xlsx` | 同一份数据的表格版 |")
    out.append("| `03-机器审核结论.md` | 机器审核判了什么、理由是什么、哪些行没审过 |")
    out.append("| `10-各行产物包/` | 每一行自己的产物包（结构、序列、脚本、报告） |")
    out.append("")
    out.append("## 这一批的运行情况")
    out.append("")
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r.get("状态") or "")] = counts.get(str(r.get("状态") or ""), 0) + 1
    out.append("| 状态 | 行数 |")
    out.append("| --- | --- |")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        out.append("| %s | %d |" % (k or "—", v))
    out.append("")
    done_min = [float(r["耗时(分钟)"]) for r in rows
                if r.get("耗时(分钟)") not in ("", None)]
    if done_min:
        done_min.sort()
        mid = done_min[len(done_min) // 2] if len(done_min) % 2 else (
            (done_min[len(done_min) // 2 - 1] + done_min[len(done_min) // 2]) / 2)
        out.append("单行真实耗时：中位 **%.1f 分钟**（最短 %.1f、最长 %.1f，"
                   "共 %d 行有数）。"
                   % (mid, done_min[0], done_min[-1], len(done_min)))
        out.append("")
        out.append("> 耗时是**认领那一刻到结束**，不含排队等前面行的时间。")
        out.append("")
    out.append("## 读这张表要注意的")
    out.append("")
    out.append("- 「结果表」是科学结果（每个靶标算出了什么）；本包里的台账是"
               "**运行情况**（哪行崩了、为什么、跑了多久）。两者的行号对齐。")
    out.append("- 「机器审核」为 `fail` 的行，验收状态是「打回」；为 `error` 的行"
               "**验收状态不动**——审核没跑成不等于不通过，留在「待验收」等人。")
    out.append("- 「机器审核」记的是**最近一次**审核的结论。**这一行后来又重跑过"
               "的话，那条结论是上一轮的**——看「审核时间」对不对得上；对不上时"
               "「验收」是「未审」或「待验收」，那是**对的**：新一轮还没审。")
    out.append("- 没有产物包的行（崩在计算阶段）不会出现在 `10-各行产物包/` 里，"
               "但它们在台账里——**少了哪一行本身就是交付的一部分**。")
    out.append("")
    return "\n".join(out)
