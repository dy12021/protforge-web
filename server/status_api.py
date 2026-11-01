"""状态面板的后端：平均单个用时、任务分布、系统性能四项。

三档可见性，**按账号角色判，不按面板判**：

===================  ==========================  ==========================
接口                  常规用户                    管理员
===================  ==========================  ==========================
/api/status/overview  只有**自己**的任务统计        全局统计 + 全局吞吐
/api/status/system    **403**（前端也别渲染）      系统性能四项
/api/status/workflow/ 自己名下的工作流              任何工作流
===================  ==========================  ==========================

**为什么系统指标要单独一个端点**，而不是塞进 overview 再按角色删字段：
单独一个端点才能让「普通用户根本拿不到」成为**服务端的事实**，而不是
「前端拿到了一堆字段然后决定不显示」——后者一个 devtools 控制台就能看到。

**零新依赖。** 这个仓库没有 requirements.txt（公开仓库不带本机依赖清单），
所以系统指标走标准库：Windows 上用 ctypes 调 kernel32，GPU 靠 nvidia-smi
（可能不存在，必须能优雅降级），磁盘用 shutil。

**取不到就说取不到。** 每一个指标都允许是 null：成功率没有样本时是 null
（不是 0%），一个任务都没跑完时用时是 null（不是 0 秒），探测失败时是 null
（不是 0%）。两个前端面板都已经认 null（显示「—」）——后端要是拿 0 充数，
那就是在对着没发生过的事报平安，比没有这个数更坏。
"""
from __future__ import annotations

import logging
import os
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

import auth
import db
import store

log = logging.getLogger(__name__)

#: **router 本身不带 prefix**：同一个 router 要挂在两个前缀下——
#: 常规面板用 /api/status/*，管理端用 /api/admin/status/*
#: （管理端前端的 apiBase() 会拼上 /api/admin）。
router = APIRouter(tags=["status"])

#: **用时统计**的取数上限。一次拉太多会把「打开面板」变成一次全表扫描。
#:
#: 只管用时，不管计数：计数走 SQL（见 :func:`_exact_counts`），是精确的。
#: 用时是**样本**——样本量写在 ``duration.count`` 里，看的人自己有数。
STATS_LIMIT = 5000

#: SQL 的 IN 子句一次最多塞多少个参数。
#:
#: 老版本 SQLite 的 SQLITE_MAX_VARIABLE_NUMBER 是 999（3.32 之后才涨到
#: 32766），一次塞满 5000 个 id 会在老库上直接抛「too many SQL variables」。
#: 而那个异常被吞掉之后的表现是「用时统计悄悄退回 created_at 起点」——
#: 不报错，只是数全错了。分块就没有这个坑。
SQL_CHUNK = 500

#: 全局吞吐的统计窗口（小时）。
THROUGHPUT_WINDOW_H = 24.0

#: nvidia-smi 的超时。它卡住的时候，面板不能跟着卡住。
GPU_PROBE_TIMEOUT = 2.5

#: nvidia-smi 的查询。**多卡时每一行一张卡**（--query-gpu 不带 index 就是
#: 每张卡一行）。
GPU_QUERY_CMD = ["nvidia-smi",
                 "--query-gpu=utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"]


# ---------------------------------------------------------------- 取数


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _running_events(task_ids: list[int]) -> dict[int, list[datetime]]:
    """每个任务「开始跑」的时刻，**按先后排好**。

    一次可能不止一条：管理员重跑会把任务从终态推回队列再跑一次，这时
    要取**结束前最后一条**——取第一条会把「上一轮跑完到这一轮重跑」之间
    的空档算进这一轮的用时里。

    IN 子句分块（见 :data:`SQL_CHUNK`）。
    """
    ids = [int(i) for i in task_ids]
    out: dict[int, list[datetime]] = {}
    if not ids:
        return out
    con = db.connect()
    try:
        for start in range(0, len(ids), SQL_CHUNK):
            chunk = ids[start:start + SQL_CHUNK]
            marks = ",".join("?" * len(chunk))
            try:
                rows = con.execute(
                    "SELECT task_id, created_at FROM events "
                    f"WHERE kind='status' AND to_status='running' "
                    f"AND task_id IN ({marks}) ORDER BY id", chunk).fetchall()
            except Exception:                             # noqa: BLE001
                log.exception("读任务开始时间失败")
                rows = []
            for r in rows:
                ts = _parse_ts(r[1])
                if ts is not None:
                    out.setdefault(int(r[0]), []).append(ts)
    finally:
        con.close()
    for stamps in out.values():
        stamps.sort()
    return out


