"""FastAPI 主服务。**两个 app 实例，两个端口**，不是一台机器两个 app。

    create_public_app()    -> 0.0.0.0:8473    用户 API + 用户面板前端
    create_internal_app()  -> 127.0.0.1:8475  内部 API（本机反代不该碰）

为什么不能合成一个 app + 检查来源 IP：用户的反代跑在本机，公网请求经反代
转发之后 `request.client.host` **同样是 127.0.0.1**——有反代时"检查来源 IP"
等于不检查。所以隔离只能靠物理层：内部端口只绑回环地址，且反代不转发它。
路由不共存是第二道闸（public app 上根本没有 /api/internal/*），测试钉死这一条。

**没有 /advance 端点**：状态推进全由系统触发（调度器接走 / agent 收口 /
打包成功 / 中止超时）。用户能推 running → packaging 就绕过了完成判定。

`detail` 恒为中文字符串，含 pydantic 框架层错误——数组透出去前端拿到
"[object Object]"。做法抄 `D:\\coding\\creative-panel\\server\\app.py:74`。

**凡是本文件写完事件的地方，后面都跟一句 `sse.notify()`。** 那是 SSE 内存总线
的叫醒钩子，写漏了这条事件只能等 3 秒轮询才到用户眼前（不会丢，只是迟）。
store.py 属于别的 worker，数据层不该知道有没有人在听，所以叫醒钩子放在 API 层。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import (Depends, FastAPI, File, HTTPException, Query, Request,
                    UploadFile)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

import artifacts
import auth
import db
import dispatcher
import guards
import janitor
import reflection
import skills_index
import sse
import store

VERSION = "0.1.0"

log = logging.getLogger("protforge.web")

# 用户面板前端。挂在仓库里而不是数据盘：它是程序的一部分，不该跟着数据一起被清。
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
USER_UI_FILES = frozenset({"index.html", "app.js", "style.css",
                           "workflow.html", "workflow.js"})

PUBLIC_PORT = 8473
INTERNAL_PORT = 8475

# 参数名 -> 中文标签。pydantic 的 loc 里全是英文 key，不翻的话错误提示是
# "title 不合法"这种夹生文案。
#
# **也是单一来源**：admin/app_admin.py import 下面这个名字，不要再抄一份。
# 抄过一次就已经漂了——admin 侧那份少了 `error`，`brief` 还写成了「需求说明」
# 而这边是「需求描述」，两边注释还互相矛盾。键集合一旦漂了两边就永远对不上，
# 所以新加参数名只加这里。
#
# `brief` 的译法取「需求说明」：这是界面上真实叫的名字（`web/index.html`、
# `web/app.js` 都是它），报错里换个说法等于让用户去找一个界面上不存在的字段。
PARAM_LABEL = {
    "username": "用户名", "password": "密码", "title": "标题",
    "brief": "需求说明", "text": "步骤文本", "kind": "事件类型",
    "message": "内容", "summary": "结论", "error": "错误信息",
    "task_id": "任务编号", "status": "状态",
    # 以下是管理员面板多出来的参数名
    "role": "角色", "ok": "确认结论", "command": "干预命令", "model": "模型名",
    "thinking_level": "思考档位", "limit": "条数", "after_id": "游标",
    "artifact_id": "产物编号", "enabled": "停用状态",
}

# **错误文案的单一来源。** admin/app_admin.py import 下面这两个名字，不要再抄一份：
# 抄过一次就已经漂了（app 侧有 422/503、admin 侧没有，两边的注释还互相矛盾），
# 同一个框架层错误在两个面板上显示成两句话。
#
# 键的含义是「**框架层自己抛的** HTTPException 默认 detail 是英文短语，翻成中文」。
# 谁抛的决定它走不走这张表：`user_detail()` 先认我们自己的异常类，认不出才查表。
# 所以带具体中文原因的**必须** raise PreciseHTTPException，而不是指望"这张表里
# 没配这个码就没事"——那只是让覆盖晚点发生（下一个人给 409 补个条目，
# 「结果包还在生成中」就静默变成「请求冲突」）。
#
# **401/403 刻意不配条目**：auth.py 的 Unauthorized/Forbidden 也走这条兜底，
# 而它们没法用我们的子类（auth 被 app import，反过来会成环）。它们抛的 detail
# 本来就是中文，原样透出即可。
HTTP_DETAIL = {
    400: "请求不对", 404: "接口不存在", 405: "请求方法不对",
    409: "请求冲突", 413: "内容太大", 422: "请求参数不合法",
    500: "服务器内部错误", 503: "服务暂时不可用",
}


def user_detail(exc, own=()) -> str:
    """把一个 StarletteHTTPException 变成一句给用户看的中文 detail。

    规则只有一条：**我们自己抛的（own 里的那些类）原样返回，绝不被通用话覆盖。**
    查表的只该是框架层兜底的那些（路由不存在、方法不对、请求体不合法）。两个面板
    共用这一个函数，所以同一个错在两个面板上必然是同一句话。
    """
    if isinstance(exc, own):
        return str(exc.detail)
    return HTTP_DETAIL.get(exc.status_code, str(exc.detail))


def validation_message(exc: RequestValidationError) -> str:
    """把 pydantic 的错误数组翻成一句人话。

    下游都假定 detail 是字符串：前端 `throw new Error(d.detail)`、CLI 直接
    打印。数组透出去的话前端拿到 "[object Object]"，而两种形态混在同一个
    字段里，下游没法写匹配的逻辑。

    文案按 loc 定到具体参数；认不出来的退回一句通用话，**不把 pydantic 原文**
    （Input should be ...）甩给人看。
    """
    msgs: list[str] = []
    for err in exc.errors():
        loc = tuple(err.get("loc") or ())
        kind = err.get("type") or ""
        name = PARAM_LABEL.get(loc[1], loc[1]) if len(loc) > 1 else ""
        if loc[:1] == ("body",):
            if len(loc) == 1:
                msg = "缺少请求体"
            elif kind == "missing":
                msg = f"缺少{name}"
            elif kind in ("json_invalid", "model_attributes_type"):
                msg = "请求体不是合法的 JSON"
            else:
                msg = f"「{name}」不合法"
        elif loc[:1] == ("path",):
            msg = f"{name or '路径参数'}必须是整数"
        elif loc[:1] == ("query",):
            msg = f"{name or '查询参数'}不合法"
        else:
            msg = "请求参数不合法"
        if msg not in msgs:
            msgs.append(msg)
    return "；".join(msgs) or "请求参数不合法"


def _install_common(app: FastAPI) -> None:
    """两个 app 共用的异常处理器与 /api/health。detail 全局只有一种形态。"""
    _warn_if_dev_login()          # 三个 app 都过这里，所以只喊一次就够

    @app.exception_handler(store.NotFound)
    async def _not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(store.Invalid)
    async def _invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(auth.Invalid)
    async def _auth_invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    # 框架层（pydantic）错误：少了它，同一个 {"detail": ...} 下会同时存在 str 和数组。
    @app.exception_handler(RequestValidationError)
    async def _framework_error(request, exc):
        return JSONResponse(status_code=422,
                            content={"detail": validation_message(exc)})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request, exc):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": user_detail(exc, PreciseHTTPException)})

    @app.get("/api/health")
    def health():
        # 技能数放进健康检查，是"这台机器还能不能算"的第一个信号。
        # 扫目录可能因为环境不全失败，所以失败时报 null 而不是让健康检查 500。
        try:
            skills = len(skills_index.scan())
        except Exception:                                   # noqa: BLE001
            skills = None
        return {"ok": True, "version": VERSION, "skills": skills}


def _task_view(task: dict) -> dict:
    """任务视图：扁平的 tasks 行 + 中文状态标签。"""
    out = dict(task)
    out["status_label"] = db.STATUS_LABEL.get(task["status"], task["status"])
    return out


def _detail(task: dict) -> dict:
    return {"task": _task_view(task),
            "events": store.list_events(task["id"]),
            "artifacts": store.artifacts.list(task["id"])}


def _delete_task_row(task_id: int) -> None:
    """删 tasks 一行，uploads / artifacts / events 靠 schema 的 ON DELETE CASCADE 清。

    业务 SQL 本该在 store.py，这里直接写是因为本 task 的文件归属只有
    auth.py / app.py / 两个测试文件，store.py 属于另一个 worker——不去抢。
    依赖 db.connect() 的 `foreign_keys=ON`（每连接生效，不是建库属性），
    级联真的生效由 test_delete_task_cascades_to_events_and_artifacts 钉死。

    **只删数据库**：work/<id>/、uploads/<id>/、packages/ 一律不动，磁盘清理由
    janitor.py 负责（7/14/30 天保留）。数据库里查不到的东西仍然可以回收，
    反过来则不然。

    **但反思正文不在这个约定里**：它不是「工作区的产物」，而是那条反思记录
    自己的正文——记录被级联删掉，正文就成了没人认领的残留（见
    :func:`_purge_reflection_files`）。所以删任务时顺手把它清掉。
    """
    _purge_reflection_files(task_id)
    c = db.connect()
    try:
        with c:
            c.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
    finally:
        c.close()


# --- 删干净：磁盘上的残留 --------------------------------------------------------------
#
# 两条删除路径原来都只删数据库，于是盘上留下了没人认领的文件：
#   * 删工作流 → 源表与结果表还在 data/workflows/<id>/；
#   * 删任务 → 反思正文还在 data/reflections/task-N.md。
#
# janitor 也不管这两块（它只扫 work/uploads/logs/packages），所以不自己清，
# 就再没有人清。留着不只是浪费盘：工作流的结果表里还有上一批的结果，
# 而反思正文留着，下一次同一条任务重跑时新旧正文会混在同一个路径上。


def _unlink_under(base: Path, path: Path) -> bool:
    """删一个文件，但**只在它真的落在 base 底下时才删**。

    位置闸不是形式主义：路径来自数据库，而数据库里的路径只说明「是哪个文件」，
    不说明「可以删哪儿」。janitor 也是这么判的（它直接复用 guards._within）。
    """
    try:
        real = Path(guards._resolve(path))
        root = Path(guards._resolve(base))
    except (guards.Violation, OSError, ValueError, RuntimeError):
        return False
    if real == root or not guards._within(real, root):
        log.warning("路径 %s 不在 %s 底下，不删", real, root)
        return False
    try:
        real.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.warning("删不掉 %s：%s", real, e)
        return False


def _purge_workflow_files(wf: dict) -> None:
    """把工作流留在盘上的东西删掉：源表、结果表，以及它们所在的目录。

    目录**空了才删**：里面还有别的东西时留着（不猜、不递归删）。
    """
    base = _workflow_root()
    for key in ("source_path", "result_path"):
        raw = (wf.get(key) or "").strip()
        if raw:
            _unlink_under(base, Path(raw))
    try:
        d = base / str(int(wf["id"]))
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _purge_reflection_files(task_id: int) -> None:
    """把一条任务留下的反思正文删掉。

    删任务会级联删掉 reflections 里那一条（外键 ON DELETE CASCADE），但**正文
    是文件**——级联管不到它。于是库里那条没了，盘上的 task-N.md 还在：没人再
    找得到它，也没人会清它。

    方案文件（refl-<id>.patch / .new）跟那条反思一起走：它们是那条反思的
    派生物，同样没人认领。
    """
    try:
        rec = store.get_reflection_by_task(task_id)
    except Exception:                                 # noqa: BLE001
        rec = None
    if not rec:
        return
    body = (rec.get("path") or "").strip()
    if body:
        _unlink_under(reflection.reflections_dir(), Path(body))
    try:
        rid = int(rec.get("id"))
    except (TypeError, ValueError):
        return
    for name in (f"refl-{rid}.patch", f"refl-{rid}.new"):
        _unlink_under(reflection.proposals_dir(),
                      reflection.proposals_dir() / name)


# ---------------------------------------------------------------- 工作流

# 控制动作白名单。**必须显式校验，不能靠类型标注**：action 是路径参数（用户可控
# 的字符串），不校验的话拼错的 action、`../admin/xxx` 这类路径穿越尝试都会落到
# 同一条控制流上——按 else 分支执行就是「用户点错了按钮，平台把整批中止了」。
#
# 刻意不写成 `Literal["start", ...]`：那个标注被改成 str 时，下面这一行还在，
# 围栏照样红；反过来只靠标注的话，"删掉白名单"等价于"改成 str"，而失败方式是
# **静默中止整批**——不报任何错。
WORKFLOW_ACTIONS = frozenset({"start", "pause", "resume", "abort"})

# 上传建记录时提示词模板还没配（要等 POST /api/workflows 写）。store.create_workflow
# 拒空模板（它不是可空列），所以先落这个**哨兵值**，并在 start 时拒它——悄悄填个
# 真模板的话，用户不配模板直接点「开始」，每一行都会拿同一句占位文本去白烧一次
# 完整计算，而且时间线上看不出异常。
PROMPT_PLACEHOLDER = "（还没配置提示词模板）"


def _workflow_root() -> Path:
    """工作流源文件目录。**刻意在 guards.regions() 四区之外**——
    agent 不该有权限碰用户的表格，回写一律由平台做（规格 4.4）。"""
    return db.data_root() / "workflows"


def _wf_view(wf: dict, stats: dict | None = None) -> dict:
    """对外的工作流视图。

    **writeback 原样返回**（规格 2 决策表「回写是可开关」/ 7.1 / 9.1）：用户
    要有手段关掉回写——批量场景下表里有敏感序列时，回写会整表复制到磁盘、
    还可被下载。一个「前端能勾、后端不读」的开关比没有开关更危险，所以接口
    接受它、返回它，归档路径也真的按它决定要不要写 result.xlsx。

    另附一个 ``result_ready``：**服务端算的「结果表现在能不能下」**。前端拿它
    决定下载入口显不显示——判据是「有没有一张已经生成的结果表」，而不是
    ``status == 'finished'``。后者是 M-1 死路的后半段：中止之后工作流停在
    paused，界面把下载入口永久藏起来，用户既看不到已算出的部分结果、也没有
    任何办法拿到。而规格 7.1 要的是「随时能下载**当前进度**」。
    """
    out = dict(wf)
    raw = (out.get("result_path") or "").strip()
    out["result_ready"] = bool(raw) and Path(raw).is_file()
    # 行级统计**只在列表端点挂上来**（_row_stats 那条 SQL 已经是一次 GROUP BY，
    # 别的端点拿不到聚合值就不给这个键）。不给键比给一个默认 0 强：前端区分
    # 「真的 0 行失败」和「这里没有这个数」，后者不能显示成「没有失败行」。
    if stats is not None:
        out["failed_rows"] = int(stats["failed_rows"])
        out["current_row"] = stats["current_row"]
        out["current_status"] = stats["current_status"]
    return out


def _row_packages(workflow_id: int) -> list[tuple[dict, Path]]:
    """这一批里**现在就能下载**的行产物包：行任务 + 包文件路径。

    判据三条，都跟单任务下载端点同源：
    ① 行任务有 package_path（没打包完的行没有）；
    ② 路径过 guards.resolve_package（只认本任务自己的那一个包，
       别人任务的文件、packages 目录下的别的文件都进不来）；
    ③ 文件真的在。

    为什么单独一个函数：详情端点要报**件数与总大小**（前端据此显示
    「N 个 · X MB」并决定要不要给入口），下载端点要报**清单**。
    两处判据必须一致，所以只能有一份实现。
    """
    out: list[tuple[dict, Path]] = []
    for row in store.list_workflow_rows(workflow_id):
        raw = (row.get("package_path") or "").strip()
        if not raw:
            continue
        try:
            real = guards.resolve_package(int(row["id"]), raw)
        except guards.Violation:
            continue
        if real.is_file():
            out.append((row, real))
    return out


def _zip_entry_name(text: str) -> str:
    """把源表里的值变成能当 zip 条目名的片段。

    zip 条目名里带 ``/`` 会被当成子目录，带 ``\\`` 和控制字符更糟。
    所以只保留「非路径分隔符、非控制字符」的那些，其余换成下划线。
    """
    cleaned = "".join(c if (c.isprintable() and c not in '/\\:*?"<>|') else "_"
                      for c in str(text or ""))
    return cleaned.strip().strip(".")[:60] or "未命名"


# 行任务里「还没收工」的那些状态。**从 db 里推，不另抄一份词表**：抄一份的话，
# 将来加了状态这里不会跟着变，卡片就会把一行永远算成「不在跑」。
_LIVE_STATUSES = tuple(s for s in db.VALID_STATUSES if s not in db.TERMINAL_STATUSES)


def _row_stats(workflow_ids) -> dict:
    """每个工作流的行级统计：已经失败的行数 + 当前在跑第几行（规格 8.2 列表那条）。

    **一条 GROUP BY 拿全，不按工作流逐个查。** 列表页有 N 张卡片，逐个查就是
    N 次往返，而每张卡片背后的行任务可能有几百行（500 行的工作流一次卡片刷新
    就是 500 行 SELECT）。

    为什么算而不是存：workflows 表里加一列失败计数，就得在每次行状态迁移时维护
    它，而漏维护的那一次**永远查不出来**——卡片上会一直挂着一个与真实行任务对不
    上的数。对不上的失败数比没有失败数更坏：用户照着它去排查，排查的是一件
    根本没发生过的事。行任务是唯一事实，这里每读一次都现算。

    ``current_row`` 取非终态行里最小的 workflow_row：工作流是**串行**的
    （规格 6.3 闸门②），同一时刻至多有一行没进终态，所以最小的那行就是当前行。
    连它的 status 一起返回，规格 7.5 要的「第 N 行停在打包中」才写得出来——
    只报行号的话，packaging 卡住与计算中在卡片上长得一模一样。

    **跳过的行不算失败（Major-1）**：用户在库里看到的跳过行，status 是
    ``failed``（补偿任务建出来就是终态，db.VALID_STATUSES 里没有 ``skipped``——
    加它是 CHECK 迁移，表重建级别，见 fix2-report 的取舍），而同一行在结果表里
    写的是「已跳过」。这里不排除的话就是**表说一套、卡片说另一套**：跳 2 行 →
    表里两行「已跳过」，卡片标红「失败 2 行」，「只看失败行」也把它们列出来。
    用户分不清「这行是我跳过的」（不用管）和「这行跑失败了」（要查）——
    而这两件事的处理方式正好相反。

    判据用 ``workflow_skips``（唯一事实）而不是 status：``failed`` 这一个值
    同时装着两种完全相反的语义，按 status 分不开。
    """
    ids = [int(i) for i in workflow_ids]
    out = {i: {"failed_rows": 0, "current_row": None, "current_status": None}
           for i in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT workflow_id, status, COUNT(*), MIN(workflow_row)"
            f" FROM tasks WHERE workflow_id IN ({marks})"
            # **排除被跳过的行**（见上面的 docstring）。workflow_row 为 NULL 的
            # 行（普通任务）不在 workflow_skips 里，NOT EXISTS 判不出来就被留下，
            # 所以这条不影响非工作流任务。仍然是**一条** GROUP BY，没有 N+1。
            " AND NOT EXISTS (SELECT 1 FROM workflow_skips s"
            " WHERE s.workflow_id = tasks.workflow_id"
            " AND s.row_index = tasks.workflow_row)"
            " GROUP BY workflow_id, status", ids).fetchall()
    finally:
        c.close()
    for r in rows:
        wid = int(r["workflow_id"])
        if wid not in out:
            continue
        if r["status"] == "failed":
            out[wid]["failed_rows"] = int(r[2])
        elif r["status"] in _LIVE_STATUSES:
            row = int(r[3])
            # 同一工作流有多条非终态行时取最小的——串行批次里第一条就是当前行。
            # 「管理员重跑」会造出额外行，但那些行的 workflow_row 更大，
            # 不该盖掉正跑的那一行。
            if out[wid]["current_row"] is None or row < out[wid]["current_row"]:
                out[wid]["current_row"] = row
                out[wid]["current_status"] = r["status"]
    return out


def _has_output_columns(workflow_id: int) -> bool:
    """配没配结果列。**判据只有这一个**——它与 writeback 开关正交：
    「配不配结果列」决定有没有东西可写，「开不开回写」决定写不写到磁盘。"""
    return any(c["role"] == "output" and (c.get("field") or "").strip()
               for c in store.list_workflow_columns(workflow_id))


def _writeback_enabled(wf: dict) -> bool:
    """回写开关开没开（规格 7.1 / 9.1「开关打开时才有」）。"""
    return bool(int(wf.get("writeback") or 0))


def save_upload(filename: str, data: bytes, owner_id: int) -> dict:
    """落盘 + 建库记录。**先 staging、解析成功再建记录**（规格 4.5 ③）。

    反过来（先建记录再解析）的话，解析失败要回滚一行记录加一个目录，
    两处都得清，漏一处就留下垃圾。
    """
    import workflow as wfmod
    wfmod.check_upload(filename, data)          # 魔数 / 扩展名 / 大小 / 空文件
    root = _workflow_root()
    staging = wfmod.staging_path(root)
    staging.write_bytes(data)
    try:
        view = wfmod.read_source(staging)
    except Exception:
        staging.unlink(missing_ok=True)         # 解析失败：库里不留痕，磁盘不留垃圾
        raise
    wf = store.create_workflow(
        owner_id, wfmod.safe_display_name(filename), PROMPT_PLACEHOLDER,
        sheet=view.sheet, source_path="", header_row=1, key_col=0,
        total_rows=len(view.row_numbers))
    source = wfmod.finalize_upload(root, staging, int(wf["id"]))
    store.update_workflow(int(wf["id"]), source_path=str(source))
    store.set_workflow_columns(int(wf["id"]), [
        {"col_index": i, "header": h, "role": ("key" if i == 0 else "ignore")}
        for i, h in enumerate(view.headers)])
    return {"workflow": _wf_view(store.get_workflow(int(wf["id"]))),
            "headers": view.headers,
            "row_count": len(view.row_numbers), "preview": view.preview}


def _abort_live_rows(workflow_id: int) -> list[int]:
    """把在跑的行逐个写成 failed（actor=human）——**这是唯一的跨进程中止通道**。

    不要试图去拿调度器实例调 abort：``scripts/start-services.ps1:41`` 对 public /
    internal / admin **三个进程都设了 WORKERS_ENV=1**，所以三个进程各自建了
    一个 Dispatcher 并 start()（``app.py:799-801``），靠 claim_queued 的 CAS
    决定谁真的起进程。于是 public 里的 app.state.dispatcher 虽然是活的，它的
    running 里**通常没有别人的 agent**——对着它调 abort 是对着空字典操作，静默无效。

    真正管用的是数据库：现有 abort 路由（``app.py:487-491``）就只写库，持有该
    agent 的那个 dispatcher 在下一次 _reap（<=1 秒）里经 _abort_signal 看到终态
    且 actor 是人，于是真去杀进程。这里照抄那一套，不发明新机制。
    """
    out = []
    for row in store.list_workflow_rows(workflow_id):
        if row["status"] not in db.TERMINAL_STATUSES:
            store.set_status(row["id"], "failed", actor="human",
                             message="工作流中止")
            store.update_task(row["id"], error="工作流中止")
            out.append(int(row["id"]))
    return out


def _settle_abort_signal(seconds: float) -> None:
    """给中止信号留出投递时间，然后**不假装确认过**。

    public 进程看不到 agent 是否真死了（不在它的 running 里），能观测到的
    只有数据库。所以这不是「等进程退出」，只是让持有它的那个 dispatcher 至少
    完成一轮 _reap。**绝不能先删记录再等**——删完 _db_status 立刻返回 ""，
    中止信号根本产生不出来，那正是「删工作流留下一个烧到自然结束的僵尸」的
    成因（规格 6.7）。
    """
    import time
    time.sleep(seconds)


# ---------------------------------------------------------------- 请求体

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=store.TITLE_MAX)
    brief: str = Field(default="", max_length=store.BRIEF_MAX)


class TaskPatch(BaseModel):
    """只允许改标题与需求正文。

    **没有 status**：用户侧没有状态推进权（也没有 /advance），多传的 status
    由 pydantic 默认忽略——既不是 400（前端不该为一个非法字段整次失败），
    更不是静默生效。

    `None`（不传 / 显式 null）一律当"这个字段不动"，所以模型里全是可空：
    要清空需求正文传 `""`，不传就是保持原样。
    """

    title: str | None = Field(default=None, max_length=store.TITLE_MAX)
    brief: str | None = Field(default=None, max_length=store.BRIEF_MAX)


class StepIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class EventIn(BaseModel):
    # kind 收窄到 agent 能写的那几种：created/status/finished 由系统写，
    # agent 冒充它们就能自己宣布任务完成。
    kind: Literal["log", "note", "error"] = "log"
    message: str = Field(default="", max_length=8000)


class AuditIn(BaseModel):
    """计算进程上报的一条命令审计（spec §9.2 的 `audit_log`）。

    kind 收窄到**它自己产生**的三种。`admin_rpc` 之类的留给管理员端点，
    不放开的话 agent 可以往审计表里写"管理员干预"，把追责记录伪造掉。

    `elapsed_ms` 有上限：字段来自 agent 进程，一个传 10^18 的耗时就能把
    任何按时间排序/聚合的查询撑爆，这不是模型会算错的数，是不可信的输入。
    """
    kind: Literal["bash", "bash_denied", "tool"] = "bash"
    # 命令原文。**不截断语义**：管理端 CSV 导出与"是不是有 rm"的判断
    # 都建立在这一列上，宁可长也别替它决定哪里可以省略。
    command: str = Field(default="", max_length=8000)
    exit_code: int | None = None
    elapsed_ms: int | None = Field(default=None, ge=0, le=86_400_000)


class PreciseHTTPException(HTTPException):
    """detail 原样返回，不被 HTTP_DETAIL 的通用话覆盖。

    **凡是带具体中文原因的错都用这个类抛**，不管状态码在不在映射表里。表是给
    框架层兜底用的（路由不存在、方法不对）；用它挡自己的错，等于把「结果包还在
    生成中」交给下一个人一不小心就变成「请求冲突」。
    admin/app_admin.py 的 PanelError 是同一个角色的另一份实现，两边都靠
    `user_detail(exc, 自己的类)` 保住具体原因。
    """


class RpcCmdIn(BaseModel):
    """管理员面板 -> 内部通道。与 admin/app_admin.py 的 RpcIn 同形，两边独立定义。"""

    command: str = Field(min_length=1, max_length=32)
    message: str = Field(default="", max_length=4000)
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=64)
    thinking_level: str = Field(default="", max_length=64)


class FinishIn(BaseModel):
    summary: str = Field(default="", max_length=4000)
    error: str = Field(default="", max_length=4000)
    # agent 自报的产物清单。**注意它和产物发现不是一回事**：这里说的是工作目录下的
    # 相对路径，而 artifacts.discover 扫的是 ProtForge 的 outputs 根，两棵不同的树。
    # 所以这份清单只记进时间线供排查，不参与打包——真按它去 assemble 会打空包。
    # 但字段必须收下：pydantic 会把未声明的键**静默丢掉**，agent 以为自己报了、
    # 后端却当没收到，这种不一致最难查。
    artifacts: list[str] = Field(default_factory=list, max_length=50)


class ColumnIn(BaseModel):
    """一列的映射。col_index 必带：它是用户表格里的列号（0 基）。

    header / slot / field 都不做格式强校验，真正的判定在
    ``workflow.validate_binding``（未绑定的 {{变量}}、标识列唯一、
    输出列字段名受限都在那里报错）——放在一处判，别在两个地方各判一半。
    """

    col_index: int
    header: str = ""
    role: str = "ignore"
    slot: str = ""
    field: str = ""


class WorkflowIn(BaseModel):
    """配一个已上传的工作流：写模板 + **整体替换**列映射。

    **收 writeback**（规格 2 决策表 / 7.1 / 9.1）：回写是可开关的，用户在
    批量场景下需要能关掉它（表里有敏感序列时，回写会整表复制到磁盘且可被
    下载）。默认 True —— 与「v1 之前的行为」一致，配了结果列就回写，
    不传这个键的老客户端不会因为升级就突然丢掉结果表。

    多传的键由 pydantic 静默丢掉（与 TaskPatch 同约定），但**丢掉的键绝不许
    被转手写进别的行任务**——`update_task(id, workflow_id=别人的)` 能把一行
    搬进别人的工作流，所以任何端点都不从请求体透传 workflow_id。
    """

    workflow_id: int
    name: str = ""
    prompt_template: str
    columns: list[ColumnIn] = Field(default_factory=list)
    writeback: bool = True


class SkipIn(BaseModel):
    reason: str = Field(default="", max_length=400)


# ---------------------------------------------------------------- 内部令牌

# **令牌表只有一张，在 dispatcher 里。** 签发方是调度器（它起进程时把令牌交给
# agent），校验方是下面这个文件里的内部端点，两边必须读同一张表：本文件原先自己
# 抄了一份，两个进程各留各的，症状是 agent 每一步回调都 401 且极难看出原因。
#
# 顺带一个约束：令牌是进程内存，所以调度器必须与内部 app(8475) 同进程，
# 签发走 /api/internal/admin/tokens/{task_id}，别写进数据库——进程重启即失效
# 反而是对的，这把钥匙不该比它守的门活得久。
AGENT_TOKEN_TTL = dispatcher.AGENT_TOKEN_TTL

issue_agent_token = dispatcher.issue_agent_token
revoke_agent_token = dispatcher.revoke_agent_token
agent_token_task = dispatcher.agent_token_task


def require_agent(task_id: int, request: Request) -> int:
    """FastAPI 依赖：校验 X-Agent-Token 与路径里的 task_id 是否同一个任务。

    对不上返 403 而不是 401：令牌本身是真的，只是用错了地方。

    这里（以及 login 的 401）用普通 HTTPException 而不是 PreciseHTTPException 是
    有意的：401/403 不在 HTTP_DETAIL 里，原样透出，detail 保住。**哪天有人给
    401 补了表条目，这两处要一起换成 PreciseHTTPException**。
    """
    raw = (request.headers.get("X-Agent-Token") or "").strip()
    if not raw:
        raise HTTPException(401, "缺少 agent_token 请求头")
    bound = agent_token_task(raw)
    if bound is None:
        raise HTTPException(401, "agent_token 无效或已过期")
    if bound != int(task_id):
        raise HTTPException(403, f"这个 agent_token 只能用于任务 #{bound}")
    return int(task_id)


# ---------------------------------------------------------------- 用户 API

def create_public_app() -> FastAPI:
    db.init_db()
    app = FastAPI(title="蛋白计算平台", version=VERSION)
    _install_common(app)

    @app.post("/api/auth/login")
    def login(body: LoginIn):
        user = auth.authenticate(body.username, body.password)
        if user is None:
            left = auth.locked_out(body.username)
            if left:
                raise HTTPException(401, f"登录失败次数过多，请 {int(left // 60) + 1} 分钟后再试")
            # 用户不存在与口令不对同一条话：分开说等于可以拿它枚举用户名
            raise HTTPException(401, "用户名或密码不对")
        token = auth.issue_token(user["id"])
        return {"token": token, "expires_at": auth.token_expires_at(token),
                "user": user}

    @app.get("/api/me")
    def me(user=Depends(auth.current_user)):
        return user

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskIn, user=Depends(auth.current_user)):
        task = store.create_task(body.title, body.brief, owner_id=user["id"],
                                 actor="human")
        # store.create_task 刻意不写 created 事件（事件流由 API 层负责），
        # 所以这一条得在这儿补，否则时间线是从"已排队"凭空开始的。
        store.add_event(task["id"], "created", "human", f"提交了任务：{task['title']}")
        sse.notify()
        return _detail(task)

    @app.get("/api/tasks")
    def list_tasks(status: str | None = Query(default=None),
                   scope: Literal["regular", "workflow", "all"] = "regular",
                   limit: int = Query(default=100, ge=1, le=500),
                   user=Depends(auth.current_user)):
        is_admin = user["role"] == "admin"
        rows = store.list_tasks(None if is_admin else user["id"], status,
                                is_admin=is_admin, limit=limit)
        # scope 只管「行任务算不算进来」，**不管归属**——归属由上面的
        # owner_id 参数负责，两个维度是正交的。scope 本身是用户可控的查询维度，
        # 白名单收窄成这三个值（Literal）：不校验的话一个随手编的值就会落进
        # 「不过滤」的分支，把不该在这一屏里出现的行任务放出来。
        if scope == "regular":
            rows = [r for r in rows if r.get("workflow_id") is None]
        elif scope == "workflow":
            rows = [r for r in rows if r.get("workflow_id") is not None]
        return {"tasks": [_task_view(t) for t in rows]}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int, user=Depends(auth.current_user)):
        # 越权走 store.get_task → NotFound → 404，不返 403（不承认 id 存在）
        return _detail(store.get_task(task_id, owner_id=user["id"],
                                       is_admin=user["role"] == "admin"))

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: int, body: TaskPatch, user=Depends(auth.current_user)):
        task = store.get_task(task_id, owner_id=user["id"],
                              is_admin=user["role"] == "admin")
        fields, changed = {}, []
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise store.Invalid("标题不能为空")
            fields["title"] = title
        if body.brief is not None:
            if len(body.brief) > store.BRIEF_MAX:
                raise store.Invalid(f"需求描述过长，最多 {store.BRIEF_MAX} 个字符")
            fields["brief"] = body.brief
        if not fields:
            raise store.Invalid("没有要修改的字段")
        for k, v in fields.items():
            if task.get(k) != v:
                changed.append(k)
        if not changed:
            # 内容没变就不写事件：时间线是给人看的，不该被无意义的保存灌满
            raise store.Invalid("内容没有变化，不用保存")
        updated = store.update_task(task_id, **fields)
        # message 里放**原样的英文字段名**：这是 API 契约的一部分，翻译留给
        # 前端显示层（FIELD_CN 翻成「改了标题 / 需求」），服务端不写死中文结构名。
        store.add_event(task_id, "edited", "human", "修改了：" + "、".join(changed))
        sse.notify()
        return _detail(updated)

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: int, user=Depends(auth.current_user)):
        task = store.get_task(task_id, owner_id=user["id"],
                              is_admin=user["role"] == "admin")     # 越权 → 404
        if task.get("workflow_id") is not None:
            # 删掉行任务后归档再也扫不到它 → 那一行的结果静默丢失，而闸门②
            # 因为该行不见了反而满足，整批继续往下跑（规格 6.5 洞三）。
            raise PreciseHTTPException(
                409,
                "这是工作流行任务，删掉它会让那一行的结果无法归档。"
                "要跳过这一行请用「跳过这一行」，要清掉整批请删除整个工作流。")
        _delete_task_row(task_id)
        # 任务行已经没了，这条"删除"事件无处可写，也不假装记了日志
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/abort")
    def abort(task_id: int, user=Depends(auth.current_user)):
        task = store.get_task(task_id, owner_id=user["id"],
                              is_admin=user["role"] == "admin")
        if task["status"] in db.TERMINAL_STATUSES:
            raise PreciseHTTPException(
                422, f"任务{db.STATUS_LABEL.get(task['status'])}了，不能再中止")
        updated = store.set_status(task_id, "failed", actor="human",
                                   message="用户中止了任务")
        # error 字段也补上：详情页读它展示失败原因，而调度器看到库里已经是 failed
        # 之后收尾不会再覆盖（_mark_failed 遇到 failed 直接返回），不写就空了。
        updated = store.update_task(task_id, error="用户中止了任务")
        sse.notify()
        return _detail(updated)

    @app.get("/api/tasks/{task_id}/stream")
    async def stream_task(task_id: int, request: Request,
                          user=Depends(auth.current_user)):
        """任务时间线的 SSE 实时流。

        **鉴权先做、流后开**：越权走 store.get_task → NotFound → 404（不返 403，
        403 等于承认这个 id 存在）。前端 openDrawer 会在详情页里拉一次，这里
        同样拦住，省得靠"连上了没数据"来暴露别人的任务。

        游标优先取 `Last-Event-ID` 头（前端重连时带），退化到 `last_event_id`
        查询参数（curl 手测方便）。两者都缺/是脏数据就是 0 = 从头补。

        处理器是 `async def`：响应体是一条长连接，必须挂在事件循环上；
        鉴权依赖 current_user 是同步函数，由 FastAPI 自己丢线程池，不影响。
        """
        store.get_task(task_id, owner_id=user["id"],
                       is_admin=user["role"] == "admin")
        raw = request.headers.get("Last-Event-ID")
        if raw is None:
            raw = request.query_params.get("last_event_id")
        try:
            last_seen = max(0, int(raw)) if raw not in (None, "") else 0
        except (TypeError, ValueError):
            # 游标是脏数据就从头补：宁可重复（前端按事件 id 去重）也不丢
            last_seen = 0
        return StreamingResponse(
            sse.stream(task_id, request, last_seen),
            media_type="text/event-stream",
            # X-Accel-Buffering: no —— 反代（nginx）默认会缓冲响应体，
            # 不显式关掉，实时流会被攒成一块再吐给浏览器，退化成轮询。
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    @app.get("/api/tasks/{task_id}/package")
    def download(task_id: int, user=Depends(auth.current_user)):
        task = store.get_task(task_id, owner_id=user["id"],
                              is_admin=user["role"] == "admin")
        if task["status"] not in db.TERMINAL_STATUSES:
            raise PreciseHTTPException(409, "结果包还在生成中，任务完成后才能下载")
        raw = (task.get("package_path") or "").strip()
        if not raw:
            raise PreciseHTTPException(409, "这个任务还没有可下载的结果包")
        try:
            # **不能是 guards.resolve()**：生产打包写在 data\packages\task_<id>.zip，
            # 而 resolve() 认的是 agent 的四个区域（work/uploads/outputs/inputs），
            # packages 不在里面——于是每个真实任务点下载都恒 409，包生成了、用户
            # 永远拿不到，测试全绿是因为夹具写的是 work 区里一个生产上不存在的布局。
            # resolve_package() 只认本任务自己的那一个文件，顺带把 inputs/outputs/
            # work 里的任意文件也堵了（原来 write=False 是放行它们的）。
            real = guards.resolve_package(task_id, raw)
        except guards.Violation:
            # 越界（package_path 被写坏或指向别处）一律当"没有包"，
            # 不把文件系统布局透出去。
            raise PreciseHTTPException(409, "结果包不在允许的目录里，无法下载")
        if not real.is_file():
            raise PreciseHTTPException(409, "结果包文件不见了")
        return FileResponse(real, filename=real.name, media_type="application/zip")

    # ---------------------------------------------------------------- 工作流
    #
    # 归属校验在这一段里**统一走 store.get_workflow(owner_id=...)**，越权一律 404
    # 不 403（403 等于承认这个 id 存在，见 app.py:387 的同款理由）。
    #
    # 管理员对别人的工作流**只读**：列表 / 详情 / 结果表下载看得见（值班台需要），
    # 但 start / pause / resume / abort / skip / delete 一律 404——一次误点就停了
    # 别人几十分钟 GPU 的批次。控制类端点因此**一律不传 is_admin**，这是刻意的。

    @app.post("/api/workflows/upload")
    async def upload_workflow(file: UploadFile = File(...),
                              user=Depends(auth.current_user)):
        data = await file.read()
        return save_upload(file.filename or "", data, user["id"])

    @app.post("/api/workflows")
    def save_workflow(body: WorkflowIn, user=Depends(auth.current_user)):
        """上传端点已经建好了记录，这里只负责**配列映射与模板**。

        越权先判（404），再强校验，再落库——顺序反了会把「别人的工作流」
        这个信息通过 400/409 的差异泄露出去。

        **列是整体替换**（store.set_workflow_columns 先 DELETE 再 INSERT）：
        用户把一列解绑再保存，旧列行必须消失，否则它继续参与渲染。
        """
        import workflow as wfmod
        wf = store.get_workflow(body.workflow_id, owner_id=user["id"])
        cols = [c.model_dump() for c in body.columns]
        wfmod.validate_binding(body.prompt_template, cols)     # 服务端强校验
        fields = {"prompt_template": body.prompt_template,
                  # 回写开关：写进库，归档路径真的读它（规格 7.1 / 9.1）
                  "writeback": 1 if body.writeback else 0}
        if (body.name or "").strip():
            fields["name"] = body.name.strip()
        store.update_workflow(body.workflow_id, **fields)
        store.set_workflow_columns(body.workflow_id, cols)
        return {"workflow": _wf_view(
            store.get_workflow(int(wf["id"]), owner_id=user["id"]))}

    @app.get("/api/workflows")
    def list_workflows(user=Depends(auth.current_user)):
        is_admin = user["role"] == "admin"
        wfs = store.list_workflows(
            None if is_admin else user["id"], is_admin=is_admin)
        # 卡片要显示失败数与「当前在跑第几行」（规格 8.2 列表那条 / 7.4
        # 「失败行数放到最显眼处」）。前端不编这个数：列表端点原来只有
        # SELECT * FROM workflows，而失败数不在 workflows 表里——所以它只能
        # 由行任务算出来。算的地方在这一处，**一次 GROUP BY 覆盖全部卡片**。
        stats = _row_stats([w["id"] for w in wfs])
        return {"workflows": [_wf_view(w, stats.get(int(w["id"])))
                              for w in wfs]}

    @app.get("/api/workflows/{workflow_id}")
    def get_workflow(workflow_id: int, user=Depends(auth.current_user)):
        is_admin = user["role"] == "admin"
        wf = store.get_workflow(workflow_id, owner_id=user["id"],
                                is_admin=is_admin)
        pkgs = _row_packages(workflow_id)
        return {"workflow": _wf_view(wf),
                "columns": store.list_workflow_columns(workflow_id),
                "skips": sorted(store.list_workflow_skips(workflow_id)),
                "rows": store.list_workflow_rows(workflow_id),
                # **产物包件数与总大小**：前端据此显示「N 个 · X MB」并决定
                # 要不要给下载入口。少了它，用户只能先点进去才知道有没有东西可下。
                "packages_count": len(pkgs),
                "packages_bytes": sum(p.stat().st_size for _, p in pkgs)}

    @app.get("/api/workflows/{workflow_id}/preview")
    def preview_workflow(workflow_id: int,
                         row: int = Query(default=0, ge=0),
                         user=Depends(auth.current_user)):
        """预览第 row 个数据行渲染出来的提示词。

        **管理员可以预览**：§9.3 的「只读」说的是不给 start/pause/resume/abort/
        skip/delete，不是「看不见」。预览是纯读操作，不泄露任何写能力，所以跟
        列表/详情/结果表同属值班台可见项。"""
        import workflow as wfmod
        wf = store.get_workflow(workflow_id, owner_id=user["id"],
                                is_admin=user["role"] == "admin")
        source = Path(wf["source_path"])
        view = wfmod.read_source(source, header_row=int(wf["header_row"]))
        values = wfmod.row_values(source, view, int(row))
        slots = {}
        for col in store.list_workflow_columns(workflow_id):
            if col["role"] in ("input", "context") and col.get("slot"):
                slots[col["slot"]] = values.get(int(col["col_index"]), "")
        return {"row": row,
                "rendered": wfmod.render(wf["prompt_template"], slots),
                "values": {wf["sheet"]: values}}

    @app.post("/api/workflows/{workflow_id}/{action}")
    def control_workflow(workflow_id: int, action: str,
                         user=Depends(auth.current_user)):
        """action ∈ WORKFLOW_ACTIONS。越权一律 404（规格 9.2）。

        **白名单在归属校验之前**：非法 action 不是别人的资源，判它不会泄露任何
        东西；而顺序反过来（先 get_workflow 再判 action）会让「不存在的工作流 +
        非法 action」与「存在的工作流 + 非法 action」给出不同响应。
        """
        if action not in WORKFLOW_ACTIONS:
            raise PreciseHTTPException(422, "没有这个操作")
        wf = store.get_workflow(workflow_id, owner_id=user["id"])   # 管理员也按 owner
        if action in ("start", "resume"):
            if wf["prompt_template"] == PROMPT_PLACEHOLDER:
                raise PreciseHTTPException(
                    422, "还没配提示词模板，先把模板和列映射填好再开始。")
            import workflow as wfmod
            wfmod.validate_binding(wf["prompt_template"],
                                   store.list_workflow_columns(workflow_id))
            # abort_requested 必须一起清：中止之后 status 回到 paused，不清它的话
            # 调度器那一行 `or int(wf.get("abort_requested") or 0)` 永远不放行，
            # 用户看到的现象是「点了开始，状态是运行中，但一行都不动」。
            wf = store.update_workflow(workflow_id, status="running",
                                       abort_requested=0)
        elif action == "pause":
            wf = store.update_workflow(workflow_id, status="paused")
        else:
            wf = store.update_workflow(workflow_id, status="paused",
                                       abort_requested=1)
            _abort_live_rows(workflow_id)
        sse.notify()
        return {"workflow": _wf_view(wf)}

    @app.post("/api/workflows/{workflow_id}/rows/{row_index}/skip")
    def skip_row(workflow_id: int, row_index: int,
                 body: SkipIn, user=Depends(auth.current_user)):
        """跳过 = 写 workflow_skips + 把该行置终态 + 归档写「已跳过」。

        **先写 workflow_skips，再处置行任务**，顺序不能反：放行侧
        （dispatcher._workflow_admit_one）在按 cursor 建任务前会读这张表，
        「跳过先于放行」是常态——表里没有这一行的话，那一行照跑不误，
        白烧一次完整计算，而用户点它的用意正是「这行序列是坏的，别跑」。

        置终态也是必需的：不置的话闸门② 会一直等这一行，整批卡住。
        还没有行任务的那些行，调度器放行到它时会补一条**建出来就是终态**的
        任务（同样不会被跑），由归档 pass 写「已跳过」。两种顺序都收敛。
        """
        wf = store.get_workflow(workflow_id, owner_id=user["id"])
        store.skip_workflow_row(workflow_id, row_index, body.reason)
        for row in store.list_workflow_rows(workflow_id, row_index):
            if row["status"] not in db.TERMINAL_STATUSES:
                # actor 只能是 human/agent/admin/system（events 的 CHECK）。
                # 这里是人点的，用 human；行任务本身由调度器以 system 建（规格 8）。
                store.set_status(row["id"], "failed", actor="human",
                                 message=f"用户跳过：{body.reason or '未说明'}")
        sse.notify()
        return {"workflow": _wf_view(wf),
                "skips": sorted(store.list_workflow_skips(workflow_id))}

    @app.get("/api/workflows/{workflow_id}/result.xlsx")
    def download_result(workflow_id: int, user=Depends(auth.current_user)):
        wf = store.get_workflow(workflow_id, owner_id=user["id"],
                                is_admin=user["role"] == "admin")
        # 判据两条，都是「有没有理由生成结果表」，**不是** status：
        # ① 配了 output 列吗？没有的话调度器压根不生成 result.xlsx；
        # ② 回写开关开着吗？规格 9.1 明写「开关打开时才有」——关掉之后结果
        #    只存在平台内，磁盘上不会有、也不该由这个端点凭空造一份出来。
        if not _has_output_columns(workflow_id):
            raise PreciseHTTPException(404, "这个工作流没有配结果列，不生成结果表")
        if not _writeback_enabled(wf):
            raise PreciseHTTPException(404, "这个工作流关掉了结果回写，不生成结果表")
        # **必须先判空串再取 Path**：`Path("")` 是 "."，`.exists()` 对它为真，
        # 于是「没配结果表」会走进 FileResponse 变成 500（dispatcher.py:1561
        # 记着同一个坑）。用 is_file() 而不是 exists()：目录也得挡掉。
        raw = (wf.get("result_path") or "").strip()
        path = Path(raw) if raw else None
        if path is None or not path.is_file():
            raise PreciseHTTPException(404, "结果表还没生成")
        return FileResponse(path, filename=path.name,
                            media_type=("application/vnd.openxmlformats-"
                                        "officedocument.spreadsheetml.sheet"))

    @app.get("/api/workflows/{workflow_id}/packages")
    def download_workflow_packages(workflow_id: int,
                                    user=Depends(auth.current_user)):
        """把这一批所有行的**产物包**打成一个 zip 下载（每行一个子文件）。

        为什么要有这个：结果表（result.xlsx）只是**状态与摘要**，真正的交付物
        在每行各自的产物包里（结构、序列、脚本、报告）。一批 61 行就是一个
        一个下——其中一行就 90 MB，下到什么时候是个问题；而且逐个下很容易
        漏掉几行，漏掉的那些行在表上看着像「跑过了」。

        **压缩方式用 ZIP_STORED**：里面装的本来就是 zip，再压一遍只烧 CPU
        （几百 MB 的文件反复 deflate）而几乎不省空间——实测省不到 3%。
        """
        wf = store.get_workflow(workflow_id, owner_id=user["id"],
                                is_admin=user["role"] == "admin")
        items = _row_packages(workflow_id)
        if not items:
            raise PreciseHTTPException(404, "这一批还没有可下载的产物包")
        total = sum(p.stat().st_size for _, p in items)

        tmpdir = Path(tempfile.mkdtemp(prefix="wf-pkgs-"))
        free = shutil.disk_usage(tmpdir).free
        if total > free:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise PreciseHTTPException(
                507, f"临时磁盘装不下这一批的产物包（需要 {total // 1048576} MB，"
                     f"只剩 {free // 1048576} MB）。可以先只下几行的，或把临时目录挪到数据盘。")
        out_zip = tmpdir / f"{_zip_entry_name(wf['name'])}-产物包.zip"
        # 每行的文件名：第 N 行 + 源表里的关键列值（编号），免得下载完
        # 一堆「第1行.zip」「第2行.zip」还得自己对着表认。
        import workflow as wfmod
        source = Path(wf["source_path"])
        view = wfmod.read_source(source, header_row=int(wf["header_row"]))
        key_col = int(wf["key_col"] or 0)
        # 文件名里的标识信息：关键列的值 + **这一行各 slot 的值**。
        # slot 就是用户在表里填的那几列（种属、靶标名那些）——只给「第3行_17」
        # 的话，61 个包下完用户还得自己对着表认；带上 slot 值就能一眼看出
        # 哪个包是哪个靶标。
        slot_cols = sorted(
            (c for c in store.list_workflow_columns(workflow_id)
             if c["role"] in ("input", "context") and c.get("slot")),
            key=lambda c: int(c["col_index"]))
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_STORED,
                             allowZip64=True) as zf:
            for row, path in items:
                idx = int(row.get("workflow_row") or 0)
                values: dict = {}
                try:
                    values = wfmod.row_values(source, view, idx)
                except Exception:       # noqa: BLE001 - 读不到就只用行号
                    values = {}
                key = str(values.get(key_col, "") or "").strip()
                name = f"第{idx + 1}行"
                if key:
                    name += "_" + _zip_entry_name(key)
                for c in slot_cols:
                    v = str(values.get(int(c["col_index"]), "") or "").strip()
                    if v:
                        name += "_" + _zip_entry_name(v)
                zf.write(path, arcname=name[:80] + ".zip")
        # 传完就删：临时目录留着就是垃圾（几十 GB 的包放在那里没人会知道）。
        return FileResponse(out_zip, filename=out_zip.name,
                            media_type="application/zip",
                            background=BackgroundTask(shutil.rmtree, tmpdir, True))

    @app.delete("/api/workflows/{workflow_id}")
    def delete_workflow(workflow_id: int, user=Depends(auth.current_user)):
        """先中止在跑的行任务，再删记录（规格 6.7）。

        **归属校验必须在 _abort_live_rows 之前**：反过来，越权请求会先把别人
        在跑的计算杀掉、再返回 404——凭空造出一条「B 的工作流刚被中止过」的
        时间线，而 B 什么都没做。删掉记录后 tasks.workflow_id 级联带走行任务，
        `_db_status` 返回 ""、`_abort_signal` 因 "" not in TERMINAL_STATUSES 直接
        返回 None，调度器不会去杀它——所以顺序反了还会留下一个烧到自然结束的僵尸。
        """
        wf = store.get_workflow(workflow_id, owner_id=user["id"])    # 越权 → 404
        _abort_live_rows(workflow_id)
        _settle_abort_signal(dispatcher.POLL_INTERVAL * 3)
        # **行任务是被外键级联删掉的**（tasks.workflow_id ON DELETE CASCADE），
        # 不走 _delete_task_row —— 所以那些行的反思正文得在这里清：
        # 级联只管数据库，管不到文件，而正文留在盘上就成了没人认领的残留
        # （实测：删一个跑过的工作流，六行的 task-N.md 全留在 reflections/ 里）。
        for row in store.list_workflow_rows(workflow_id):
            _purge_reflection_files(int(row["id"]))
        # 归属参数必须传：store 层默认拒绝（owner_id=None → NotFound）。
        # 刻意**不传** is_admin——管理员对别人的工作流只读，删不了（§9.3）。
        # 上面那行 get_workflow 不能省：它挡在 _abort_live_rows 之前，store 层
        # 的闸门在删除那一刻才生效，那时别人的计算已经被杀过了。
        store.delete_workflow(workflow_id, owner_id=user["id"])
        # 盘上的源表与结果表一起清：只删数据库的话它们会永远留在
        # data/workflows/<id>/，用户以为删干净了，实际上那些表还在。
        _purge_workflow_files(wf)
        sse.notify()
        return {"ok": True}

    # 用户面板前端挂在**最后**，且只放行这三个文件。
    #
    # **不要**改成 app.mount("/", StaticFiles(...))：那会把整棵树接管，POST /api/...
    # 从 404 变成 405、detail 也不再是中文，而"用户侧没有 /advance"这条设计就被
    # 悄悄改掉了——症状是三条不相干的测试同时变红，没人会想到是挂载顺序。
    #
    # 前端用相对路径引用（href="style.css"），所以单段路径就够；/api/* 至少两段，
    # 撞不上。未知单段路径落到下面 raise 的 404，仍是统一的中文提示。
    if not (WEB_DIR / "index.html").is_file():
        log.warning("用户面板前端不在 %s，8473 首页会是 404", WEB_DIR)

    @app.get("/")
    def user_ui():
        if not (WEB_DIR / "index.html").is_file():
            raise PreciseHTTPException(404, "用户面板前端文件缺失")
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.get("/favicon.ico")
    def user_favicon():
        # 前端用的是 data: 空图标，但浏览器仍会来要一次。给它 204 而不是 404，
        # 省得在日志里堆无意义的报错。
        from fastapi import Response
        return Response(status_code=204)

    @app.get("/{asset}")
    def user_asset(asset: str):
        if asset not in USER_UI_FILES:
            raise PreciseHTTPException(404, "页面不存在")
        f = WEB_DIR / asset
        if not f.is_file():
            raise PreciseHTTPException(404, "页面不存在")
        # 静态资源不该被中途改掉：前端文件没有版本号，缓存一旧就白屏
        return FileResponse(f, headers={"Cache-Control": "no-cache"})

    return app


# ---------------------------------------------------------------- 后台工种
#
# 这三样都是"模块写好了但没人调"的那一类，症状一律是静默的：任务停在 packaging
# 没人打包、E 盘只增不减、没人能登录。所以它们在这里被真正接上，而不是留在文档里。

PACK_SCAN_INTERVAL = 2.0        # 秒。packaging 是给人看的状态，不该让人等太久
JANITOR_INTERVAL = 30 * 60      # 半小时。清理是体力活，不需要勤

# 正在打包的 task_id。打包在子进程里跑、要占磁盘，同一个任务不能被扫两遍。
_pack_lock = threading.Lock()
_packing: set[int] = set()


def _task_visible(task_id: int) -> dict | None:
    """读任务并叫醒 SSE；读不到返回 None。"""
    try:
        task = store.get_task(task_id, is_admin=True)
    except Exception:                                  # noqa: BLE001
        log.exception("读任务 %s 失败", task_id)
        return None
    sse.notify()
    return task


def _fail_task(task_id: int, reason: str) -> None:
    """把任务标成失败并说明原因。已经是终态的**不覆盖**。

    不覆盖的原因：打包失败和调度器收尾可能撞在一起（比如管理员同时点了中止），
    谁后到谁覆盖的话，用户点的是中止，最后看到的却是"打包失败"——
    和 dispatcher._mark_failed 里那道闸门是同一个道理。
    """
    try:
        task = store.get_task(task_id, is_admin=True)
    except Exception:                                  # noqa: BLE001
        log.exception("读任务 %s 失败，跳过标记", task_id)
        return
    if task.get("status") in db.TERMINAL_STATUSES:
        return
    try:
        store.update_task(task_id, error=reason)
        store.set_status(task_id, "failed", actor="system", message=reason)
        store.add_event(task_id, "error", "system", reason)
    except Exception:                                  # noqa: BLE001
        log.exception("把任务 %s 标记为失败时出错", task_id)
    sse.notify()


def _package_one(task_id: int) -> None:
    """打一个包：先登记产物，再压 zip，最后写 package_path 并收口。

    产物靠时间窗兜底发现时 verified=0（"先显示、再确认"），这种情况**不自动放行**：
    自动确认等于把"先显示、再确认"这道闸门拆了。它诚实地失败，由管理员确认后
    走 /api/internal/admin/repack 重新打包。
    """
    try:
        found = artifacts.discover(task_id)
    except Exception as e:                             # noqa: BLE001
        log.exception("登记产物失败：%s", task_id)
        _fail_task(task_id, f"登记产物时出错：{e}")
        return

    if not artifacts.downloadable(task_id):
        if found:
            _fail_task(task_id, "产物已找到但尚未确认打包："
                              "需要管理员在产物列表里确认后重新打包")
        else:
            _fail_task(task_id, "计算结束了，但没有找到任何产物")
        return

    out = artifacts.default_package_path(task_id)
    try:
        artifacts.package(task_id, out_zip=out)
    except artifacts.PackageError as e:                # QuotaExceeded 是它的子类
        log.warning("打包 %s 失败：%s", task_id, e)
        _fail_task(task_id, str(e))
        return
    except Exception:                                   # noqa: BLE001
        log.exception("打包 %s 时出错", task_id)
        _fail_task(task_id, "打包过程出错，详见服务端日志")
        return

    # **写 done 之前必须重读一次状态。** 20 GB 的 zip 可能压好几分钟，用户在这期间
    # 点中止（跑在 public app 那个进程里）是完全正常的。不重读就会把 failed 覆盖回
    # done——用户刚点完中止，任务却变成"已完成"，中止被静默撤销。
    # 失败路径已经有一道同款闸（_fail_task），成功路径漏了，两边不对称。
    try:
        current = store.get_task(task_id, is_admin=True)
    except Exception:                                   # noqa: BLE001
        log.exception("读任务 %s 失败，跳过收口", task_id)
        return
    if current.get("status") in db.TERMINAL_STATUSES:
        # 已经被中止/失败盖住了：结果包留着（管理员确认后可以走 repack 重打），
        # 但绝不改成 done。
        log.info("任务 %s 在打包期间已进入终态（%s），不再改写为 done",
                 task_id, current.get("status"))
        sse.notify()
        return

    try:
        store.update_task(task_id, package_path=str(out))
        store.set_status(task_id, "done", actor="system",
                         message="结果包已就绪，可以下载")
    except Exception:                                   # noqa: BLE001
        log.exception("收口任务 %s 失败", task_id)
        return
    sse.notify()


def _pack_scan_once() -> list[int]:
    """把停在 packaging 的任务打包掉。返回处理过的 task_id。"""
    handled: list[int] = []
    try:
        rows = store.list_tasks(status="packaging", is_admin=True, limit=50)
    except Exception:                                   # noqa: BLE001
        log.exception("读取待打包任务失败")
        return handled
    for row in rows:
        task_id = int(row["id"])
        if (row.get("package_path") or "").strip():
            continue                                   # 已经打过包了，别再来一遍
        with _pack_lock:
            if task_id in _packing:
                continue
            _packing.add(task_id)
        try:
            _package_one(task_id)
            handled.append(task_id)
        finally:
            with _pack_lock:
                _packing.discard(task_id)
    return handled


def _pack_loop(stop: threading.Event) -> None:
    while not stop.wait(PACK_SCAN_INTERVAL):
        try:
            _pack_scan_once()
        except Exception:                               # noqa: BLE001 - 线程不能死
            log.exception("打包扫描出错")


def _janitor_loop(stop: threading.Event) -> None:
    while not stop.wait(JANITOR_INTERVAL):
        try:
            janitor.sweep()
        except Exception:                               # noqa: BLE001
            log.exception("磁盘清理出错")


def _start_housekeeping(app: FastAPI) -> list[threading.Thread]:
    """起打包与清理两个后台线程，返回它们（停机时 join）。"""
    threads = []
    for name, target in (("packager", _pack_loop), ("janitor", _janitor_loop)):
        stop = threading.Event()
        app.state.housekeeping_stop = stop

        def run(stop=stop, target=target, name=name):
            target(stop)

        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        threads.append(t)
    return threads


ADMIN_USER_ENV = "PROTFORGE_ADMIN_USER"
ADMIN_PASSWORD_ENV = "PROTFORGE_ADMIN_PASSWORD"


def bootstrap_admin() -> dict | None:
    """按环境变量建初始管理员，返回建出来的用户。没配返回 None。

    **只在环境变量给了用户名+口令时才动手**，不自动生成随机口令往日志里打——
    那种做法迟早会有人把日志贴到工单里。没配也**不是错误**：可能管理员是别人
    建好的，所以由 :func:`_warn_if_no_admin` 去说这件事。
    """
    user = (os.environ.get(ADMIN_USER_ENV) or "").strip()
    pwd = os.environ.get(ADMIN_PASSWORD_ENV) or ""
    if not user or not pwd:
        return None
    if auth.get_user(user) is not None:
        return None                                    # 已经有了，**绝不覆盖**
    try:
        made = auth.create_user(user, pwd, role="admin")
    except Exception:                                  # noqa: BLE001
        log.exception("创建初始管理员 %s 失败", user)
        return None
    log.info("已按环境变量创建初始管理员：%s", user)
    return made


def _warn_if_no_admin() -> None:
    """一个管理员都没有时把话说清楚——否则表现是"登录页谁都登不进去"。"""
    if not _has_any_admin():
        log.warning(
            "库里还没有管理员，用户端登录与管理面板都会用不了。"
            "在 .env 里设 %s / %s 再重启即可创建。", ADMIN_USER_ENV, ADMIN_PASSWORD_ENV)


def _warn_if_dev_login() -> None:
    """临时登录旁路开着的时候**必须喊出来**。

    这个开关等于"这个进程上所有接口都不需要凭证"。开着就喊，喊得很难看，
    这样没人会带着它忘了关。
    """
    raw = (os.environ.get(auth.DEV_LOGIN_ENV) or "").strip()
    if not raw:
        return
    log.warning(
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        "  临时登录旁路已开启（%s=%s）：本进程上的接口**不需要任何凭证**。\n"
        "  只在真机联调时用，测完请把这个环境变量去掉并重启。\n"
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        auth.DEV_LOGIN_ENV, raw)


def _has_any_admin() -> bool:
    try:
        c = db.connect()
        try:
            row = c.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
            return row is not None
        finally:
            c.close()
    except Exception:                                  # noqa: BLE001
        log.exception("检查管理员是否存在时出错")
        return True                                    # 查不了就别拦着，交给登录时报错


# ---------------------------------------------------------------- 内部 API

# 后台工种总开关：调度器 + 打包器 + 清理器，三样一起开或一起关。
#
# **生产必须设为 1**，否则任务提交后永远停在"排队中"，不会有 agent 被拉起来。
# 默认关闭只有一个理由：测试用 TestClient 起内部 app 时，这三样都会对着临时库
# 真去干活（起 node 进程、压 zip），测试既慢又不确定。
WORKERS_ENV = "PROTFORGE_WEB_WORKERS"
_TRUTHY = ("1", "on", "true", "yes")


@contextlib.asynccontextmanager
async def _workers_lifespan(app: FastAPI):
    """内部 app 顺带把三个后台工种带起来——**只有内部 app 带**。

    - **调度器**：agent 的令牌在这里校验、管理员的 RPC 也转发到这里，它跟这两样
      同进程才共享得到那张内存令牌表。public app 里再起一个等于起两份，
      两份会互相抢任务。
    - **打包器 / 清理器**：跟着调度器一起开关。三样都是"没人调用就静默不干活"的
      东西，分成两个旋钮的话，运维只开一个会得到一个能登录、能提交、但永远
      排队的服务——比全关掉更难查。

    全部由 :data:`WORKERS_ENV` 显式打开。
    """
    bootstrap_admin()
    _warn_if_no_admin()

    if os.environ.get(WORKERS_ENV, "").strip().lower() not in _TRUTHY:
        yield
        return

    disp = dispatcher.Dispatcher()
    app.state.dispatcher = disp
    disp.start()
    threads = _start_housekeeping(app)
    try:
        yield
    finally:
        disp.stop()
        for t in threads:
            t.join(timeout=2.0)


def create_internal_app() -> FastAPI:
    db.init_db()
    app = FastAPI(title="蛋白计算平台（内部通道）", version=VERSION,
                  lifespan=_workers_lifespan)
    _install_common(app)

    @app.post("/api/internal/agent/{task_id}/step")
    def agent_step(task_id: int, body: StepIn,
                   _tid: int = Depends(require_agent)):
        store.get_task(task_id)
        step = store.set_step(task_id, body.text)      # 返回值已是截断后的
        store.add_step_event(task_id, "agent", step)    # 与上一条同文则不重复写时间线
        sse.notify()                                    # 无论去重与否都要唤醒：还活着
        return {"step": step}

    @app.post("/api/internal/agent/{task_id}/event")
    def agent_event(task_id: int, body: EventIn,
                    _tid: int = Depends(require_agent)):
        store.get_task(task_id)
        eid = store.add_event(task_id, body.kind, "agent", body.message)
        sse.notify()
        return {"event_id": eid}

    @app.post("/api/internal/agent/{task_id}/audit")
    def agent_audit(task_id: int, body: AuditIn,
                    _tid: int = Depends(require_agent)):
        """收计算进程的命令审计。

        这一条端点是规格 §14「所有 bash 调用全量记入审计日志」**唯一的实现**。
        少了它，`audit_log` 表、管理端审计页签、CSV 导出、30 天保留策略
        和清理器里的审计清扫会全部静默地空转——每一行代码都在，但没有一行
        会真的被调用到。

        刻意**不**调 `sse.notify()`：一次 rfdiffusion 能产生上万条命令，
        逐条唤醒 SSE 会把用户的实时流变成日志瀑布。正是为了避开这一点，
        审计才被从 `events` 里分出来（§9.2）；在这里又把它灌回去等于
        把当初的设计推翻。管理端本来就是 10 秒轮询审计页签。
        """
        store.get_task(task_id)
        rid = store.audit.log(task_id, body.kind, command=body.command,
                              exit_code=body.exit_code, elapsed_ms=body.elapsed_ms,
                              actor="agent")
        return {"audit_id": rid}

    @app.post("/api/internal/agent/{task_id}/finish")
    def agent_finish(task_id: int, body: FinishIn,
                     _tid: int = Depends(require_agent)):
        task = store.get_task(task_id)
        # 闸门看 finished 标记，不只看状态：状态已经不由 finish 决定了，
        # 只拿终态去判会把第二次 finish 放进来。
        if store.has_finished(task_id) or task["status"] in db.TERMINAL_STATUSES:
            raise PreciseHTTPException(422, "任务已经结束，不能重复收口")
        # 给了 error 就判失败：agent 自己报的失败要能落进时间线，
        # 但只有 system/调度器才允许把任务推上 done。
        if body.error:
            updated = store.set_status(task_id, "failed", actor="agent",
                                       message=body.error)
            updated = store.update_task(task_id, error=body.error)
        else:
            # **成功时不改状态。** 算完不等于交付完：还要 _settle 推 packaging、
            # 打包器打 zip，才到 done。原来这里直接置 done，调度器随后又把状态改回
            # packaging，界面上流转轨会**倒退一格**再前进——明明算完了却看着像返工。
            updated = task
        if body.summary:
            updated = store.update_task(task_id, summary=body.summary)
        # finished 事件放最后：调度器拿 has_finished() 区分"重连"与"真跑完"，
        # 它出现即代表本次计算已彻底收口。
        store.record_finished(task_id, actor="agent")
        if body.artifacts:
            names = "、".join(p.strip() for p in body.artifacts[:8] if p.strip())
            more = f"（共 {len(body.artifacts)} 项）" if len(body.artifacts) > 8 else ""
            store.add_event(task_id, "note", "agent",
                            f"计算进程自报产物：{names}{more}")
        sse.notify()
        # 标记落了不等于进程会退。agent 报完收口只是把结果告诉了我们，它自己
        # 还挂在 runRpcMode 上等 stdin——不主动收口，任务就永远停在「计算中」，
        # 打包与下载整条链路都走不到。详见 Dispatcher.wind_down。
        disp = getattr(app.state, "dispatcher", None)
        if disp is not None:
            try:
                disp.wind_down(task_id)
            except Exception:                        # noqa: BLE001 - 收口失败不该让 finish 报错
                log.exception("收口 %s 的计算进程时出错", task_id)
        return _detail(updated)

    # 干预命令白名单。面板是值班台，不该成为往计算进程里塞任意命令的口子——
    # 白名单与 admin/app_admin.py 的 RPC_COMMANDS 保持一致，但**独立**：面板那边
    # 是它自己的边界，这里是内网通道的边界，两道闸不合并。
    _RPC_COMMANDS = ("steer", "follow_up", "abort", "set_model", "set_thinking_level")

    @app.get("/api/internal/admin/stream/{task_id}")
    async def admin_stream(task_id: int, request: Request,
                           user=Depends(auth.require_admin)):
        """把计算进程 stdout 里的事件流转成 SSE，给值班台看。

        游标用的是 agent 那条 stdout 自己的 ``seq``，**不是** ``events.id``：
        那是两套不同的流，游标语义不能互换——换过去要么重连丢事件，要么重复。
        """
        store.get_task(task_id, is_admin=True)
        disp = getattr(app.state, "dispatcher", None)
        if disp is None:
            raise PreciseHTTPException(503, "调度器没有运行，看不到实时流")
        if int(task_id) not in disp.running:
            # 任务没在跑就别开长连接：面板会拿着 409 当成"流断了"然后无限重连。
            raise PreciseHTTPException(409, "这个任务的计算进程不在运行，没有实时流")
        raw_after = (request.headers.get("Last-Event-ID")
                     or request.query_params.get("after") or 0)
        try:
            after = int(raw_after)
        except (TypeError, ValueError):
            after = 0

        def _gen():
            try:
                for obj in disp.stream(int(task_id), after=after):
                    seq = obj.get("seq", 0)
                    data = json.dumps(obj, ensure_ascii=False)
                    yield f"id: {seq}\nevent: agent\ndata: {data}\n\n"
            except Exception:                       # noqa: BLE001
                log.exception("任务 %s 的实时流出错", task_id)
                yield 'event: error\ndata: {"detail": "实时流中断"}\n\n'
            # 明确收尾：面板靠它停止重连，而不是等超时
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/internal/admin/rpc/{task_id}")
    def admin_rpc(task_id: int, body: RpcCmdIn, user=Depends(auth.require_admin)):
        """把一条干预命令送进计算进程（spec §8.1 的 RPC 闭环）。

        这一层做的是**形状翻译**：面板发的是 ``{"command": "steer", "message": ...}``，
        agent 认的是 ``{"type": "steer", "message": ...}``，中间就差这一段。
        """
        command = (body.command or "").strip()
        if command not in _RPC_COMMANDS:
            raise PreciseHTTPException(400, f"不支持的干预命令：{command}")
        store.get_task(task_id, is_admin=True)

        disp = getattr(app.state, "dispatcher", None)
        if disp is None:
            raise PreciseHTTPException(503, "调度器没有运行，无法干预计算进程")

        # abort 直接杀进程树，不透传给 agent。面板上那个按钮的意思是"停掉这个计算"，
        # 不是"让当前这轮回答先说完"——留着进程继续烧 GPU 比多等几秒优雅重要。
        if command == "abort":
            stopped = disp.abort(task_id, reason="管理员中止")
            sse.notify()
            return {"ok": True, "result": {"aborted": bool(stopped)}}

        if command in ("steer", "follow_up"):
            text = (body.message or "").strip()
            if not text:
                raise PreciseHTTPException(400, "干预内容不能为空")
            wire = {"type": command, "message": text}
        elif command == "set_model":
            model = (body.model or "").strip()
            if not model:
                raise PreciseHTTPException(400, "模型名不能为空")
            wire = {"type": "set_model",
                    "provider": (body.provider or "").strip() or dispatcher.LLM_PROVIDER_ID,
                    "modelId": model}
        else:                                                   # set_thinking_level
            level = (body.thinking_level or "").strip()
            if not level:
                raise PreciseHTTPException(400, "思考档位不能为空")
            wire = {"type": "set_thinking_level", "level": level}

        try:
            result = disp.rpc(task_id, wire)
        except dispatcher.AgentGone:
            raise PreciseHTTPException(
                409, "计算进程已经不在了，干预命令没有发出去") from None
        except dispatcher.RpcTimeout:
            raise PreciseHTTPException(504, "计算进程没有在期限内回应") from None
        sse.notify()
        return {"ok": True, "result": result}

    @app.post("/api/internal/admin/repack/{task_id}")
    def repack(task_id: int, user=Depends(auth.require_admin)):
        """把任务推回 packaging，让打包器再打一次。

        存在的理由：产物靠时间窗兜底发现时 verified=0（"先显示、再确认"），
        管理员在产物列表里点完确认之后需要一个重新打包的入口。
        只认 failed 与 packaging——已经是 done 的任务重打会把已有 zip 覆盖掉，
        那种事该由人显式决定，不该由一个通用端点顺手做掉。
        """
        task = store.get_task(task_id, is_admin=True)
        if task["status"] not in ("failed", "packaging"):
            raise PreciseHTTPException(
                409, f"任务现在是{db.STATUS_LABEL.get(task['status'], task['status'])}，"
                     "不需要重新打包")
        store.update_task(task_id, error="")
        updated = store.set_status(task_id, "packaging", actor="admin",
                                   message="已重新排队打包")
        sse.notify()
        return _detail(updated)

    # 内部管理通道的第二套令牌：管理员 Bearer。它只负责发 agent_token，
    # 自己不碰任务状态——状态推进全由系统触发。
    @app.post("/api/internal/admin/tokens/{task_id}")
    def admin_issue_token(task_id: int, user=Depends(auth.require_admin)):
        store.get_task(task_id)
        return {"task_id": task_id, "token": issue_agent_token(task_id)}

    return app


__all__ = [
    "VERSION", "PUBLIC_PORT", "INTERNAL_PORT", "create_public_app",
    "create_internal_app", "validation_message", "require_agent",
    "issue_agent_token", "revoke_agent_token", "agent_token_task", "main",
]


def main(argv: list[str] | None = None) -> int:
    """`python server/app.py public|internal` —— **两个进程，各起一个 uvicorn**。

    故意不做"一个进程两个 app"的写法：内部 app 必须只绑回环地址，
    绑在同一进程里靠参数切换，早晚有人用错参数把内部通道开到 0.0.0.0。
    两条命令分开跑，端口写死在代码里，反代配置照着抄即可：

        python server/app.py public      # 0.0.0.0:8473  反代指向它
        python server/app.py internal    # 127.0.0.1:8475 反代**不要**转发它
    """
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(description="蛋白计算平台主服务")
    p.add_argument("which", choices=("public", "internal"))
    p.add_argument("--port", type=int, default=None)
    args = p.parse_args(argv)

    if args.which == "public":
        app, host, port = create_public_app(), "0.0.0.0", args.port or PUBLIC_PORT
    else:
        app, host, port = create_internal_app(), "127.0.0.1", args.port or INTERNAL_PORT
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
