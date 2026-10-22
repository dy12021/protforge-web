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
"""
from __future__ import annotations

import logging
import os
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

import auth
import db
import store

log = logging.getLogger(__name__)

#: **router 本身不带 prefix**：同一个 router 要挂在两个前缀下——
#: 常规面板用 /api/status/*，管理端用 /api/admin/status/*
#: （管理端前端的 apiBase() 会拼上 /api/admin）。
router = APIRouter(tags=["status"])

#: 统计用的取数上限。一次拉太多会把「打开面板」变成一次全表扫描。
STATS_LIMIT = 5000

#: 全局吞吐的统计窗口（小时）。
THROUGHPUT_WINDOW_H = 24.0

#: nvidia-smi 的超时。它卡住的时候，面板不能跟着卡住。
GPU_PROBE_TIMEOUT = 2.5


# ---------------------------------------------------------------- 取数


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _started_at_map(task_ids: list[int]) -> dict[int, datetime]:
    """每个任务**真正开始跑**的时刻。

    不能用 ``tasks.created_at``：工作流是先把整批行任务都建出来再串行跑的，
    所以 created_at 是「这一批建好的时刻」，早于它开始跑。
    ``done_at - created_at`` 算出来的是「排队 + 计算」，比真实耗时长得多
    （实测某批 61 行的表：单行中位 8.4 小时，其中大半是在等前面的行）。

    真正的起点是认领那一刻——调度器把任务从 queued 改成 running 时写的
    那条 status 事件。
    """
    if not task_ids:
        return {}
    out: dict[int, datetime] = {}
    con = db.connect()
    try:
        marks = ",".join("?" * len(task_ids))
        for row in con.execute(
                f"SELECT task_id, created_at FROM events "
                f"WHERE kind='status' AND to_status='running' AND task_id IN ({marks}) "
                f"ORDER BY id", task_ids):
            tid = int(row[0])
            ts = _parse_ts(row[1])
            if ts is not None and tid not in out:
                out[tid] = ts
    except Exception:                                     # noqa: BLE001
        log.exception("读任务开始时间失败")
    finally:
        con.close()
    return out


def _durations(tasks: list[dict]) -> list[float]:
    """终态任务的真实耗时（秒）。没有可靠起点的不计入。"""
    started = _started_at_map([int(t["id"]) for t in tasks])
    out: list[float] = []
    for t in tasks:
        if t.get("status") not in db.TERMINAL_STATUSES:
            continue
        end = _parse_ts(t.get("done_at"))
        if end is None:
            continue
        begin = started.get(int(t["id"])) or _parse_ts(t.get("created_at"))
        if begin is None:
            continue
        secs = (end - begin).total_seconds()
        if secs >= 0:
            out.append(secs)
    return out


def _counts(tasks: list[dict]) -> dict:
    counts = {"total": len(tasks), "queued": 0, "running": 0,
              "packaging": 0, "done": 0, "failed": 0}
    for t in tasks:
        st = str(t.get("status") or "")
        if st in counts:
            counts[st] += 1
    return counts


def _split(tasks: list[dict]) -> tuple[list[dict], list[dict]]:
    """把「独立计算任务」和「工作流行任务」分开。

    两者混在一起算一个「任务总数」是没法对账的：看板列表只列独立任务，
    而批量作业的活全在工作流行任务里（一个批次 61 行 = 61 个任务）。
    面板要两个口径都说清楚，别让读者自己猜。
    """
    regular = [t for t in tasks if t.get("workflow_id") is None]
    rows = [t for t in tasks if t.get("workflow_id") is not None]
    return regular, rows


def _duration_block(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "avg_seconds": 0.0, "median_seconds": 0.0,
                "max_seconds": 0.0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "avg_seconds": round(statistics.fmean(ordered), 1),
        "median_seconds": round(statistics.median(ordered), 1),
        "max_seconds": round(ordered[-1], 1),
    }


def _throughput_per_hour(tasks: list[dict]) -> float:
    """最近 24 小时完成的任务数 / 24。只对管理员给。

    **cutoff 必须和库里时间一样是 naive 的。** 库里的时间戳由
    ``db.now_iso()`` 写成不带时区的 ISO 串，解析出来也是 naive；
    拿一个带时区的 cutoff 去比会直接 TypeError（真机 500）。
    """
    cutoff = datetime.now() - timedelta(hours=THROUGHPUT_WINDOW_H)
    n = 0
    for t in tasks:
        end = _parse_ts(t.get("done_at"))
        if end is not None and end >= cutoff and t.get("status") == "done":
            n += 1
    return round(n / THROUGHPUT_WINDOW_H, 2)


# ---------------------------------------------------------------- 系统性能


def _cpu_percent() -> float:
    """CPU 占用率（%）。

    Windows：GetSystemTimes 取两次，算差值。**必须睡一小会儿**，否则两次
    采样的差值全是 0，报出来是 0%。
    """
    if os.name == "nt":
        try:
            import ctypes

            class _FILETIME(ctypes.Structure):
                _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

            def _v(ft):
                return (ft.high << 32) | ft.low

            def _times():
                idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
                if not ctypes.windll.kernel32.GetSystemTimes(
                        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                    raise OSError("GetSystemTimes 失败")
                return _v(idle), _v(kernel), _v(user)

            i1, k1, u1 = _times()
            time.sleep(0.25)
            i2, k2, u2 = _times()
            idle_d = i2 - i1
            total_d = (k2 - k1) + (u2 - u1)
            if total_d <= 0:
                return 0.0
            return round(max(0.0, min(100.0, (total_d - idle_d) * 100.0 / total_d)), 1)
        except Exception:                                 # noqa: BLE001
            log.debug("取 CPU 占用率失败", exc_info=True)
            return 0.0
    try:
        load1, load5, load15 = os.getloadavg()
        cores = os.cpu_count() or 1
        return round(min(100.0, load1 / cores * 100.0), 1)
    except (OSError, AttributeError):
        return 0.0


def _memory() -> dict:
    """内存占用。"""
    if os.name == "nt":
        try:
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
            return {"percent": round(st.dwMemoryLoad, 1),
                    "used_gb": round(used, 1), "total_gb": round(total, 1)}
        except Exception:                                 # noqa: BLE001
            log.debug("取内存占用失败", exc_info=True)
    try:
        info = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = float(rest.strip().split()[0]) / (1024 ** 2)
        total = info.get("MemTotal", 0.0)
        avail = info.get("MemAvailable", info.get("MemFree", 0.0))
        used = total - avail
        return {"percent": round(used * 100 / total, 1) if total else 0.0,
                "used_gb": round(used, 1), "total_gb": round(total, 1)}
    except (OSError, ValueError, IndexError):
        return {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0}


def _gpu() -> dict:
    """GPU 占用。**没有 GPU 是正常状态**，不是错误。"""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=GPU_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "util_percent": 0, "used_gb": 0.0, "total_gb": 0.0}
    line = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not line:
        return {"available": False, "util_percent": 0, "used_gb": 0.0, "total_gb": 0.0}
    try:
        util, used_mb, total_mb = [x.strip() for x in line[0].split(",")]
        return {"available": True, "util_percent": int(float(util)),
                "used_gb": round(float(used_mb) / 1024, 1),
                "total_gb": round(float(total_mb) / 1024, 1)}
    except (ValueError, IndexError):
        return {"available": False, "util_percent": 0, "used_gb": 0.0, "total_gb": 0.0}


def _disk() -> dict:
    """数据盘剩余空间。**看的是数据根，不是系统盘**——数据盘满了才是真出事。"""
    try:
        root = str(db.data_root())
        usage = shutil.disk_usage(root)
        return {"used_gb": round(usage.used / (1024 ** 3), 1),
                "total_gb": round(usage.total / (1024 ** 3), 1),
                "free_gb": round(usage.free / (1024 ** 3), 1)}
    except OSError:
        return {"used_gb": 0.0, "total_gb": 0.0, "free_gb": 0.0}


def _cpu_cores() -> int:
    return os.cpu_count() or 1


# ---------------------------------------------------------------- 端点


@router.get("/overview")
def overview(user: dict = Depends(auth.current_user)):
    """总览。**常规用户只看到自己的**；管理员看到全局并多拿一个吞吐。"""
    is_admin = (user or {}).get("role") == "admin"
    tasks = store.list_tasks(None if is_admin else int(user["id"]),
                             is_admin=is_admin, limit=STATS_LIMIT)
    regular, rows = _split(tasks)
    counts = _counts(tasks)
    row_counts = _counts(rows)
    durations = _durations(tasks)
    done = counts["done"]
    failed = counts["failed"]
    out = {
        "scope": "admin" if is_admin else "user",
        "tasks": counts,
        "breakdown": {"regular": len(regular), "workflow": len(rows)},
        "workflow_rows": row_counts,
        "duration": _duration_block(durations),
        "success_rate": round(done / (done + failed), 4) if (done + failed) else 0.0,
    }
    if is_admin:
        # 全局速度：只对管理员给。常规用户拿不到别人任务的吞吐。
        out["throughput_per_hour"] = _throughput_per_hour(tasks)
    return out


@router.get("/system")
def system(_user: dict = Depends(auth.require_admin)):
    """系统性能四项。**只有管理员**——普通用户这里是 403，不是「拿到再隐藏」。"""
    return {
        "cpu": {"percent": _cpu_percent(), "cores": _cpu_cores()},
        "memory": _memory(),
        "gpu": _gpu(),
        "disk": _disk(),
    }


@router.get("/workflow/{workflow_id}")
def workflow_status(workflow_id: int, user: dict = Depends(auth.current_user)):
    """**单个**工作流的状态面板：行数分布、单行用时、预计还要多久。"""
    is_admin = (user or {}).get("role") == "admin"
    try:
        wf = store.get_workflow(int(workflow_id), owner_id=None if is_admin else int(user["id"]),
                                is_admin=is_admin)
    except store.NotFound:
        raise HTTPException(status_code=404, detail="工作流不存在")
    except store.StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    rows = store.list_workflow_rows(int(workflow_id))
    # 行任务：workflow_row 是行号（从 0 起），title 里带行号便于排查。
    tasks = [r for r in rows if r.get("status")]
    counts = {"done": 0, "failed": 0, "running": 0, "queued": 0, "skipped": 0}
    for r in rows:
        st = str(r.get("status") or "")
        if st in counts:
            counts[st] += 1
    durations = _durations(tasks)
    total_rows = int(wf.get("total_rows") or len(rows))
    done_rows = counts["done"]
    remaining = max(0, total_rows - done_rows - counts["failed"] - counts["skipped"])
    median = statistics.median(durations) if durations else 0.0
    eta = round(median * remaining) if remaining and median else 0
    return {
        "workflow_id": int(wf["id"]),
        "name": wf.get("name") or "",
        "status": wf.get("status") or "",
        "total_rows": total_rows,
        "rows": counts,
        "duration": _duration_block(durations),
        "eta_seconds": eta,
        "throughput_rows_per_hour": (
            round(len(durations) / (sum(durations) / 3600.0), 2)
            if durations and sum(durations) > 0 else 0.0),
    }


__all__ = ["router"]