def _pick_start(stamps: list[datetime], end: datetime) -> datetime | None:
    """结束前**最后**一个「开始跑」的时刻。没有早于 end 的就返回 None。"""
    earlier = [s for s in stamps if s <= end]
    return earlier[-1] if earlier else None


def _durations(tasks: list[dict]) -> list[float]:
    """终态任务的真实耗时（秒）。

    起点是**认领那一刻**（状态变成 running 的那条事件），不是 created_at：
    工作流把整批行任务先建出来再串行跑，created_at 是「整批建好的时刻」，
    拿它当起点会把「排队等前面的行」也算进去（实测某批 61 行的表：
    单行中位 8.4 小时，其中大半是在等前面的行）。

    **工作流行任务没有 running 事件就不计入**：它压根没跑过，created_at
    不是它的起点，拿它算出来的数是假的。独立任务没有 running 事件时
    退回 created_at——它是自己提交完就开跑的，排队时间可以忽略。
    """
    terminal = [t for t in tasks if t.get("status") in db.TERMINAL_STATUSES]
    running = _running_events([int(t["id"]) for t in terminal])
    out: list[float] = []
    for t in terminal:
        end = _parse_ts(t.get("done_at"))
        if end is None:
            continue
        begin = _pick_start(running.get(int(t["id"])) or [], end)
        if begin is None:
            if t.get("workflow_id") is not None:
                continue          # 没跑过的工作流行任务：起点不可信，不计入
            begin = _parse_ts(t.get("created_at"))
            if begin is None:
                continue
        secs = (end - begin).total_seconds()
        if secs >= 0:
            out.append(secs)
    return out


def _exact_counts(owner_id: int | None, is_admin: bool) -> tuple[dict, dict, dict]:
    """精确的任务计数：**不受取数上限影响**。

    「任务总数」报少比报不出来更坏：面板上写「任务总数 5000」而库里其实
    有 5001 个，读的人不会怀疑这个数——它看起来就是个真数。所以计数走
    一条 GROUP BY（精确），只有用时统计才走取数上限（那是**样本**，样本量
    写在 ``duration.count`` 里）。

    返回三份：全量计数、独立任务/工作流行任务的拆分、工作流行任务自己的
    计数。与前端要的三个键一一对应。
    """
    sql = ("SELECT status, COUNT(*) AS n,"
           " SUM(CASE WHEN workflow_id IS NULL THEN 1 ELSE 0 END) AS reg,"
           " SUM(CASE WHEN workflow_id IS NOT NULL THEN 1 ELSE 0 END) AS rows_n"
           " FROM tasks")
    args: list = []
    if not is_admin and owner_id is not None:
        sql += " WHERE owner_id = ?"
        args.append(int(owner_id))
    sql += " GROUP BY status"
    con = db.connect()
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()

    counts = {"total": 0, "queued": 0, "running": 0, "packaging": 0,
              "done": 0, "failed": 0}
    row_counts = dict(counts)
    regular = workflow = 0
    for r in rows:
        st = str(r["status"])
        n = int(r["n"])
        counts["total"] += n
        if st in counts:
            counts[st] += n
        reg = int(r["reg"] or 0)
        rn = int(r["rows_n"] or 0)
        regular += reg
        workflow += rn
        row_counts["total"] += rn
        if st in row_counts:
            row_counts[st] += rn
    return counts, {"regular": regular, "workflow": workflow}, row_counts


def _duration_block(values: list[float]) -> dict:
    """用时统计块。**一个样本都没有时给 null，不给 0**。

    0 会被面板显示成「平均耗时 0 秒」「成功率 0%」——那是在对着没发生过
    的事报平安。null 让两个前端都显示「—」，那才是真的。
    """
    if not values:
        return {"count": 0, "avg_seconds": None, "median_seconds": None,
                "max_seconds": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "avg_seconds": round(statistics.fmean(ordered), 1),
        "median_seconds": round(statistics.median(ordered), 1),
        "max_seconds": round(ordered[-1], 1),
    }


def _iso_ms(dt: datetime) -> str:
    """与库里时间戳同格式（毫秒精度）的字符串。

    库里的 created_at / done_at 是 ``db.now_iso()`` 写的，固定到毫秒。
    拿它做**字符串比较**时（SQL 里没有时区概念，只能比字符串），位数
    必须一样：``'...:00.123456'`` 与 ``'...:00.123'`` 比大小是按字符
    来的，多一位就大——边界上会把刚完成的那个任务算到窗口外面去。
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03d" % (dt.microsecond // 1000)


def _throughput_per_hour(owner_id: int | None, is_admin: bool) -> float:
    """最近 24 小时完成的任务数 / 24。只对管理员给。

    走 SQL 精确计数，**不用取数上限那一份样本**：样本只有最近
    STATS_LIMIT 个任务，任务一多就数不全「24 小时内完成了多少」。

    ``cutoff`` 与库里的时间戳必须同为 naive 且同为本地时间——库里的
    时间戳由 ``db.now_iso()`` 写成不带时区的 ISO 串，拿一个带时区的
    cutoff 去比会直接 TypeError（真机 500）。
    """
    cutoff = _iso_ms(datetime.now() - timedelta(hours=THROUGHPUT_WINDOW_H))
    sql = "SELECT COUNT(*) FROM tasks WHERE status = 'done' AND done_at >= ?"
    args: list = [cutoff]
    if not is_admin and owner_id is not None:
        sql += " AND owner_id = ?"
        args.append(int(owner_id))
    con = db.connect()
    try:
        n = int(con.execute(sql, args).fetchone()[0])
    finally:
        con.close()
    return round(n / THROUGHPUT_WINDOW_H, 2)


# ---------------------------------------------------------------- 系统性能


def _no_window() -> dict:
    """Windows 上别为了探测闪一个控制台窗口出来（与 dispatcher 同一做法）。"""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _windows_times() -> tuple[int, int, int]:
    """GetSystemTimes：空闲 / 内核 / 用户，三个都是 100 纳秒计数。"""
    import ctypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    def _v(ft):
        return (ft.high << 32) | ft.low

    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError("GetSystemTimes 失败")
    return _v(idle), _v(kernel), _v(user)


def _windows_memory() -> dict:
    """GlobalMemoryStatusEx：dwMemoryLoad 就是内存占用百分比。"""
    import ctypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    st = _MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        raise OSError("GlobalMemoryStatusEx 失败")
    total = st.ullTotalPhys / (1024 ** 3)
    used = (st.ullTotalPhys - st.ullAvailPhys) / (1024 ** 3)
    return {"percent": round(float(st.dwMemoryLoad), 1),
            "used_gb": round(used, 1), "total_gb": round(total, 1)}


def _cpu_percent() -> float | None:
    """CPU 占用率（%）。**取不到就是 None，不是 0。**

    Windows：GetSystemTimes 取两次，算差值。**必须睡一小会儿**，否则两次
    采样的差值全是 0，报出来是 0%。
    """
    if os.name == "nt":
        try:
            i1, k1, u1 = _windows_times()
            time.sleep(0.25)
            i2, k2, u2 = _windows_times()
        except Exception:                                 # noqa: BLE001
            log.debug("取 CPU 占用率失败", exc_info=True)
            return None
        idle_d = i2 - i1
        total_d = (k2 - k1) + (u2 - u1)
        if total_d <= 0:
            return None
        return round(max(0.0, min(100.0, (total_d - idle_d) * 100.0 / total_d)), 1)
    try:
        load1, load5, load15 = os.getloadavg()
        cores = os.cpu_count() or 1
        return round(min(100.0, load1 / cores * 100.0), 1)
    except (OSError, AttributeError):
        return None


def _memory() -> dict:
    """内存占用。取不到时三个字段都是 None。"""
    if os.name == "nt":
        try:
            return _windows_memory()
        except Exception:                                 # noqa: BLE001
            log.debug("取内存占用失败", exc_info=True)
            return {"percent": None, "used_gb": None, "total_gb": None}
    try:
        info = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = float(rest.strip().split()[0]) / (1024 ** 2)
        total = info.get("MemTotal", 0.0)
        avail = info.get("MemAvailable", info.get("MemFree", 0.0))
        used = total - avail
        return {"percent": round(used * 100 / total, 1) if total else None,
                "used_gb": round(used, 1), "total_gb": round(total, 1)}
    except (OSError, ValueError, IndexError):
        return {"percent": None, "used_gb": None, "total_gb": None}


def _gpu_missing() -> dict:
    """**没有 GPU 是正常状态**，不是错误。

    这里给的是「确定没有」而不是「取不到」：``available`` 为 False，
    其余字段一律 None（没有一个占用率可言）。
    """
    return {"available": False, "util_percent": None, "used_gb": None,
            "total_gb": None, "count": 0}


def _gpu() -> dict:
    """GPU 占用。**没有 GPU 是正常状态**，不是错误。

    多卡时：**利用率取最忙的一张**（平台一张卡跑一个任务，机器忙不忙
    看的是最忙那张），**显存用求和**（used/total 是整台机器的账）。
    """
    try:
        proc = subprocess.run(GPU_QUERY_CMD, capture_output=True, text=True,
                              timeout=GPU_PROBE_TIMEOUT, **_no_window())
    except (OSError, subprocess.SubprocessError):
        # 没装 nvidia-smi / 卡住 / 权限不够：都降级成「无 GPU」，不抛。
        return _gpu_missing()
    if proc.returncode != 0:
        return _gpu_missing()
    utils: list[float] = []
    used_mb = 0.0
    total_mb = 0.0
    for line in (proc.stdout or "").splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 3:
            continue
        try:
            utils.append(float(cols[0]))
            used_mb += float(cols[1])
            total_mb += float(cols[2])
        except ValueError:
            continue
    if not utils:
        return _gpu_missing()
    return {"available": True, "util_percent": round(max(utils)),
            "used_gb": round(used_mb / 1024, 1),
            "total_gb": round(total_mb / 1024, 1),
            "count": len(utils)}


def _disk() -> dict:
    """数据盘剩余空间。**看的是数据根，不是系统盘**——数据盘满了才是真出事。"""
    try:
        root = str(db.data_root())
        usage = shutil.disk_usage(root)
    except OSError:
        return {"used_gb": None, "total_gb": None, "free_gb": None}
    return {"used_gb": round(usage.used / (1024 ** 3), 1),
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1)}


def _cpu_cores() -> int:
    return os.cpu_count() or 1


def _safe(fn, fallback):
    """一次探测坏了不许把整个端点带崩。

    四项是**各自独立**的：GPU 探测不到，不该让 CPU / 内存 / 磁盘也拿不到。
    各个探测函数自己已经吞了异常，这里再兜一层是因为「探测函数自己会
    抛」这件事不该由端点的可用性来承担——今天的探测函数都吞了，但明天
    换一种实现方式时，这一层就是唯一还挡着 500 的东西。
    """
    try:
        return fn()
    except Exception:                                     # noqa: BLE001
        log.debug("系统指标探测失败", exc_info=True)
        return fallback


# ---------------------------------------------------------------- 端点


@router.get("/overview")
def overview(user: dict = Depends(auth.current_user)):
    """总览。**常规用户只看到自己的**；管理员看到全局并多拿一个吞吐。"""
    is_admin = (user or {}).get("role") == "admin"
    owner_id = None if is_admin else int(user["id"])
    # 计数是**精确**的（一条 GROUP BY），不受取数上限影响。
    counts, breakdown, row_counts = _exact_counts(owner_id, is_admin)
    # 用时是**样本**：只取最近 STATS_LIMIT 个任务算，样本量在 duration.count。
    tasks = store.list_tasks(owner_id, is_admin=is_admin, limit=STATS_LIMIT)
    durations = _durations(tasks)
    done = counts["done"]
    failed = counts["failed"]
    out = {
        "scope": "admin" if is_admin else "user",
        "tasks": counts,
        "breakdown": breakdown,
        "workflow_rows": row_counts,
        "duration": _duration_block(durations),
        # 没有终态任务时**没有成功率**（null），不是 0%——0% 是在说
        # 「跑过 0 个、成功 0 个」，而事实是「一个都还没跑完」。
        "success_rate": (round(done / (done + failed), 4)
                         if (done + failed) else None),
    }
    if is_admin:
        # 全局速度：只对管理员给。常规用户拿不到别人任务的吞吐。
        out["throughput_per_hour"] = _throughput_per_hour(owner_id, is_admin)
    return out


@router.get("/system")
def system(_user: dict = Depends(auth.require_admin)):
    """系统性能四项。**只有管理员**——普通用户这里是 403，不是「拿到再隐藏」。

    任何一项探测失败都只让**那一项**变成 None，不许让整个端点 500。
    """
    return {
        "cpu": {"percent": _safe(_cpu_percent, None), "cores": _cpu_cores()},
        "memory": _safe(_memory, {"percent": None, "used_gb": None,
                                  "total_gb": None}),
        "gpu": _safe(_gpu, _gpu_missing()),
        "disk": _safe(_disk, {"used_gb": None, "total_gb": None,
                              "free_gb": None}),
    }


@router.get("/workflow/{workflow_id}")
def workflow_status(workflow_id: int, user: dict = Depends(auth.current_user)):
    """**单个**工作流的状态面板：行数分布、单行用时、预计还要多久。"""
    is_admin = (user or {}).get("role") == "admin"
    try:
        wf = store.get_workflow(int(workflow_id),
                                owner_id=None if is_admin else int(user["id"]),
                                is_admin=is_admin)
    except store.NotFound:
        raise HTTPException(status_code=404, detail="工作流不存在")
    except store.StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    rows = store.list_workflow_rows(int(workflow_id))
    # **跳过的行不算失败**（与 server/app.py 的 _row_stats 同一判据）：
    # 跳过的行在库里是终态 failed（补偿任务建出来就是终态），而同一行在
    # 结果表里写的是「已跳过」。不排除的话就是「表说一套、面板说另一套」：
    # 跳 2 行 → 表里两行「已跳过」，面板标红「失败 2 行」。
    # 判据用 workflow_skips（唯一事实），不是 status——failed 这一个值
    # 同时装着两种完全相反的语义，按 status 分不开。
    skips = store.list_workflow_skips(int(workflow_id))
    counts = {"done": 0, "failed": 0, "running": 0, "queued": 0,
              "packaging": 0, "skipped": 0}
    timed: list[dict] = []
    for r in rows:
        row_no = r.get("workflow_row")
        if row_no is not None and int(row_no) in skips:
            counts["skipped"] += 1
            continue
        st = str(r.get("status") or "")
        if st in counts:
            counts[st] += 1
        if st in db.TERMINAL_STATUSES:
            timed.append(r)
    # 跳过的行**不进用时统计**：补偿任务建出来就是终态，耗时几乎为 0，
    # 混进去会把平均单行用时拉到接近 0，预计剩余时间跟着变成假的小数字。
    durations = _durations(timed)
    block = _duration_block(durations)
    # 前端读的是 **avg_row_seconds**（web/app.js 的工作流卡片）。
    # 两个名字给同一个数：avg_seconds 是通用名，avg_row_seconds 是这一屏
    # 自己的名字，缺了它那一句「每行平均 …」永远不显示。
    block["avg_row_seconds"] = block["avg_seconds"]

    total_rows = int(wf.get("total_rows") or len(rows))
    done_rows = counts["done"]
    remaining = max(0, total_rows - done_rows - counts["failed"] - counts["skipped"])
    median = block["median_seconds"]
    if remaining <= 0:
        eta = 0                      # 跑完了，「还要 0 秒」是真的
    elif median is None:
        eta = None                  # 一行都还没跑完：估不出来，不给假数字
    else:
        eta = round(median * remaining)
    total_secs = sum(durations)
    return {
        "workflow_id": int(wf["id"]),
        "name": wf.get("name") or "",
        "status": wf.get("status") or "",
        "total_rows": total_rows,
        "rows": counts,
        "rows_remaining": remaining,
        "duration": block,
        "eta_seconds": eta,
        "throughput_rows_per_hour": (
            round(len(durations) / (total_secs / 3600.0), 2)
            if durations and total_secs > 0 else None),
    }


__all__ = ["router"]
