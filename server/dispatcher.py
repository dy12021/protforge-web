"""调度器（dispatcher.py）——计算进程的生命周期与双向 RPC 通道。

后台的 Node agent（``agent/run.mjs``，pi 的 RPC 模式）是**子进程**：后端 spawn 它、
持有它的 stdin/stdout、管理它的退出。调度器就是管这件事的，不碰打包也不碰鉴权。

四件事：

1. **接队列**：轮询 ``status='queued'``，按并发上限接走并 spawn。
2. **spawn**：四个环境变量注进去（``PROTFORGE_WEB_INTERNAL`` / ``PROTFORGE_TASK_ID``
   / ``PROTFORGE_AGENT_TOKEN`` / ``PROTFORGE_WORK_DIR``），**令牌绝不进 argv**——
   argv 会出现在任务列表页，也会进进程命令行历史。
3. **收尾分流**：进程退出时查 ``store.has_finished(task_id)``——
   **有**标记算正常收口，进 ``packaging`` 交给打包；**没有**标记就是异常结束，进
   ``failed``。这两条必须分得开，否则「算到一半崩了」会被当成算完，用户下到一个
   缺产物的包还不知道。
4. **中止**要杀整棵进程树：agent 起的 ``docker exec`` / ``wsl.exe`` 会变成孤儿继续
   烧 GPU。Windows 上 ``terminate()`` 只杀直接子进程，必须 ``taskkill /T /F``。
5. **重启恢复**：``self._running`` 是纯内存表，进程一起就没了。上一条命接走的任务
   在库里还是 ``running``，却再没有任何进程被跟踪——用户看到"永远在计算中"，同时
   那些 node agent 成了孤儿继续烧 GPU。派活时把 pid 落一份运行态记录（``run/``），
   ``start()`` 里据此收尾：**验身份** → 杀整棵进程树 → 落成带中文原因的终态。

**并发上限 = nvidia-smi 探测的 GPU 数**，因为 ``_common.sh`` 的 ``protforge_exec``
会自动挑空闲 GPU，上限等于卡数才不会互相抢卡。探测失败退回 1（不能因为没装
nvidia-smi 就起不来），手动配置**只允许下调**——配成独立的固定数字，用户加卡后
忘了改配置就等于超卖。

令牌是**进程内存里的字典**（``_TokenStore``）：一个 task_id 一个令牌、与 task_id
绑定、进程退出或重启即失效。不落库是因为 store 里没有这张表，而「重启即失效」
恰好就是「一次性」想要的语义。

**因此有一个硬前提：调度器必须与内部 app（8475）同进程。** 两个 uvicorn 是物理
隔离的（app.py 的 ``main()`` 明确分开跑），内存表跨进程看不见——调度器注进 env
的令牌会被内部 app 判成无效，agent 的每次回调都 401。推荐把 Dispatcher 挂在
**内部 app** 里：公开 app 只管把任务写进库（``queued``），调度器轮询接单，两边靠
SQLite 共享队列，本来就不需要共享内存。令牌校验请走
:func:`verify_agent_token`，不要另抄一份表。
"""

from __future__ import annotations

import collections
import datetime
import hmac
import json
import logging
import os
import queue
import secrets
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import db
import guards
import llm
import reflection
import sse
import store
import workflow as workflow_mod

log = logging.getLogger(__name__)

# --- 常量 ----------------------------------------------------------------------------

DEFAULT_INTERNAL_BASE = "http://127.0.0.1:8475"     # 内部 API 端口（plan Global Constraints）
DEFAULT_AGENT_SCRIPT = Path(__file__).resolve().parent.parent / "agent" / "run.mjs"
DEFAULT_PROTFORGE_DATA = guards.DEFAULT_PROTFORGE_ROOT   # 与守卫同一个来源，不另抄一份

#: 探测不到 GPU 时的兜底并发数。**有下限不是默认值**：宁可一次只跑一个，也不要
#: 因为 nvidia-smi 不在 PATH 就把平台判死。
DEFAULT_MAX_CONCURRENCY = 1

AGENT_TOKEN_TTL = 12 * 60 * 60                     # 令牌最长活 12 小时（兜底，正常几小时就退出）
RPC_TIMEOUT = 30.0                                 # 管理员 steer 等响应的默认上限
POLL_INTERVAL = 1.0                                # 队列轮询 + 退出检测的间隔
GPU_PROBE_TIMEOUT = 10.0
KILL_TIMEOUT = 15.0

# --- 反思（rsi）调参 -------------------------------------------------------------
# 一轮巡检最多入队几条。刻意给得小：巡检跑在 tick 主路径上（POLL_INTERVAL=1s），
# 而一次模型调用要几十秒起。入队多了只是让队列更长，不会更快。
REFLECTION_SWEEP_LIMIT = 3
# 后台反思线程数。比归档少：归档是本地磁盘 IO，这里每一次都是一次**要花钱的**
# 模型调用，堆线程只会更快地把 token 花掉，不会更快地拿到结果。
REFLECTION_WORKERS = 2
READER_JOIN_TIMEOUT = 5.0
#: agent 报完 finish 之后，给它自己收尾（关 stdin → runRpcMode 返回 → 进程退出）
#: 的宽限。逾时就杀进程树——宁可强杀也不能让任务永远停在「计算中」。
WIND_DOWN_GRACE = 10.0
MAX_LINE = 1 << 20                                 # 单行 stdout 上限 1 MB，防 agent 吐大 blob 撑爆内存
MAX_EVENTS = 5000                                  # 事件流缓冲上限，溢出丢最旧并计数

# --- 重启恢复 ------------------------------------------------------------------------
#
# self._running 是**纯内存**表，进程一起就没了。上一条命接走的任务在库里仍是
# running，却没有进程被跟踪：用户看到"永远在计算中"，而那些 node agent 成了孤儿，
# Docker 里的算还在烧 GPU。派活时必须把 pid 落一份盘，start() 时据此收尾。

RUN_DIRNAME = "run"                 # 运行态记录目录。**不放 logs/**：janitor 对
                                    # logs/ 有名字形态闸（^agent-\d+(\.err)?\.log），
                                    # 多一种后缀会被记成命名漂移；run/ 不参与清扫，
                                    # 记录由 _settle 自己删。
RECOVER_BUDGET = 20.0               # 恢复一轮的总时限（秒）。杀进程树可能卡在
                                    # taskkill 上，不能让 N 个孤儿把启动拖成 N×15 秒。
PID_REUSE_SLACK = 2.0               # 进程创建时间允许的误差（秒）
#: _abort_signal 回看末尾多少条事件去找"进终态那条"。见那里的说明。
_ABORT_LOOKBACK = 200
#: 无记录补扫用的哨兵 run_id。固定值 = 两个实例补扫同一个任务会抢同一个锁。
_NO_RECORD = "no-record"

_INTERNAL_ENV = "PROTFORGE_WEB_INTERNAL"
_MAX_CONCURRENCY_ENV = "PROTFORGE_WEB_MAX_CONCURRENCY"
_NODE_ENV = "PROTFORGE_WEB_NODE"
_AGENT_SCRIPT_ENV = "PROTFORGE_WEB_AGENT_SCRIPT"

#: 归档线程池大小。**下限是 2，不是调优参数**。
#:
#: 一条 write_back 是整表 load + 整表 save（宽表实测 2.2 秒），只要它挂在
#: 网络盘、Excel 独占或坏盘上不返回，单线程就会让**整个归档队列停摆**——
#: 而闸门② 等的正是这些行，于是全平台所有工作流一起停住，线程却还活着、
#: 看起来一切正常（实测：3 行全部滞留 _archive_inflight、闸门② 全死）。
#:
#: **不要做「超时后重排」**：重排会让同一行被写两次，重复写竞态引入的
#: 数据损坏比它解决的问题更糟。宁可让这一行慢慢等，也不写第二次。
#:
#: **并发是按工作流分段的，不是按行的**（Major-2）：同一工作流任意时刻只有一行
#: 在途（见 `_archive_wf_busy` 与 `_archive_wf_lock`），不同工作流之间仍然各跑各的。
#: 分段而不是全局一把锁，是这个下限 2 还有意义的前提。
ARCHIVE_WORKERS = 2
#: 入队多久还没写完，就给该行记一条用户可见的「卡住了」。正常 write_back
#: 几秒内返回，超过 30 秒基本可以断定结果表在网络盘上或被占用。
ARCHIVE_STUCK_SECONDS = 30.0

_DEAD = object()                                   # 进程死了，塞给还在等响应的 rpc()


# --- 异常 ----------------------------------------------------------------------------


class DispatcherError(Exception):
    """调度器自身的问题（不是 store 的）。"""


class AgentGone(DispatcherError):
    """目标计算进程已经不在了（没起来、已退出、已被中止）。"""


class RpcTimeout(DispatcherError):
    """命令写进去了，但进程没在期限内回应。"""


# --- 令牌 ----------------------------------------------------------------------------


class _TokenStore:
    """``task_id -> (token, 签发时刻)`` 的内存表。

    一个 task_id 同时只有一个有效令牌，重新 spawn 覆盖旧的——旧的进程已经死了，
    让它继续能用等于给了一个没人看管的入口。
    """

    def __init__(self, ttl: float = AGENT_TOKEN_TTL):
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._items: dict[int, tuple[str, float]] = {}

    def issue(self, task_id: int) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._purge()
            self._items[int(task_id)] = (token, time.time())
        return token

    def verify(self, task_id: int, token) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            self._purge()
            item = self._items.get(int(task_id))
        if item is None:
            return False
        # 恒定时间比较：令牌长度已知，比较耗时不该泄漏前缀信息
        return hmac.compare_digest(item[0], token)

    def revoke(self, task_id: int) -> None:
        with self._lock:
            self._items.pop(int(task_id), None)

    def bound_task(self, token) -> int | None:
        """令牌 -> 它绑定的 task_id。没配对或过期返回 None。

        表是按 task_id 建的，:meth:`verify` 只能回答"这个令牌对不对得上这个任务"。
        内部 API 还需要区分**两种错**：令牌根本不存在（401）与令牌是真的但用在了
        别的任务上（403）——后者是拿 task 1 的钥匙去开 task 2 的门，提示语不该一样。
        所以这里补一个反查，让 API 层能说出人话，而不是一律"令牌无效"。
        """
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            self._purge()
            items = list(self._items.items())
        try:
            for tid, (known, _) in items:
                if hmac.compare_digest(known, token):
                    return tid
        except TypeError:
            # compare_digest 碰到非 ASCII 的 str 会抛，而不是返回 False。令牌是
            # base64url，本来就不该有非 ASCII；真来了就当它没配对——绝不能让鉴权
            # 端点因为一个畸形请求头返回 500。
            return None
        return None

    def _purge(self) -> None:
        now = time.time()
        for tid, (_, issued) in list(self._items.items()):
            if now - issued > self.ttl:
                self._items.pop(tid, None)


_TOKENS = _TokenStore()


def issue_agent_token(task_id: int) -> str:
    """给 task_id 签一个一次性令牌。内部 API 建 agent 通道时调它。

    令牌表是**模块级单例**：同进程内所有 Dispatcher 共用一张（所以别指望换个
    Dispatcher 实例就能作废令牌），要作废就调 :func:`revoke_agent_token`。
    内部 API 校验 agent 通道时用 :func:`verify_agent_token`。
    """
    return _TOKENS.issue(task_id)


def verify_agent_token(task_id: int, token) -> bool:
    """校验「令牌 == 属于这个 task_id 的有效令牌」。

    内部端点 ``/api/internal/agent/{task_id}/*`` 必须先过这一关，否则拿 task 1 的
    令牌就能改 task 2 的状态。
    """
    try:
        return _TOKENS.verify(task_id, token)
    except (TypeError, ValueError):
        return False


def revoke_agent_token(task_id: int) -> None:
    _TOKENS.revoke(task_id)


def agent_token_task(token) -> int | None:
    """令牌 -> 绑定的 task_id；查不到或已过期返回 None。

    内部 app 的 agent 通道用它把 401（令牌不存在）和 403（令牌用错了任务）分开。
    表还是 :data:`_TOKENS` 这一张——app.py **不要**再抄一份自己的令牌表，两份表
    在两个进程里会各说各话，症状是 agent 每一步回调都 401。
    """
    return _TOKENS.bound_task(token)


# --- 模型配置 ------------------------------------------------------------------------
#
# 端点、密钥来源、物化 models.json 这几件事住在 llm.py（反思也用同一套端点，
# 两边各抄一份的话，改了一边另一边不跟着变）。这里只留一个别名，
# 免得外部按 dispatcher.LLM_PROVIDER_ID 取这个名字的地方断掉。
LLM_PROVIDER_ID = llm.LLM_PROVIDER_ID


# --- GPU 探测 ------------------------------------------------------------------------

GPU_PROBE_CMD = ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"]


def probe_gpu_count() -> int:
    """``nvidia-smi`` 报几张卡就返回几；报不出来（没装、不在 PATH、超时）返回 0。

    返回 0 而不是抛异常：这个函数被并发上限的唯一输入，探测失败不该让整个平台
    起不来（调用方会退回 ``DEFAULT_MAX_CONCURRENCY``）。
    """
    kwargs = {}
    if os.name == "nt":
        # 别为了数个卡闪一个控制台窗口出来
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(GPU_PROBE_CMD, capture_output=True, text=True,
                             timeout=GPU_PROBE_TIMEOUT, **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("nvidia-smi 探测失败，按 0 张卡处理：%s", e)
        return 0
    if out.returncode != 0:
        log.warning("nvidia-smi 返回 %s，按 0 张卡处理", out.returncode)
        return 0
    return sum(1 for line in (out.stdout or "").splitlines() if line.strip())


def _norm_image(path) -> str:
    """进程映像名归一：只留 basename，去掉 .exe，转小写。

    记下来的是 ``node`` 这种短名（可能只是 PATH 里的一个词），而系统报回来的是
    ``C:\\Program Files\\nodejs\\node.exe``，直接比字符串永远不相等。
    """
    return os.path.basename(str(path or "")).removesuffix(".exe").lower()


def _proc_fingerprint(pid) -> tuple[str, float] | None:
    """pid -> (映像/命令行, 进程创建时刻)；**进程不在就返回 None**。

    用来回答"这个 pid 还是不是我们起的那个进程"。单看 pid 不够：Windows 与 Linux
    都会很快复用 pid，一个睡了几小时的孤儿号很可能已经被无关程序占着，不验身份
    就 taskkill 等于替用户杀掉他随手开的编辑器。

    创建时刻是两个平台都拿得到、且**不会随 pid 复用而相同**的量：Windows 走
    ``GetProcessTimes``，Linux 走 ``/proc/<pid>/stat`` 的 starttime 字段（开机以来
    的时钟滴答数，不是墙上时间，不受改系统时间影响）。故意只用标准库——为一个
    查身份的活引第三方依赖不值得，本项目也没有这个依赖。

    拿不到就返回 None，调用方必须据此**放弃杀进程**（判不出来 ≠ 判成是我们自己的）。
    """
    pid = int(pid)
    if pid <= 0:
        return None
    if os.name == "nt":
        return _win_fingerprint(pid)
    return _posix_fingerprint(pid)


def _win_fingerprint(pid: int) -> tuple[str, float] | None:
    """Windows：QueryFullProcessImageNameW 拿映像路径，GetProcessTimes 拿创建时刻。

    两个 API 都只要 ``PROCESS_QUERY_LIMITED_INFORMATION``，不需要 SeDebugPrivilege，
    也**不额外起进程**——恢复轮里每个孤儿都要问一次，起 PowerShell 的话几十个孤儿
    就是几十秒。
    """
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    # 不显式声明 argtypes/restype 的话，64 位下 HANDLE 会被截成 32 位整数，
    # OpenProcess 返回值高位丢失 → 拿到一个乱句柄。必须声明。
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                             wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)]
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.GetProcessTimes.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL

    handle = k.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None                                  # 进程没了（或无权查）→ 当它不在
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        image = buf.value
        created = wintypes.FILETIME()
        other = [wintypes.FILETIME() for _ in range(3)]
        if not k.GetProcessTimes(handle, ctypes.byref(created), *other):
            return image, 0.0                        # 拿不到时刻：还剩映像名可用
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return image, ticks / 1e7 - 11644473600.0     # FILETIME(1601 起 100ns) → unix 秒
    finally:
        k.CloseHandle(handle)


def _posix_fingerprint(pid: int) -> tuple[str, float] | None:
    """Linux 走 /proc；macOS/BSD 没有 /proc，用 ps 兜底。"""
    try:                                              # Linux
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
        # comm 字段带括号且可能含空格，从**最后一个** ')' 之后切才是第 3 个字段
        # （state）。starttime 是整表第 22 个字段，即切分后下标 19。
        fields = raw[raw.rindex(b")") + 2:].split()
        start = int(fields[19]) / float(os.sysconf("SC_CLK_TCK"))
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        return cmd or f"pid-{pid}", start
    except (OSError, ValueError, IndexError):
        pass
    try:                                              # macOS / BSD
        def _ps(fmt):
            out = subprocess.run(["ps", "-p", str(pid), "-o", fmt],
                                 capture_output=True, text=True,
                                 timeout=GPU_PROBE_TIMEOUT, check=False)
            return (out.stdout or "").strip() if out.returncode == 0 else ""

        command = _ps("command=")
        if not command:
            return None
        # lstart 的格式随 locale 变，解析不出来就只交命令行，交给调用方比映像名
        start = 0.0
        raw_start = _ps("lstart=")
        for fmt in ("%a %b %d %H:%M:%S %Y", "%c"):
            try:
                start = datetime.datetime.strptime(raw_start, fmt).timestamp()
                break
            except ValueError:
                continue
        return command, start
    except (OSError, subprocess.SubprocessError):
        return None


def _kill_tree_pid(pid, timeout: float = KILL_TIMEOUT) -> int | None:
    """按 pid 杀掉进程**整棵树**，返回 taskkill 的退出码（拿不到返回 None）。

    恢复路径上没有 :class:`subprocess.Popen` 句柄（进程属于上一条命），只有 pid，
    所以树杀要能按 pid 发起。Windows 上仍然必须 ``/T /F``：只杀 node 本体的话，
    它派生的 ``docker exec`` / ``wsl.exe`` 会活下来继续烧 GPU。
    """
    pid = int(pid)
    if pid <= 0:
        return None
    if os.name == "nt":
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        try:
            out = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                                 capture_output=True, text=True, timeout=timeout, **kwargs)
            return out.returncode
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("taskkill %s 失败：%s", pid, e)
            return None
    # POSIX：能整组杀就整组杀，但**先确认它不在我们这一组**。本模块 spawn 时用了
    # start_new_session，所以在跑的进程总是独立进程组；可恢复路径拿到的是孤儿——
    # 万一当初没起独立组，killpg 会连调度器自己一起杀掉。
    pgid = None
    try:
        pgid = os.getpgid(pid)
    except (OSError, AttributeError):
        pgid = None
    if pgid is not None and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
            return 0
        except OSError as e:
            log.warning("killpg %s 失败，退回单进程：%s", pgid, e)
    try:
        os.kill(pid, signal.SIGKILL)
        return 0
    except OSError as e:
        log.warning("kill %s 失败：%s", pid, e)
        return None


def kill_tree(proc: subprocess.Popen, timeout: float = KILL_TIMEOUT) -> int | None:
    """杀掉进程**整棵树**，返回 taskkill 的退出码（拿不到返回 None）。

    Windows 上 ``proc.terminate()`` 只杀直接子进程：agent 起的 ``docker exec`` /
    ``wsl.exe`` 会脱离出去继续烧 GPU，而且没人再管它。``taskkill /T /F`` 才是
    「连子孙一起杀」，``/F`` 是为了不让它有机会留下半截输出文件。

    树杀逻辑本身在 :func:`_kill_tree_pid`（恢复路径只有 pid，没有句柄），这里只补
    句柄兜底：按 pid 杀不动时退回 ``terminate()``。
    """
    code = _kill_tree_pid(proc.pid, timeout)
    if code is not None:
        return code
    try:
        proc.terminate()
    except OSError:
        pass
    return None


# --- 进程句柄 ------------------------------------------------------------------------


@dataclass
class AgentProc:
    """一个计算进程的句柄。

    ``env`` / ``argv`` 故意留成公开属性：注入对不对、令牌有没有混进 argv，
    都得能从外面一眼看出来（测试就是这么钉的）。
    """

    task_id: int
    pid: int
    argv: list
    env: dict
    work_dir: Path
    token: str
    log_path: Path
    stderr_path: Path
    started_at: str
    brief_path: Path
    proc: subprocess.Popen = field(default=None, repr=False)
    stderr_fh: object = field(default=None, repr=False)
    reader: threading.Thread = field(default=None, repr=False)

    aborted: bool = False                    # 管理员明确中止过
    abort_reason: str = ""                   # 为什么中止；收尾时用它当失败原因
    settled: bool = False                    # 已被收尾（幂等闸）
    exit_code: int | None = None
    eof: bool = False                        # stdout 读完
    seq: int = 0                             # 事件自增序号（stream 的游标）
    dropped: int = 0                         # 事件缓冲溢出丢弃数
    last_rpc_id: str | None = None
    #: 模型最后一段纯文本回复。agent 自己结束而没调 finish 时，**它就是
    #: 唯一的结论**——不拿它当摘要，这条任务就只剩一句「没报收口」。
    last_reply: str = ""

    events: collections.deque = field(default_factory=collections.deque, repr=False)
    responses: dict = field(default_factory=dict, repr=False)
    cond: threading.Condition = field(default_factory=threading.Condition, repr=False)


def _is_response(obj: dict) -> bool:
    """响应 vs 事件：agent 明确写了 type 就听它的，没写就看有没有 ``ok``。"""
    kind = obj.get("type")
    if kind:
        return kind == "response"
    return "ok" in obj


def _append_log(path: Path, line: str) -> None:
    """原样留一份 stdout。JSON 解析失败时的排查全靠它。"""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass          # 日志写不进去不能带崩事件流


def _db_status(task_id: int) -> str:
    """读库里的任务状态；读不到就返回空串（这一轮当作没看见）。"""
    try:
        return str(store.get_task(task_id, is_admin=True).get("status") or "")
    except Exception:                                 # noqa: BLE001 - 轮询线程不能死
        log.exception("读任务 %s 状态失败", task_id)
        return ""


#: 只有**人**按下的中止会在终态事件上留这个 actor。全库仅 public app 的
#: ``/api/tasks/{id}/abort`` 写它（打包失败 app._fail_task、调度器收尾
#: :meth:`Dispatcher._mark_failed` 都是 system）。见 :func:`_abort_signal`。
_ABORT_ACTOR = "human"

#: 中止事件没有文案时的兜底原因。不能留空：空字符串会让详情页显示不出失败原因。
ABORT_FALLBACK_REASON = "用户中止了任务"


def _abort_signal(task_id: int, status: str) -> str | None:
    """这个终态**是不是人按的**？是则返回中止文案，不是则返回 ``None``。

    跨进程中止的唯一信道是数据库（用户点中止跑在 public app 那个进程里，
    改得到库、改不到这里的进程）。但**库里的状态本身不能当中止的判据**：
    ``failed`` 是个笼统终态，打包失败、agent 异常、磁盘满都会写它，早年的
    :meth:`Dispatcher._reap` 靠 ``status == "failed"`` 猜，结果是"算到一半崩了"
    被当成用户中止，:meth:`Dispatcher._settle` 写出"任务已被中止"这种张冠李戴
    的原因。

    判据因此落在**进终态的那条 status 事件的 actor 上**，理由有三条：

    1. **不用改 schema**：``events.actor`` 的 CHECK 本来就含 human，时间线又是
       只增不改的，改判据不需要迁移表结构（db.py / store.py 都不在本模块的改动
       范围里，判据复制一份 SQL 过去也只会和真正的事件类型漂移）。
    2. **语义正交**：actor 说的是"谁干的"，status 说的是"干到哪一步了"。中止是
       人的动作，不是某一步的失败，用 actor 表达才不会和别的失败路径混淆。
    3. **不写死 failed**：这里只问"最后一条进**这个**终态的事件是谁写的"，所以将来
       新增 ``cancelled`` 之类表示中止的终态（写它的那个端点同样会用
       actor=human），判据照样成立，不必再改这一行。

    向后兼容：老任务就是"已经 failed + 带一条 actor=human 的终态事件"，走的是同
    一条路径，能被认出来。

    文案取那条事件的 message（public app 写的是"用户中止了任务"），用作收尾原因，
    用户在详情页看到的就是自己按下中止时的那句话。
    """
    if status not in db.TERMINAL_STATUSES:
        return None                      # 还没进终态，谈不上中止
    try:
        # 只看末尾一段事件：状态迁移每任务至多 6 次，末尾 200 条必然含最后那次迁移，
        # 而 list_events 是从头取 limit 条的，长任务必须先定位到游标附近。
        cursor = max(0, store.latest_event_id(task_id) - _ABORT_LOOKBACK)
        rows = store.list_events(task_id, after_id=cursor, limit=_ABORT_LOOKBACK)
    except Exception:                     # noqa: BLE001 - 判不出来就当没中止，别误杀进程
        log.exception("读任务 %s 的事件失败，无法判断是否中止", task_id)
        return None
    for row in reversed(rows):
        if row.get("kind") != "status" or row.get("to_status") != status:
            continue
        if str(row.get("actor") or "") != _ABORT_ACTOR:
            return None                  # 进终态的是系统，不是人 → 不是中止
        return str(row.get("message") or "")
    return None                          # 压根没有进终态这条事件 → 不是中止


def _restart_reason(pid: int, cleaned: bool) -> str:
    """服务重启后收尾遗留任务的失败原因。**不许静默改状态。**

    用户看到的是一个已经算不成、也不打算自动重算的任务（重跑可能有几万步的副作用，
    悄悄重排队的代价比让他自己按一次重提交大）。所以文案必须把"为什么"说全：不是
    任务算错了，是服务重启把进程带走了。

    ``cleaned`` 只在**确认进程真的没了**时为真。没确认就不能说"已清理"——否则用户
    以为 GPU 空出来了，其实还在替他烧。
    """
    if cleaned:
        return (f"服务重启，计算进程已中断（残留进程树 pid={pid} 已清理）；"
                f"这次计算没有完成，需要的话请重新提交")
    if pid > 0:
        return (f"服务重启，计算进程已中断（原进程 pid={pid} 未能确认清理，请检查 "
                f"是否还有残留计算进程）；这次计算没有完成，需要的话请重新提交")
    return "服务重启，计算进程已中断；这次计算没有完成，需要的话请重新提交"


def _notify() -> None:
    """写完事件叫醒 SSE 订阅者（Task 6 的交接项）。

    没有订阅者时是零成本空转。叫醒之后内容仍然从库里读，所以漏叫一次最多迟
    3 秒（sse 的轮询兜底），不会丢事件——这是能要的最好结果，不该反过来让它
    带崩调度器。
    """
    try:
        sse.notify()
    except Exception:                     # noqa: BLE001 - 叫醒失败不是调度器的故障
        log.debug("SSE 叫醒失败", exc_info=True)


# --- 运行态记录（重启恢复用） -------------------------------------------------------
#
# tasks 表里**没有 pid 列**（schema 在 db.py，本模块不碰它），而 self._running 是纯内存
# 的，进程一起就没了。所以派活时把 pid 与它的身份指纹落一份小 JSON 到 run/<id>.json，
# 收尾时删掉。落盘的是运行态而不是业务数据：丢了最坏结果是恢复时少杀一个孤儿，
# 不会让任务算错，所以刻意不进数据库、不做迁移。


def _record_path(run_dir: Path, task_id: int) -> Path:
    return Path(run_dir) / f"{int(task_id)}.json"


def _claim_path(run_dir: Path, task_id: int, run_id: str) -> Path:
    """认领锁的文件名**带 run_id**：新一次派活换一把锁，不会被上一条命的残锁堵住。"""
    return Path(run_dir) / f"{int(task_id)}-{run_id}.claim"


def _verify_pid(record: dict) -> tuple[bool, str]:
    """这个 pid 还是不是我们起的那个 agent？返回 (能不能杀, 理由)。

    两道闸，缺一不可：

    1. **进程得还在**（``_proc_fingerprint`` 返回 None）。顺带把"上一条命的进程早就
       退了"这种情况从"杀"降级成"只是账没销"。
    2. **创建时刻对得上**。pid 会被复用，创建时刻不会；差出 :data:`PID_REUSE_SLACK`
       就是别人的进程。Windows 上再比一遍映像名（node vs node.exe 这类短名/全名
       差异由 :func:`_norm_image` 吸收）。

    记录里压根没有指纹（派活时就没查到）时**一律不杀**：判不出来不等于判成自己的，
    宁可留一个孤儿，也不能替用户杀掉他无关的程序。
    """
    pid = int(record.get("pid") or 0)
    fp = _proc_fingerprint(pid)
    if fp is None:
        return False, f"原计算进程 pid={pid} 已经不在了"
    image, start = fp
    want_start = float(record.get("start_epoch") or 0.0)
    if want_start and start and abs(start - want_start) > PID_REUSE_SLACK:
        return False, f"pid={pid} 已被别的进程复用（{image}），不杀"
    want_image = record.get("image") or record.get("node_exe")
    if want_image and os.name == "nt" and _norm_image(image) != _norm_image(want_image):
        return False, f"pid={pid} 的映像是 {image}，不是我们起的 {want_image}，不杀"
    return True, f"残留计算进程 pid={pid}"


def _claim(run_dir: Path, task_id: int, run_id: str) -> bool:
    """抢恢复的认领权。抢不到就返回 False（别的实例正在收尾同一个 pid）。

    ``O_CREAT | O_EXCL`` 是文件系统级的原子原语，与 :func:`store.claim_queued` 在
    SQLite 里那句 ``UPDATE ... WHERE status='queued'`` 同源：两个调度器实例同时启动
    （或者有人在另一个 app 里手动拉起一个）时，只有一个会去杀那个 pid、落那个终态，
    另一个直接让开——否则两边各写一条"服务重启"事件、并发 taskkill 同一个号。

    与 claim_queued 不同的是这里不用库：认领的是**这台机器上的某个 pid**，不是库里
    的一行，写文件正好对得上，而且不碰 store 就不受另一个人正在改 store 的影响。
    """
    try:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_claim_path(run_dir, task_id, run_id)),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError as e:
        # 建不出锁文件不算致命：退化成"不杀进程，只销账"，见 _recover_one
        log.warning("抢恢复认领锁失败，任务 %s：%s", task_id, e)
        return True
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def _release_claim(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


class Dispatcher:
    """队列 + 子进程池。

    ``Dispatcher(db, data_root)`` 是计划里的签名；``db_module`` 缺省就是本模块导入的
    ``db``，``data_root`` 缺省读 ``db.data_root()``（测试指 tmp_path，生产指 E 盘）。

    :param node_exe:       跑 agent 的解释器，默认 ``node``（``PROTFORGE_WEB_NODE`` 可覆盖）
    :param agent_script:   agent 入口，默认 ``<repo>/agent/run.mjs``
    :param max_concurrency: 手动并发上限，**只允许下调**探测值
    :param gpu_probe:      注入的探测函数（测试用），默认 :func:`probe_gpu_count`
    """

    def __init__(self, db_module=None, data_root=None, *, node_exe=None,
                 agent_script=None, internal_base=None, max_concurrency=None,
                 gpu_probe=None, rpc_timeout=None, poll_interval=None):
        self.db = db_module or db
        self.data_root = Path(data_root) if data_root else self.db.data_root()
        self.node_exe = str(node_exe or os.environ.get(_NODE_ENV) or "node")
        self.agent_script = Path(agent_script or os.environ.get(_AGENT_SCRIPT_ENV)
                                 or DEFAULT_AGENT_SCRIPT)
        self.internal_base = str(internal_base or os.environ.get(_INTERNAL_ENV)
                                 or DEFAULT_INTERNAL_BASE)
        self.rpc_timeout = float(rpc_timeout or RPC_TIMEOUT)
        self.poll_interval = float(poll_interval or POLL_INTERVAL)
        self.logs_dir = self.data_root / "logs"
        self.run_dir = self.data_root / RUN_DIRNAME    # 运行态记录，见 _verify_pid 上方
        #: 最近一次恢复的账（start() 时写）。给测试与排障看"启动到底收了几条"。
        self.last_recovery: dict = {"candidates": 0, "settled": [], "killed": [],
                                    "left_alone": [], "errors": []}
        self.gpu_count = max(0, int((gpu_probe or probe_gpu_count)()))
        self.limit = self._resolve_limit(max_concurrency)

        self._lock = threading.RLock()
        self._running: dict[int, AgentProc] = {}
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stopping = False

        # --- 归档工作线程 -----------------------------------------------------------
        # openpyxl 不支持增量写：一次 write_back 是整表 load + 整表 save（宽表实测
        # 2.2 秒）。放在 tick 主路径里同步做，等于每一轮调度（放行常规任务、心跳、
        # 超时清理）都白等两秒，而 POLL_INTERVAL 只有 1 秒。所以归档**只入队**，
        # 真正的读写发生在 :meth:`_archive_loop` 那条后台线程上。
        self._archive_lock = threading.Lock()
        self._archive_q: queue.Queue = queue.Queue()
        self._archive_inflight: set[int] = set()
        #: **已经在队列里或正在写的工作流**。同一工作流任意时刻至多一行在途：
        #: 结果表是**同一个文件**，两条 write_back 同时进去就是 read-modify-write
        #: 互相覆盖——后写的拿它自己那份更早读到的旧内容盖上去，前一行的值永久
        #: 丢失，而两行都会 workflow_archived=1，归档 pass 再也不会回头（Major-2）。
        #: 按 task_id 去重挡不住这个：同一工作流的两行各有各的 task_id。
        #: 与 `_archive_wf_locks` 是两道：**入队时**这条保证一个卡住的工作流只占
        #: 一条线程（平台不会整体停摆），**执行时**那把锁兜住一切别的入口。
        self._archive_wf_busy: set[int] = set()
        #: workflow_id -> 它自己的归档锁。**分段加锁**：不同工作流各拿各的锁，
        #: 跨工作流仍然并发（全局一把锁会把归档串成单线程）。
        self._archive_wf_locks: dict[int, threading.Lock] = {}
        #: 代表线程（池里第 0 条）。既有断言和外部代码只看一条，留个引用。
        self._archive_thread: threading.Thread | None = None
        self._archive_threads: list[threading.Thread] = []
        self._archive_stopped = False
        #: 一个 job 从入队到完成的时限，超了就告警（见 _archive_watchdog）。
        self._archive_stuck_seconds = ARCHIVE_STUCK_SECONDS
        self._archive_queued_at: dict[int, float] = {}
        #: 已经就「卡住」告过警的行。同一行不重复告警，否则又是一条事件风暴。
        self._archive_warned: set[int] = set()

        # --- 反思工作线程 -----------------------------------------------------------
        # 反思要调一次模型（几十秒到几分钟），放在 tick 主路径里同步做等于整个
        # 调度器每轮都停在那里。所以和归档一样：**只查库、只入队**，模型调用与
        # 写文件都发生在后台线程上。
        self._refl_lock = threading.Lock()
        self._refl_q: queue.Queue = queue.Queue()
        #: 已在队列里或正在复盘的任务。防止两次 tick 把同一条投两遍——真投两遍
        #: 也不会产生第二条记录（reflections.task_id 上有 UNIQUE），但那意味着
        #: 白烧一次模型调用，而 token 是要花钱的。
        self._refl_inflight: set[int] = set()
        self._refl_thread: threading.Thread | None = None
        self._refl_threads: list[threading.Thread] = []
        self._refl_stopped = False

    # --- 并发上限 ---------------------------------------------------------------

    def _resolve_limit(self, manual) -> int:
        """探测值是天花板，手动值只能往下压。"""
        base = self.gpu_count or DEFAULT_MAX_CONCURRENCY
        if manual is None:
            env_manual = os.environ.get(_MAX_CONCURRENCY_ENV)
            manual = int(env_manual) if env_manual and env_manual.strip().isdigit() else None
        if manual is None:
            return max(1, base)
        try:
            manual = int(manual)
        except (TypeError, ValueError):
            return max(1, base)
        if manual <= 0:                 # 0/负数当成「没配」，别把人自己锁死
            return max(1, base)
        return max(1, min(manual, base))

    # --- 生命周期 ---------------------------------------------------------------

    @property
    def running(self) -> dict:
        """在跑的 ``task_id -> AgentProc`` 快照。"""
        with self._lock:
            return dict(self._running)

    def start(self) -> None:
        """起后台线程轮询队列。重复调用是空操作。

        **先恢复再开线程**：上一条命接走的任务在库里还是 running，而本进程的
        ``_running`` 是空的，没有任何东西会去收尾它们——用户看到的是"永远在计算
        中"，同时那些 node agent 成了孤儿继续烧 GPU。恢复必须发生在起线程之前，
        否则新接的任务会先占满并发，恢复还被排在后面。
        """
        self.recover()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop, name="dispatcher", daemon=True)
            self._thread.start()
        log.info("调度器启动：并发上限 %s（探测到 %s 张 GPU）", self.limit, self.gpu_count)

    # --- 重启恢复 ----------------------------------------------------------------

    def recover(self) -> dict:
        """收尾「上一条命」留下的 running 任务，返回一张账。

        每个 ``run/<id>.json`` 代表一个曾被派出去、进程可能还活着的 agent：
        查身份 → 杀整棵进程树 → 把任务落成 ``failed`` 并写明中文原因。

        **全程不抛**：库不可达、记录损坏、taskkill 卡住都只记日志。恢复是启动前的
        善后，坏掉的善后不该让平台起不来——用户至少还能进来看任务列表。
        :data:`RECOVER_BUDGET` 是总时限，杀进程树最坏要 KILL_TIMEOUT 秒一棵树，
        不能让 N 个孤儿把启动拖成 N×15 秒。
        """
        summary = {"candidates": 0, "settled": [], "killed": [],
                   "left_alone": [], "errors": [], "no_record": 0}
        self.last_recovery = summary
        try:
            records = self._run_records()
        except Exception:                         # noqa: BLE001 - 读不到就当没有孤儿
            log.exception("读取运行态记录失败，跳过重启恢复")
            return summary
        summary["candidates"] = len(records)
        # 两条路共用一个时限：记录路要先跑，补扫路必须接着在同一个预算里。
        # 所以它在早退分支**之前**就定下来。
        deadline = time.monotonic() + RECOVER_BUDGET
        seen: set = set()
        if not records:
            # **没有记录不等于没有遗留任务**，见 _recover_recordless 的说明。
            # 这里不能直接 return——库里还挂着 running 的行时，磁盘上没记录，
            # 但那些任务同样永远没人收尾。
            self._recover_recordless(summary, deadline, known=seen)
            return summary
        log.info("重启恢复：发现 %s 条遗留 running 记录", len(records))
        for task_id, record in records:
            if self._stopping:
                break
            if time.monotonic() > deadline:
                # 超时就不管了：宁可留一个孤儿在库里显示 running，也不能把服务启动
                # 卡死。剩下的记进 left_alone，日志里点名。
                log.warning("重启恢复超时，%s 条记录留给下一轮人工处理", task_id)
                summary["left_alone"].append(task_id)
                continue
            try:
                self._recover_one(task_id, record, summary)
            except Exception:                     # noqa: BLE001 - 单条失败不带走整轮
                log.exception("恢复遗留任务 %s 失败", task_id)
                summary["errors"].append(task_id)
            finally:
                # 记下来，补扫要靠它避开已经走完记录这条路的任务
                seen.add(task_id)
        self._recover_recordless(summary, deadline, known=seen)
        log.info("重启恢复结束：收尾 %s、清理孤儿 %s、无记录收尾 %s、放过 %s、出错 %s",
                 len(summary["settled"]), len(summary["killed"]),
                 summary["no_record"], len(summary["left_alone"]), len(summary["errors"]))
        return summary

    def _run_records(self) -> list:
        """读出全部运行态记录，按 task_id 升序（先来的先收）。

        认不出来的一律跳过并单独告警：记录是运行态，坏了最多丢一个孤儿的清理，
        不该在启动路径上抛异常。
        """
        out: list = []
        if not self.run_dir.is_dir():
            return out
        try:
            entries = sorted(self.run_dir.glob("*.json"), key=lambda p: p.name)
        except OSError as e:
            log.warning("扫运行态目录失败：%s", e)
            return out
        for path in entries:
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                task_id = int(rec["task_id"])
                pid = int(rec.get("pid") or 0)
            except (OSError, ValueError, TypeError, KeyError) as e:
                log.warning("运行态记录 %s 认不出来，跳过：%s", path.name, e)
                continue
            if pid <= 0:
                log.warning("运行态记录 %s 没有 pid，跳过", path.name)
                continue
            out.append((task_id, rec))
        return out

    def _recover_recordless(self, summary: dict, deadline: float,
                            known: set) -> None:
        """收尾「库里是 running、磁盘上却没有运行态记录」的任务。

        为什么这一路必须单独存在
        ----------------------
        :meth:`recover` 遍历的是 ``run/<id>.json``，因为 pid 只存在那儿。
        于是「库里 running + 没有记录」这一类会**整轮被漏掉**，任务永远停在
        「计算中」。这不是理论情况，两个来源：

        1. **派活途中崩了**：``claim_queued`` 把 queued 改成 running 之后、
           ``_spawn`` 写运行态记录之前有个窗口，进程在这个窗口里死掉，库就留下
           一条 running 而磁盘上没有对应记录。
        2. **本次修复之前派出去的任务**：记录机制是后加的，老任务的 running 行
           天生没有记录（实测本机数据盘上就有四条）。

        这类任务**没有 pid 可杀**——记录是 pid 的唯一来源，没有它就不该猜：
        猜 pid 去杀，撞上 PID 复用就是在杀无关程序。所以这里只做状态收尾，
        不动手，并把「为什么杀不了」如实写进原因里。

        抢锁用的是同一个 ``_claim``，哨兵 run_id 固定成 :data:`_NO_RECORD`，
        于是两个实例补扫同一个任务时只有一方会动手。**抢到就要还**——认领锁靠删文件
        释放，run/ 又不参与清扫，残锁会把同一个 task_id 永远挡在补扫之外。
        """
        try:
            rows = store.list_tasks(status="running", is_admin=True, limit=500)
        except Exception:                             # noqa: BLE001 - 库不可达就跳过
            log.exception("读取 running 任务失败，跳过无记录补扫")
            return

        orphans = [int(r["id"]) for r in rows
                   if int(r["id"]) not in self.running
                   and int(r["id"]) not in known]
        if not orphans:
            return
        log.info("重启恢复：另有 %s 条 running 任务没有运行态记录，一并收尾", len(orphans))

        for task_id in orphans:
            if self._stopping or time.monotonic() > deadline:
                summary["left_alone"].append(task_id)
                continue
            if not _claim(self.run_dir, task_id, _NO_RECORD):
                log.info("任务 %s 正被另一个实例补扫，让开", task_id)
                summary["left_alone"].append(task_id)
                continue
            try:
                # 锁内复核两件事，缺一不可：
                #  1) 库里仍然是 running —— 期间可能已被用户中止或被上一条命收尾；
                #  2) 磁盘上仍然没有记录 —— 期间可能刚被另一个实例派活出去，
                #     那时它有 pid、要杀要验身份，不归这一路管（也不能去砸它）。
                if _db_status(task_id) != "running":
                    summary["left_alone"].append(task_id)
                    continue
                if _record_path(self.run_dir, task_id).exists():
                    log.info("任务 %s 刚被派出去且已有运行态记录，补扫让开", task_id)
                    summary["left_alone"].append(task_id)
                    continue
                self._mark_failed(
                    task_id,
                    "服务重启前未留下运行记录，无法确认计算进程是否还在，"
                    "已按中断处理；如需重跑请重新提交")
                store.add_event(task_id, "log", "system",
                                "服务重启时这个任务处于运行中，但磁盘上没有对应的运行态"
                                "记录（派活途中可能中断，或该任务早于运行记录机制）")
                summary["settled"].append(task_id)
                summary["no_record"] += 1
            except Exception:                         # noqa: BLE001 - 单条不带走整轮
                log.exception("补扫无记录任务 %s 失败", task_id)
                summary["errors"].append(task_id)
            finally:
                # **抢到就要还。** 认领锁靠 O_CREAT|O_EXCL 建，靠「删掉」释放；
                # 不删的话它会永久留在这里，而 run/ 不参与 janitor 清扫。
                # 这一路的 run_id 还是固定哨兵，于是残锁会把这个 task_id
                # **永远**挡在补扫之外——同一个任务第二次以同样的方式卡住时，
                # 就再也扫不掉了，正是这个功能要根治的症状换了个形式回来。
                _release_claim(_claim_path(self.run_dir, task_id, _NO_RECORD))

    def _recover_one(self, task_id: int, record: dict, summary: dict) -> None:
        """收尾一条遗留记录。判断顺序是有讲究的，从便宜到贵、从安全到动手。"""
        run_id = str(record.get("run_id") or "")
        # 1) 本实例正在跑它 → 不是遗留，别动自己的进程
        if task_id in self.running:
            summary["left_alone"].append(task_id)
            return
        # 2) 抢认领权：另一个调度器实例已经在收同一个 pid 就让开，避免两边各杀一次、
        #    各写一条"服务重启"事件
        claim = _claim_path(self.run_dir, task_id, run_id)
        if not _claim(self.run_dir, task_id, run_id):
            log.info("任务 %s 正被另一个调度器实例恢复，让开", task_id)
            summary["left_alone"].append(task_id)
            return
        try:
            # 3) 抢到锁之后**重读**记录：抢锁之前它可能已经被新一次派活覆盖了
            #    （run_id 变了），那时这个 pid 是新进程的，杀它等于砸掉正在算的任务。
            #    这与 claim_queued「抢不到就别写回 running」是同一个道理：动手之前
            #    必须在锁内复核一次。
            fresh = self._run_records()
            current = {tid: rec for tid, rec in fresh}.get(task_id)
            if current is None or str(current.get("run_id") or "") != run_id:
                log.info("任务 %s 的运行态记录已被新一轮派活覆盖，跳过恢复", task_id)
                summary["left_alone"].append(task_id)
                return
            record = current
            # 4) 库里已经不是 running → 别人（用户中止 / 上一条命自己收尾 / 别的实例）
            #    已经处理过了，**不许覆盖**，只把账销掉
            if _db_status(task_id) != "running":
                log.info("任务 %s 已不在 running（可能已被中止或收尾），只销记录", task_id)
                summary["left_alone"].append(task_id)
                return
            # 5) 验身份 → 杀整棵树 → 落终态
            killable, why = _verify_pid(record)
            pid = int(record.get("pid") or 0)
            cleaned = False
            if killable:
                _kill_tree_pid(pid)
                # 杀完**再问一次**还在不在：taskkill 可能失败或超时。没确认就说"已清理"
                # 是在骗用户——他以为 GPU 空出来了，其实还在烧。
                cleaned = _proc_fingerprint(pid) is None
                if cleaned:
                    summary["killed"].append(task_id)
                    log.warning("任务 %s：服务重启，杀掉残留进程树 %s", task_id, why)
                else:
                    log.warning("任务 %s：服务重启，%s，但进程树仍在运行，需人工确认",
                                task_id, why)
            else:
                log.warning("任务 %s：%s", task_id, why)
            self._mark_failed(task_id, _restart_reason(pid, cleaned))
            summary["settled"].append(task_id)
        finally:
            _release_claim(claim)
            self._drop_run_record(task_id)

    def _write_run_record(self, ap: AgentProc) -> None:
        """派活成功后落运行态记录。落不下去只警告——它只影响重启后的清理。"""
        fp = _proc_fingerprint(ap.pid) or ("", 0.0)
        rec = {
            "task_id": ap.task_id,
            # run_id 是这一轮派活的身份：新一轮派活会换一个新值，恢复时靠它认出
            # 「这条记录已经被人重写」。
            "run_id": uuid.uuid4().hex,
            "pid": ap.pid,
            "argv": [str(a) for a in ap.argv],
            "node_exe": str(self.node_exe),
            "image": fp[0],
            "start_epoch": fp[1],
            "started_at": ap.started_at,
            "work_dir": str(ap.work_dir),
        }
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            _record_path(self.run_dir, ap.task_id).write_text(
                json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log.warning("写运行态记录失败，任务 %s 的孤儿将来不会被自动清理：%s",
                        ap.task_id, e)

    def _drop_run_record(self, task_id: int) -> None:
        try:
            _record_path(self.run_dir, task_id).unlink()
        except OSError:
            pass                  # 本来就没有，不算事

    def stop(self) -> None:
        """停轮询并中止所有在跑的进程。服务退出时调，可重复调。"""
        self._stopping = True
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.poll_interval * 3 + 2.0)
        self._thread = None
        for task_id in list(self.running):
            try:
                self.abort(task_id)
            except Exception:                     # noqa: BLE001 - 停机不该因单个进程失败而卡住
                log.exception("停机时中止 %s 失败", task_id)
        # 归档线程是 daemon 的：不等它也不会挂住进程。但排队里的行**必须留给下一轮**
        # ——它们还没置 workflow_archived，闸门② 会让这个工作流停住等它们，正是要的行为。
        self._archive_stop()
        # 反思同理：没做完的那条没建记录，下次启动巡检会重新扫到。
        self._reflection_stop()

    def _loop(self) -> None:
        while not self._stopping:
            try:
                self.tick()
            except Exception:                     # noqa: BLE001 - 轮询线程不能死
                log.exception("调度器轮询出错")
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    # --- 队列 -------------------------------------------------------------------

    def tick(self) -> list:
        """扫一轮：收尾 → 常规 → 工作流放行 → 工作流归档。返回接走的 task_id。"""
        self._reap()
        admitted = self._admit()
        # 放行排在 _admit 之后：让路给常规任务是**顺序**带来的结构性结果，
        # 不需要额外查库。反过来的话一个 500 行的工作流会一次占满所有槽。
        try:
            admitted += self._workflow_run_pass()
        except Exception:                          # noqa: BLE001
            log.exception("工作流放行巡检出错")
        # 归档排在放行之后。**各自 try/except** 不是洁癖：_loop 是
        # `try: tick() except`，一次异常就让整轮 _admit 都没机会执行——
        # 用户会看到所有常规任务永远停在「排队中」，而原因只是一张 Excel 被占用。
        try:
            self._workflow_archive_pass()
        except Exception:                          # noqa: BLE001
            log.exception("工作流归档巡检出错")
        # 反思排在最后：它最慢也最不紧急（反馈闭环晚一轮无所谓，
        # 而它排在前面会让一次模型调用慢的巡检拖住整轮调度）。
        try:
            self._reflection_pass()
        except Exception:                          # noqa: BLE001
            log.exception("反思巡检出错")
        return admitted

    # --- 反思 ---------------------------------------------------------------------
    #
    # 巡检只做两件事：**查库**、**入队**。一次模型调用都不在这里发生。
    #
    # 为什么不挂在 _settle 上：终态不止一处会写。agent 正常收口走 packaging
    # 再由打包改成 done，用户中止、打包超时、janitor 清理写的都是 failed。
    # 只盯一个入口的后果很安静——另外几条路径收工的任务永远没有反思，
    # 而界面上看不出任何异常。

    def _reflection_pass(self) -> int:
        """扫一轮：找进终态但还没反思的任务，入队。返回入队条数。"""
        if self._stopping or self._refl_stopped:
            return 0
        try:
            rows = store.terminal_tasks_without_reflection(limit=REFLECTION_SWEEP_LIMIT)
        except Exception:                          # noqa: BLE001
            log.exception("读取待反思任务失败")
            return 0
        queued = 0
        for row in rows:
            tid = int(row["id"])
            with self._refl_lock:
                if tid in self._refl_inflight:
                    continue
                self._refl_inflight.add(tid)
            self._refl_q.put(tid)
            queued += 1
        if queued:
            self._reflection_ensure_thread()
        return queued

    def _reflection_ensure_thread(self) -> None:
        with self._refl_lock:
            if self._refl_stopped:
                return
            alive = [t for t in self._refl_threads if t.is_alive()]
            for i in range(len(alive), REFLECTION_WORKERS):
                t = threading.Thread(target=self._reflection_loop,
                                     name=f"reflect-{i}", daemon=True)
                t.start()
                alive.append(t)
            self._refl_threads = alive
            self._refl_thread = alive[0] if alive else None

    def _reflection_loop(self) -> None:
        """后台反思线程。**任何异常都不许逃出去**——同 _archive_loop 的理由。"""
        while True:
            try:
                task_id = self._refl_q.get(timeout=0.5)
            except queue.Empty:
                if self._refl_stopped:
                    return
                continue
            if task_id is None:                    # stop() 投的结束哨兵
                return
            try:
                rec = reflection.generate(task_id)
                log.info("任务 %s 的反思已生成（建议分类：%s）",
                         task_id, rec.get("suggested") or "模型没给，等管理员定")
            except Exception:                      # noqa: BLE001
                # generate() 自己已经把失败写进记录了。这里再吞一遍，
                # 是因为一条坏记录不该带走整条反思线程——下一条还得接着做。
                log.exception("生成任务 %s 的反思失败", task_id)
            finally:
                with self._refl_lock:
                    self._refl_inflight.discard(task_id)

    def _reflection_stop(self) -> None:
        """停机：投哨兵。**不等模型调用返回**——那是几十秒到几分钟的事，
        而派生的进程是 daemon 的，不等也不会挂住进程。没做完的那条任务下次
        启动时巡检会重新扫到（reflections 上还没有它的行）。"""
        with self._refl_lock:
            self._refl_stopped = True
            n = len(self._refl_threads)
        for _ in range(max(n, 1)):
            self._refl_q.put(None)

    def _reap(self) -> None:
        for ap in list(self.running.values()):
            # 用户中止发生在 public app，那是**另一个进程**：那边改得到数据库，改不到
            # 这里的进程。所以中止要靠库里的状态传导过来。
            #
            # 但**不能只看状态是不是 failed**：failed 是笼统终态，打包失败、agent 异常
            # 都会写它。靠它猜中止，结果是"算到一半崩了"被当成用户中止，_settle 写出
            # "任务已被中止"这种张冠李戴的原因（缺陷 B）。判据是进终态那条事件的
            # actor，见 _abort_signal 的三条理由。调度器自己 abort 的 ap.aborted 已置位，
            # 跳过以免重复收尾。
            if not ap.aborted:
                reason = _abort_signal(ap.task_id, _db_status(ap.task_id))
                if reason is not None:
                    self.abort(ap.task_id, reason=reason or ABORT_FALLBACK_REASON)
                    continue
            try:
                code = ap.proc.poll()
            except OSError:
                code = ap.exit_code
            if code is not None:
                self._settle(ap, exit_code=code)

    def _queued_tasks(self) -> list:
        try:
            rows = store.list_tasks(status="queued", is_admin=True, limit=200)
        except Exception:                          # noqa: BLE001 - 库不可达就这一轮不接
            log.exception("读取排队任务失败")
            return []
        # 工作流行任务有自己更严的闸门（规格 6.4），走常规通道会绕过它们——
        # 尤其闸门②（前一行的结果必须先归档）会被直接跳过。
        rows = [r for r in rows if r.get("workflow_id") is None]
        # store 是 id DESC（列表页要最新在前），队列要先进先出；priority 高 的插队
        rows.sort(key=lambda r: (-int(r.get("priority") or 0), int(r["id"])))
        return rows

    def _admit(self) -> list:
        admitted: list[int] = []
        if len(self.running) >= self.limit:
            return admitted
        for row in self._queued_tasks():
            if len(self.running) >= self.limit:
                break
            task_id = int(row["id"])
            if task_id in self.running:
                continue
            try:
                work_dir = guards.regions(task_id)[guards.WORK]
            except guards.Violation as e:
                self._mark_failed(task_id, f"工作区路径不合法：{e}")
                continue
            # 先抢再起。抢不到说明轮到自己之前它已经不在队列里了，最常见的原因是
            # 用户刚点了中止（public app 写 failed）——这时候绝不能再写回 running，
            # 否则中止被覆盖，任务白烧一轮 GPU 还显示"正在运行"。
            try:
                got = store.claim_queued(task_id)
            except Exception:                       # noqa: BLE001 - 这一轮先跳过
                log.exception("抢占任务 %s 失败", task_id)
                continue
            if got != "queued":
                continue
            try:
                self._spawn(task_id, row.get("brief", ""), work_dir)
            except Exception as e:                  # noqa: BLE001 - 起不来必须记账，不能悄悄留着
                log.exception("spawn %s 失败", task_id)
                self._mark_failed(task_id, f"计算进程启动失败：{e}")
                continue
            _notify()
            admitted.append(task_id)
        return admitted

    # --- 工作流 ---------------------------------------------------------------------

    def _workflow_run_pass(self) -> list[int]:
        """给每个 running 工作流放行**至多一行**。

        串行是结构性保证：同一 workflow 同时最多一个非终态行任务。所以这里
        一次只放一行，1000 行也不会把队列撑爆。
        """
        admitted: list[int] = []
        try:
            workflows = store.list_running_workflows()
        except Exception:                          # noqa: BLE001
            log.exception("读取 running 工作流失败")
            return admitted
        for wf in workflows:
            if len(self.running) >= self.limit:
                break
            try:
                task_id = self._workflow_admit_one(wf)
            except Exception:                      # noqa: BLE001 - 单个工作流失败不影响别的
                log.exception("工作流 %s 放行失败", wf.get("id"))
                continue
            if task_id:
                admitted.append(task_id)
        return admitted

    def _workflow_admit_one(self, wf: dict):
        """闸门 ①④⑤②③⑥，**顺序是规格的一部分**（规格 6.4）。

        ④ 必须排在②前面：重跑的行同时满足②（有未归档的行）和④（有 queued
        的行），②在前就把它挡死，而④永远走不到——整批卡死且不报错。
        """
        wid = int(wf["id"])
        if wf.get("status") != "running" or int(wf.get("abort_requested") or 0):
            return None
        # **放行侧也要读 workflow_skips**（规格 6.5 / §10）。原来只有闸门④ 间接
        # 看它，而④ 只管「管理员重跑产生的 queued 行」；按 cursor 放行的那条路
        # 整个文件都不读它——于是「跳过先于放行」时那一行照跑不误，白烧一次
        # 完整计算，而用户点它的用意正是「这行序列是坏的，别跑」。
        skips = store.list_workflow_skips(wid)

        # ④ 先认领 queued 的行任务（管理员重跑产生的唯一落点），认领完就返回。
        #    排这么前是因为它一旦被②盖住就永远走不到，而"重跑"只有这一条路。
        queued = store.workflow_queued_rows(wid)
        if queued:
            # 认领之前必须先确认**没有正在执行的行**。
            #
            # 闸门⑤（workflow_has_live_rows）把 queued 也算成活着的行，而它排在
            # ④ 后面——④ 要认领的恰恰就是 queued 行，所以⑤ 当不了④的前置，
            # 只能在这里单独判一次「有没有真的在跑的」。
            #
            # 少了这一句的后果：管理员一次重投多行 → 头一轮认领一行、下一轮又
            # 认领一行（上一行已经是 running，但④ 不看）→ **两行同时在跑**，
            # 结果表回写会交错；而工作流的契约是「一行跑完归档回表格才放下一行」。
            if store.workflow_has_running_rows(wid):
                return None
            row = queued[0]
            row_index = int(row["workflow_row"])
            if row_index in skips:
                # 跳过**晚于**放行：这一行已经有任务了（重跑把它退回 queued）。
                # 跳过的行不再跑一遍，直接置终态，交给归档 pass 写「已跳过」。
                store.set_status(int(row["id"]), "failed", actor="system",
                                 message="用户跳过，未运行")
                return None
            return self._spawn_workflow_row(wf, row_index, int(row["id"]))

        # ⑤ 串行：有非终态行任务就不放下一行。**故意不过滤 workflow_archived**——
        #    已经归档但尚未进终态的行（重跑后又被置回 queued 之外的状态）会同时
        #    过⑤、不过②③；加上过滤就会让同一个工作流的两行同时推进。
        if store.workflow_has_live_rows(wid):
            return None
        # ② 先归档再下一个：判据必须是 workflow_archived = 0（store 那侧已钉死），
        #    因为「上一行的结果还没写进结果表」就不能宣称这一批推进了。
        if store.workflow_has_unarchived(wid):
            return None
        # ③ 跑完了
        cursor = int(wf["cursor"])
        if cursor >= int(wf["total_rows"]):
            store.update_workflow(wid, status="finished")
            return None
        if cursor in skips:
            # 跳过**先于**放行：这一行还没有任务，照样不许建任务、照常烧一次计算。
            #
            # 但它**必须有**一条终态行任务：闸门② 等的是「每一行都不用再写了」，
            # 连任务都没有的行在它眼里根本不存在，而结果列也得有一格写着「已跳过」
            # （规格 6.5 / §10）。所以这里补一条**建出来就已经是终态、不排队**
            # 的任务——它从不存在 queued 状态，所以④ 也认领不到它，不存在
            # 「建完又被放行一次」的回头路。下一轮归档 pass 把它当普通终态行
            # 写回结果表，然后闸门② 解除，下一行照常放行。
            task = store.create_task(
                f"[工作流 {wf['name']}] 第 {cursor + 1} 行 · 已跳过",
                workflow_mod.render(wf["prompt_template"], {}),
                owner_id=int(wf["owner_id"]), actor="system",
                workflow_id=wid, workflow_row=cursor)
            store.set_status(int(task["id"]), "failed", actor="system",
                             message="用户跳过，未运行")
            store.add_event(int(task["id"]), "note", "system",
                            "用户跳过了这一行，没有运行计算。")
            store.update_workflow(wid, cursor=cursor + 1)
            return None
        # ⑥ 排在常规任务之后（顺序层之外再加一道可读、可测的契约）
        if store.user_has_live_regular_tasks(int(wf["owner_id"])):
            return None
        return self._spawn_workflow_row(wf, cursor, None)

    def _spawn_workflow_row(self, wf: dict, row_index: int,
                            existing_task_id):
        """渲染第 row_index 行的提示词，建/复用任务，放出去。

        existing_task_id 非空 = 认领重跑产生的既有任务，不新建。
        """
        wid = int(wf["id"])
        source = Path(wf["source_path"])
        cols = store.list_workflow_columns(wid)
        view = workflow_mod.read_source(source, header_row=int(wf["header_row"]))
        values = workflow_mod.row_values(source, view, row_index)
        slots: dict[str, str] = {}
        key_text = ""
        for col in cols:
            role = col["role"]
            text = values.get(int(col["col_index"]), "")
            if role == "key":
                key_text = text
            elif role in ("input", "context") and col.get("slot"):
                slots[col["slot"]] = text
        brief = workflow_mod.render(wf["prompt_template"], slots)
        title = f"[工作流 {wf['name']}] 第 {row_index + 1} 行" + (
            f" · {key_text[:60]}" if key_text else "")

        if existing_task_id:
            task_id = existing_task_id
        else:
            created = store.create_task(
                title, brief, owner_id=int(wf["owner_id"]), actor="system",
                workflow_id=wid, workflow_row=row_index)
            task_id = int(created["id"])
            store.add_event(task_id, "created", "system",
                            f"工作流「{wf['name']}」第 {row_index + 1} 行")
            # 放行的同一处语句里前移游标（规格 6.3）
            store.update_workflow(wid, cursor=row_index + 1)

        if int(task_id) in self.running:
            return int(task_id)
        try:
            work_dir = guards.regions(task_id)[guards.WORK]
        except guards.Violation as e:
            self._mark_failed(task_id, f"工作区路径不合法：{e}")
            return None
        try:
            got = store.claim_queued(task_id)
        except Exception:                          # noqa: BLE001
            log.exception("抢占行任务 %s 失败", task_id)
            return None
        if got != "queued":
            return None
        try:
            self._spawn(task_id, brief, work_dir)
        except Exception as e:                     # noqa: BLE001
            log.exception("spawn 行任务 %s 失败", task_id)
            self._mark_failed(task_id, f"计算进程启动失败：{e}")
            return None
        _notify()
        return int(task_id)

    # --- 归档 ---------------------------------------------------------------------

    def _workflow_archive_pass(self) -> list[int]:
        """把终态且未归档的行**入队**，回写本身在后台线程上做。返回入队的 task_id。

        这里**只查库、只入队**，一次 xlsx 都不碰：openpyxl 不支持增量写，单次
        write_back 是整表 load + 整表 save（宽表实测 2.2 秒），而 POLL_INTERVAL
        只有 1 秒——放在 tick 主路径里同步做，等于整个调度器每轮白等两秒。

        「逐行入队」不是省事写法（规格 7.2）：一批 5 行里第 3 行失败时，前两行
        必须已经各自置位，否则下一轮会把它们连同第 3 行一起重来。「整批统一置位」
        看着更整齐，实际是把一次失败放大成 N 次重写。

        取数范围是 **running + paused**（store.list_open_workflows）：中止与暂停
        都把 status 写成 paused，而被中止那一刻正在跑的那一行会变成**终态却
        未归档**——只扫 running 的话它永远排在取数范围之外，用户点完中止，
        已经算出来的那一行既不进表也下不到。
        """
        queued: list[int] = []
        self._archive_watchdog()
        try:
            workflows = store.list_open_workflows()
        except Exception:                          # noqa: BLE001
            log.exception("读取待归档工作流失败")
            return queued
        for wf in workflows:
            wid = int(wf["id"])
            try:
                rows = store.workflow_unarchived_terminal_rows(wid)
            except Exception:                      # noqa: BLE001
                log.exception("读取待归档行失败：工作流 %s", wid)
                continue
            for row in rows:
                task_id = int(row["id"])
                with self._archive_lock:
                    # **同工作流至多一行在途**（Major-2）：入队那一刻就掐住，
                    # 而不是在执行时才发现要排队。原因不是"少一点并发"，
                    # 是结果表只有一个文件——两条 write_back 同时进去，后写的
                    # 拿它更早读到的旧内容盖上去，前一行的值永久丢失，两行却
                    # 都 workflow_archived=1，**不报错也不再重试**。
                    # 顺带的好处：一个卡住的工作流只占一条线程，别的批次照跑。
                    if wid in self._archive_wf_busy:
                        break                      # 同一个工作流，后面的行等下一轮
                    # **在途去重**：这一行已经在队列里/正在写，就不再排一次。
                    # 只靠 store 的 workflow_archived = 0 是不够的——那一列要等
                    # 写完才置位，而整表重写中间有两秒。
                    if task_id in self._archive_inflight:
                        continue
                    self._archive_inflight.add(task_id)
                    self._archive_wf_busy.add(wid)
                    # **入队时刻**：看门狗靠它算「这一行卡了多久」。用
                    # monotonic 而不是 time.time()——系统时间被对时调回，
                    # time() 会算出负的耗时，告警就永远不触发。
                    self._archive_queued_at[task_id] = time.monotonic()
                    self._archive_warned.discard(task_id)
                    self._archive_q.put((wid, task_id))
                    queued.append(task_id)
            self._converge_if_aborted(wf)
        if queued:
            self._archive_ensure_thread()
        return queued

    def _converge_if_aborted(self, wf: dict) -> None:
        """中止之后，**自己**收敛到终态，不需要用户再点一次「继续」。

        中止把 status 置成 paused，而 ``finished`` 只在闸门③ 产生、闸门① 又
        只放行 running 的工作流——于是中止后**没有任何代码能推到终态**，
        唯一出路是用户手动点「继续」。这条死路已经实测过：连跑 5 轮 tick 之后
        仍是 ``status=paused``。

        判据：**abort_requested=1**（真的点过中止，不是普通暂停）且没有非终态
        行、也没有未归档的终态行。后两条意味着「所有已算出来的结果都已经落表」
        ——在归档真正完成之前就置 finished，用户看到的仍是「跑完了」而表是空的，
        那正是 C-1 那种静默丢数据的形状。

        **为什么复用 finished 而不改 CHECK**：workflows.status 的 CHECK 是
        ('draft','running','paused','finished')，只有 finished 一个终态（规格
        6.6 明确不新增 aborted 值，理由是 SQLite 改 CHECK 要重建表）。复用它
        不会误导用户：pause 与 abort 在本系统里的区别是**要不要把在跑的那行
        杀掉**，而不是「结局叫什么」；中止之后用户看到的是一份已经收到手的
        部分结果，语义上就是「这一批到此为止」，而不是「全部跑完了」——
        跑没跑完由 cursor/total_rows 如实显示。
        """
        try:
            wid = int(wf["id"])
            if wf.get("status") != "paused" or not int(wf.get("abort_requested") or 0):
                return
            if store.workflow_has_live_rows(wid):
                return
            if store.workflow_unarchived_terminal_rows(wid):
                return
            store.update_workflow(wid, status="finished")
            log.info("工作流 %s 中止后收敛到终态（无存活行、无未归档行）", wid)
        except Exception:                          # noqa: BLE001
            # 收敛失败绝不能把归档 pass 一起带走——下一轮还会再来一次。
            log.exception("工作流 %s 中止后收敛到终态失败", wf.get("id"))

    def _archive_ensure_thread(self) -> None:
        """把归档线程池补满到 :data:`ARCHIVE_WORKERS`。已停机就不再起。

        池子**下限 2**的理由见 :data:`ARCHIVE_WORKERS` 的注释：单线程下一条
        挂起的 write_back 就冻住整条队列。这里是「补满」而不是「用完再加」——
        池空着的时候正好是最需要它的时候。
        """
        with self._archive_lock:
            if self._archive_stopped:
                return
            alive = [t for t in self._archive_threads if t.is_alive()]
            for i in range(len(alive), ARCHIVE_WORKERS):
                t = threading.Thread(target=self._archive_loop,
                                     name=f"wf-archive-{i}", daemon=True)
                t.start()
                alive.append(t)
            self._archive_threads = alive
            self._archive_thread = alive[0] if alive else None

    def _archive_watchdog(self) -> None:
        """给「入队太久还没写完」的行记一条用户可见事件。**只告警，不重排。**

        不重排的理由同上：重排等于同一行可能被写两次，重复写的竞态比单行
        停住糟得多。这一行最终要么自己返回（于是这一轮 tick 之后重新入队
        重试），要么一直挂着——两种结局都不需要看门狗去替它写。

        告警**每行一次**（记在 ``_archive_warned`` 里）：漏了这个去重，看门狗
        本身就变成 I-1 说的那条事件风暴。
        """
        try:
            with self._archive_lock:
                now = time.monotonic()
                stuck = [tid for tid, t0 in self._archive_queued_at.items()
                         if tid not in self._archive_warned
                         and now - t0 >= self._archive_stuck_seconds]
                for tid in stuck:
                    self._archive_warned.add(tid)
            for tid in stuck:
                store.add_event(
                    tid, "note", "system",
                    f"这一行的结果回写卡住超过 {int(self._archive_stuck_seconds)} "
                    "秒还没返回——多半是结果表在网络盘上，或正被 Excel/WPS 独占。"
                    "平台不会重复写同一行（重复写会写坏结果表），所以请先关掉"
                    "占用它的程序；这一行自己返回之后会自动继续。")
                log.warning("归档行任务 %s 卡住超过 %ss，未重排", tid,
                            int(self._archive_stuck_seconds))
        except Exception:                          # noqa: BLE001
            # 看门狗自己出问题绝不许连累入队：那一轮还要照常排队。
            log.exception("归档看门狗出错")

    def _archive_loop(self) -> None:
        """后台归档线程。**任何异常都不许逃出去**：一个坏文件不该带走整条线程，
        更不该顺着它回到 tick。"""
        while True:
            try:
                job = self._archive_q.get(timeout=0.5)
            except queue.Empty:
                if self._archive_stopped:
                    return
                continue
            if job is None:                        # stop() 投的结束哨兵
                return
            wid, task_id = job
            try:
                self._archive_job(wid, task_id)
            except Exception:                      # noqa: BLE001
                log.exception("归档行任务 %s 失败", task_id)
            finally:
                with self._archive_lock:
                    self._archive_inflight.discard(task_id)
                    # 这一行收工了，它的工作流才允许再排下一行（入队时那道闸门）。
                    self._archive_wf_busy.discard(wid)
                    self._archive_queued_at.pop(task_id, None)
                    self._archive_warned.discard(task_id)
                try:
                    _notify()
                except Exception:                  # noqa: BLE001
                    log.exception("归档完成广播失败")

    def _archive_wf_lock(self, wid: int) -> threading.Lock:
        """这个工作流自己的归档锁。**按 workflow_id 分段**，不是一把全局锁。

        全局锁会把归档整体串成单线程——500 行的批次每行一次整表重写，全排成一条
        队就是十几分钟，而 ARCHIVE_WORKERS 的下限 2 正是为了不让一条卡住的
        write_back 把**所有工作流**一起停住。按工作流分段之后：同一工作流串行
        （结果表只有一个文件，必须），跨工作流照旧并发。

        取锁时**不持有** `_archive_lock`：否则一条正在写盘的归档线程会把
        `_archive_lock` 攥在手里去等写盘，而 tick 线程同时在 `_archive_lock`
        上排队（它每轮都要加锁入队），写盘那条收尾时又要这把锁——那是真的死锁。
        """
        with self._archive_lock:
            lock = self._archive_wf_locks.get(int(wid))
            if lock is None:
                lock = threading.Lock()
                self._archive_wf_locks[int(wid)] = lock
            return lock

    def _archive_job(self, wid: int, task_id: int) -> None:
        """执行一次归档。**先重读这一行**：入队到真正执行之间可能已被归档过
        （比如上一轮已经写完、这一轮才轮询到），重读是唯一的真相来源。

        整个执行体在**该工作流自己的锁**里（Major-2）：`_archive_one` 里是一次
        read-modify-write（load 结果表 → 改一格 → 整表 save），同一个 dest 上
        两条同时做就是互相覆盖，且**两行都会被置 workflow_archived=1**，于是丢
        掉的那一格永远补不回来。跨工作流各拿各的锁，吞吐不受影响。
        """
        with self._archive_wf_lock(wid):
            try:
                wf = store.get_workflow(wid)
                row = store.get_task(task_id)
            except Exception:                      # noqa: BLE001
                log.exception("重读待归档行 %s 失败", task_id)
                return
            if int(row.get("workflow_archived") or 0):
                return
            # **状态也必须重读校验**：入队时这一行是终态，可入队到真正执行之间
            # 它可能���管理员「重跑」放回 queued（status 变了、workflow_archived
            # 被清成 0）。这时候再归档，就会把**上一轮的结果**写回结果表并把
            # archived 置 1 —— 于是这一轮的新结果永远不写回表，而平台上看起来
            # 一切正常（规格 6.5 那一族）。
            # 真机踩过：2026-10-20 管理员重投第 1 行时，上一轮入队的归档 job
            # 正好在途，把这一行又归档了一次，result.xlsx 里留着上一轮的 failed。
            if row.get("status") not in db.TERMINAL_STATUSES:
                return
            self._archive_one(wf, row)
            try:
                # writeback_pending 的语义就是「还有没有终态没归档的行」。写成功一
                # 行不代表整批都补上了，所以这里重新查一次再清，而不是顺手清零。
                if not store.workflow_unarchived_terminal_rows(wid):
                    store.update_workflow(wid, writeback_pending=0)
            except Exception:                      # noqa: BLE001
                log.exception("校准工作流 %s 的 writeback_pending 失败", wid)

    def _archive_one(self, wf: dict, row: dict) -> None:
        """归档**一行**：写回结果表 + 置 workflow_archived。逐行，不是整批。"""
        wid = int(wf["id"])
        task_id = int(row["id"])
        fields = {
            "status": row.get("status", ""),
            "summary": row.get("summary", ""),
            "error": row.get("error", ""),
            # 复用既有的 store.artifacts.list（store.py:525），**不要**另造一个
            # list_artifacts——这个项目已经吃过十几次「同一件事写了两份，
            # 改了一份另一份还在跑」的亏。
            "artifacts": ", ".join(
                a["name"] for a in store.artifacts.list(task_id)
                if int(a.get("verified") or 0) == 1),
        }
        output_cols = [workflow_mod.OutputCol(int(c["col_index"]), c["field"])
                       for c in store.list_workflow_columns(wid)
                       if c["role"] == "output" and c.get("field")]
        # **回写开关（规格 2 决策表 / 7.1 / 9.1）**：writeback=0 时结果只留在
        # 平台内，不生成也不更新 result.xlsx。
        #
        # **关掉时照样要归档、照样要置 workflow_archived=1**——这里最容易踩：
        # 闸门② 等的是「每一行都不用再写了」，跳过置位的话开关一关整批就永远
        # 停在那一行，而且界面显示「运行中」但一行都不动。关掉开关改变的是
        # 「结果去哪」，不是「这一行处理完了没有」。
        if not output_cols or not int(wf.get("writeback") or 0):
            # 没有 output 列也算归档完成：闸门② 等的是「这一行不用再写了」。
            store.update_task(task_id, workflow_archived=1)
            return
        # 被跳过的行写「已跳过」而不是它的 status（规格 6.5 / §10）：
        # status 在这一行是 failed，读起来像「跑失败了」，而用户的本意是
        # 「这行序列是坏的，我故意没跑」——这两件事在结果表里必须分得开。
        #
        # 跳过原因写进 **summary**（结果说明列）而不是 error：跳过不是错误，
        # error 列留给真失败。用户看表时「已跳过 + 为什么」在一行里就读完了。
        if int(row["workflow_row"]) in store.list_workflow_skips(wid):
            reason = store.workflow_skip_reason(wid, int(row["workflow_row"]))
            fields["status"] = "已跳过"
            fields["summary"] = f"用户跳过：{reason}" if reason else "用户跳过"
            fields["error"] = ""
        try:
            self._write_back_row(wf, output_cols, fields, int(row["workflow_row"]))
        except (PermissionError, OSError) as e:
            # 文件被 Excel/WPS 独占、磁盘满、权限不足…… **不置 workflow_archived**。
            #
            # 置位了就再也没人查得到这一行：待归档行的取数带
            # `AND workflow_archived = 0`，writeback_pending 会永远挂着没人清。
            # 后果是用户开着 Excel 跑完整批，结果表里对应行是**空的**，而且
            # 不报错——用户以为跑完了，实际数据丢了。静默丢失比卡住糟得多。
            # 「卡住」是可接受的：下面那条 note 事件会告诉用户关掉 Excel。
            #
            # **这一行不置位，所以下一轮照样会被重新入队重试。** 去重只压掉
            # 重复的告警，压掉重试就是把上面那个「静默丢数据」原样装回来。
            #
            # 去重的必要性：文件长期被占（用户开着 Excel 吃顿饭）时这一行每轮
            # 都重新抛错，实测 **59 次/分**——时间线上灌进几万条一模一样的
            # note，写库 1305 条 SQL/分（×3 进程），真正的告警被淹在噪声里。
            #
            # 判据用 writeback_pending，而且**读库核对**：`wf` 是入队时的快照，
            # 可能过期；拿快照判断会把「早就置过位」当成没置，白写一遍去重副作用。
            try:
                pending = int(store.get_workflow(wid).get("writeback_pending") or 0)
            except Exception:                      # noqa: BLE001
                # 读不到就当没置位：宁可多写一条 note，也不要因为读库失败
                # 而让用户完全收不到「关掉 Excel」的提示。
                log.exception("读工作流 %s 的 writeback_pending 失败", wid)
                pending = 0
            if not pending:
                store.update_workflow(wid, writeback_pending=1)
                store.add_event(task_id, "note", "system",
                                "结果表被占用，这一行还没写回去。关掉 Excel/WPS 后"
                                "会自动重试，本工作流也会在归档完成前停住。")
            log.warning("工作流 %s 第 %s 行回写被拒（%s），留待重试", wid,
                        row.get("workflow_row"), e)
            return
        except store.Invalid as e:
            # 源表被改短/损坏：重试一万次也等不到。**必须置位**，否则闸门② 会
            # 永远等这一行，整批永久死锁。记一条 error 事件说清原因。
            store.add_event(task_id, "error", "system", f"归档失败：{e}")
            log.exception("工作流 %s 第 %s 行归档失败", wid, row.get("workflow_row"))
            store.update_task(task_id, workflow_archived=1)
            return
        store.update_task(task_id, workflow_archived=1)

    def _write_back_row(self, wf, output_cols, fields, row_index) -> None:
        source = Path(wf["source_path"])
        # `Path(wf["result_path"] or "")` 是 **Path(".")**，str 出来是 "." 而不是
        # 空串——用 `if not str(result)` 判会让"没配结果表"永远走不进默认分支，
        # 然后 dest.exists() 对一个目录为真、wb.save 撞上 ValueError。
        result = wf.get("result_path") or ""
        if not result:
            result = source.parent / "result.xlsx"
            store.update_workflow(int(wf["id"]), result_path=str(result))
        result = Path(result)
        if not result.exists():
            shutil.copyfile(source, result)
        jobs = [(row_index, oc, fields.get(oc.field, ""))
                for oc in output_cols]
        header_row = int(wf["header_row"])
        # **传 view.sheet，不传 wf["sheet"]**：行号是 read_source 算的，而它只读
        # 第一张表；wf["sheet"] 是用户填的。两者不一致时 write_back 抛
        # store.Invalid——静默写错行比报错糟得多（v1 只支持第一张表）。
        view = workflow_mod.read_source(source, header_row=header_row)
        workflow_mod.write_back(source, result, view.sheet, header_row, jobs)

    def _archive_wait(self, timeout: float = 30.0) -> bool:
        """等归档队列排空（测试与停机用）。返回是否在超时前排空。"""
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            with self._archive_lock:
                busy = bool(self._archive_inflight) or not self._archive_q.empty()
            if not busy:
                return True
            time.sleep(0.02)
        return False

    def _archive_stop(self) -> None:
        self._archive_stopped = True
        threads = list(self._archive_threads)
        # **每个 worker 一个结束哨兵**：只投一个的话，池里另一条线程要等
        # 0.5 秒的 get 超时才发现 _archive_stopped（那条兜底路径还在，
        # 但停机时没必要让它空转）。卡在 write_back 上的那条拿不到哨兵，
        # 由 join 的超时兜住——它是 daemon 的，不挂住进程退出。
        for _ in threads:
            self._archive_q.put(None)
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=self.poll_interval * 3 + 2.0)
        self._archive_threads = []
        self._archive_thread = None

    # --- spawn ------------------------------------------------------------------

    def _spawn(self, task_id: int, brief: str, work_dir) -> AgentProc:
        """起一个 agent 子进程并登记到 :attr:`running`。

        argv **只有** ``[node, run.mjs]``；四个变量全走环境变量。brief 也不走 argv，
        写进 ``work_dir/brief.md`` 再用 ``PROTFORGE_WEB_BRIEF_PATH`` 指路——需求描述
        最长两万字符，塞进命令行既超长又会进进程列表。
        """
        task_id = int(task_id)
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        brief = brief or ""
        brief_path = work_dir / "brief.md"
        try:
            brief_path.write_text(brief, encoding="utf-8")
        except OSError as e:
            raise DispatcherError(f"需求描述写入工作区失败：{e}")

        token = issue_agent_token(task_id)
        env = dict(os.environ)
        env[_INTERNAL_ENV] = self.internal_base
        env["PROTFORGE_TASK_ID"] = str(task_id)
        env["PROTFORGE_AGENT_TOKEN"] = token
        env["PROTFORGE_WORK_DIR"] = str(work_dir)
        env["PROTFORGE_WEB_BRIEF_PATH"] = str(brief_path)
        env["PROTFORGE_WEB_DATA_ROOT"] = str(self.data_root)
        # agent 侧的路径守卫要跟服务端用同一个 ProtForge 根，否则它读的是另一片 outputs
        env.setdefault("PROTFORGE_DATA", DEFAULT_PROTFORGE_DATA)
        # 计算进程这一整棵子树的 stdout/stderr 一律用 UTF-8。
        # Windows 上 Python 的 stdout 默认跟随控制台 code page（本机是 cp936），
        # 中文会被按 GBK 写出来；而协议层是 UTF-8（_pump_stdout 按 utf-8 解，
        # 写给 agent 的 stdin 也按 utf-8 编码），两边对不上，中文全变 U+FFFD。
        # node 自己不受影响（它的 stdout 恒为 UTF-8），但 agent 拉起的 Python
        # 技能脚本、以及测试里当 fake agent 的 Python 子进程都会中招。
        # 只设 PYTHONIOENCODING（stdout/stderr），不动 open() 的默认编码。
        env["PYTHONIOENCODING"] = "utf-8"

        # 模型配置跟着任务走：写在工作目录里，跟着任务建、跟着任务删。
        agent_dir, have_key = llm.materialize_model_config(work_dir)
        env["PROTFORGE_AGENT_DIR"] = str(agent_dir)
        env["PROTFORGE_LLM_PROVIDER"] = LLM_PROVIDER_ID
        env["PROTFORGE_LLM_MODEL"] = llm.model_id()
        if not have_key:
            # 不在这里就失败：agent 没有模型可用，但本地技能链也许还能干点别的，
            # 让它跑起来再由人看日志，比在调度器层硬拒更灵活。说清楚缺哪个变量。
            env["PROTFORGE_LLM_KEY_ENV"] = llm.key_env()
            log.warning("任务 %s：环境变量 %s 没值，agent 大概率发不出模型请求",
                        task_id, llm.key_env())

        argv = [self.node_exe, str(self.agent_script)]
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / f"agent-{task_id}.log"
        stderr_path = self.logs_dir / f"agent-{task_id}.err.log"
        stderr_fh = open(stderr_path, "a", encoding="utf-8")

        kwargs = {}
        if os.name == "nt":
            # 控制台窗口不该在服务运行期间一个一个冒出来
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            # 独立进程组，kill_tree 才能整组杀掉
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr_fh, env=env, cwd=str(self.agent_script.parent),
                bufsize=0, **kwargs,
            )
        except Exception:
            stderr_fh.close()
            revoke_agent_token(task_id)      # 没起来的进程不该留着一把能用的钥匙
            raise

        ap = AgentProc(
            task_id=task_id, pid=proc.pid, argv=argv, env=env, work_dir=work_dir,
            token=token, log_path=log_path, stderr_path=stderr_path,
            started_at=db.now_iso(), brief_path=brief_path, proc=proc, stderr_fh=stderr_fh,
        )
        with self._lock:
            self._running[task_id] = ap
        # 立刻落运行态记录：进程一旦起来，它就可能活过本进程，必须留凭据给恢复用
        self._write_run_record(ap)
        ap.reader = threading.Thread(target=self._pump_stdout, args=(ap,),
                                     name=f"agent-stdout-{task_id}", daemon=True)
        ap.reader.start()
        try:
            store.add_event(task_id, "log", "system",
                            f"计算进程已启动 pid={proc.pid}，stdout 见 {log_path.name}")
            _notify()
        except store.StoreError:
            # 任务已被删/不存在：进程照跑（_spawn 允许无任务直接起，测试这么用），
            # 只是没地方记账。不打异常栈——这是预期路径，不是故障。
            log.debug("任务 %s 不在库里，跳过启动事件", task_id)
        except Exception:                         # noqa: BLE001
            log.exception("记录 %s 的启动事件失败", task_id)
        self._dispatch_prompt(ap, brief)
        return ap

    @staticmethod
    def _dispatch_prompt(ap: AgentProc, brief: str) -> None:
        """把需求作为第一条 prompt 发进 agent 的 stdin。**没有它任务永远不会开始。**

        run.mjs 跑的是 headless 的 ``runRpcMode``：agent 起来、加载完技能、进入
        RPC 模式，然后就等着 stdin 上的第一条命令。需求虽然写进了 brief.md 并用
        环境变量指了路，但没人把它发过去——进程活着、心跳在跳、状态永远停在
        「正在准备环境」，不报错也不退出。

        用 ``sendline`` 而不是 :meth:`rpc`：派活**不等回应**。模型的第一轮思考可能
        要几十秒到几分钟，而 rpc 的等待窗口是给管理员那种"问一句很快就有回音"的
        命令设计的。回应由 stdout 泵照常分流，不走等待队列。
        """
        text = (brief or "").strip()
        if not text:
            text = "请阅读工作目录下的 brief.md，按里面的要求完成计算。"

        # 经验块放在**需求前面**：它是背景条件，不是补充说明。放在后面的话，
        # 长需求会把前面的内容挤出注意力——而这批经验正是为了在长需求里被想起。
        # 一条都没有时**不加这段**：给模型一个「## 已沉淀的经验（暂无）」的空壳，
        # 只会让它以为这里本来就没什么经验可参考，而实际上还没沉淀过。
        lessons = reflection.lessons_block()
        if lessons:
            text = ("## 已沉淀的经验\n"
                    "下面这些是过去任务复盘后由管理员归档下来的做法。**与本次需求冲突时"
                    "以需求为准**，不冲突时优先照着做。\n\n"
                    f"{lessons}\n\n---\n\n## 本次需求\n{text}")

        # 先换模型再派活。**必须换**：不换的话 pi 会用内置 catalog 里的默认模型
        # （本机是 minimax/MiniMax-M2.7，指向 api.minimaxi.com），拿本平台的
        # api.minimax.cn 密钥去打它必然 401，而界面上只看得见 step 停在
        # 「正在准备环境」。set_model 是 headless 模式里被支持的换模型途径，
        # 而且带回应，成功与否是能验的。
        lines = [{
            "id": uuid.uuid4().hex,
            "type": "set_model",
            "provider": LLM_PROVIDER_ID,
            "modelId": llm.model_id(),
        }, {
            "id": uuid.uuid4().hex,
            "type": "prompt",
            "message": text,
        }]
        # 需求送不出去有两种**完全不同**的原因，混在一起会把一次正常收口说成事故：
        #   · 进程已经自己退了（启动即崩、或活得太短没接到活）。prompt 本来就无处可发，
        #     真相在退出码和 stdout/stderr 里，收尾路径会把它们如实记下来——由它去解释。
        #   · 进程还活着，可管道/句柄写不进去。这才是「起废了」，该报启动失败。
        # Windows 上子进程一退，父进程往那条管道写拿到的是 EINVAL 而不是 EPIPE，
        # 认不出异常类型，只能靠 poll() 判活。两次都判：poll 在前能挡掉绝大多数，
        # 异常后再判一次，挡住「判活之后、写之前」那一瞬退掉的窄缝。
        if ap.proc.poll() is not None:
            log.info("计算进程 %s 在接到需求前已退出（退出码 %s），交给收尾路径解释",
                     ap.task_id, ap.proc.returncode)
            return
        try:
            for payload in lines:
                ap.proc.stdin.write(
                    (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            ap.proc.stdin.flush()
        except Exception:                         # noqa: BLE001 - 派不出活就是起废了
            if ap.proc.poll() is not None:
                log.info("需求写进 %s 时进程已退出（退出码 %s），交给收尾路径解释",
                         ap.task_id, ap.proc.returncode)
                return
            log.exception("把需求发给计算进程 %s 失败", ap.task_id)
            raise DispatcherError(f"无法把需求发给计算进程 {ap.task_id}")
        _notify()

    # --- stdout 泵 ---------------------------------------------------------------

    def _pump_stdout(self, ap: AgentProc) -> None:
        """把 stdout 的 JSON 行分流：响应交给等它的 rpc，其余进事件流。

        一个 agent 只有一条 stdout，响应和事件共用，所以必须分一次流；不分的话
        管理员发一次 steer 就会被 agent 的进度事件冲掉响应。
        """
        try:
            while True:
                raw = ap.proc.stdout.readline(MAX_LINE)
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                _append_log(ap.log_path, line)
                try:
                    obj = json.loads(line)
                except ValueError:
                    obj = {"type": "log", "message": line}
                if not isinstance(obj, dict):
                    obj = {"type": "log", "message": line}
                if _is_response(obj):
                    self._deliver_response(ap, obj)
                else:
                    self._push_event(ap, obj)
                self._note_agent_output(ap, obj)
        except (OSError, ValueError) as e:
            log.warning("读取 %s 的 stdout 结束：%s", ap.task_id, e)
        finally:
            with ap.cond:
                ap.eof = True
                ap.cond.notify_all()

    def _note_agent_output(self, ap: AgentProc, obj: dict) -> None:
        """从 stdout 事件里记住两件事：**模型说了什么**、**它自己结束了**。

        ① 模型最后一段纯文本：agent 自己结束而没调 finish 时，这就是唯一的
        结论（见 :meth:`_on_agent_settled`）。只取纯文本段落：工具调用参数
        里的 JSON 不是给人看的话。

        ② ``agent_settled``：这一轮模型**自己结束**了。正常收口时 agent 会先
        调 finish（那时 finished 标记已经落库），所以只有在「没有收口标记」
        的情况下它才意味着「模型答完就停了」。
        """
        etype = obj.get("type")
        if etype == "message_end":
            msg = obj.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                parts = msg.get("content")
                text = "".join(
                    str(part.get("text") or "")
                    for part in (parts if isinstance(parts, list) else [])
                    if isinstance(part, dict) and part.get("type") == "text"
                ).strip()
                if text:
                    ap.last_reply = text
        elif etype == "agent_settled":
            self._on_agent_settled(ap)

    def _on_agent_settled(self, ap: AgentProc) -> None:
        """agent 自己把这一轮**结束**了，却没报收口。

        这是真机上抓到的一条卡死：模型答完话就停在那里，不调 ``finish``
        （实测 stdout：只回了一句 "2"，一个工具调用都没有），于是：

        * 进程还挂在 ``runRpcMode`` 上等 stdin → 任务永远停在「计算中」，
          直到技能超时（4 小时）才被了结；
        * 管理员这时候**干预不了**：steer / follow_up 只是进了队列——那一轮
          已经结束，pi 不会再发起下一次模型调用，队列里的消息没人取
          （实测：只有 abort 有用）。

        也就是说「模型忘了调 finish」这一件事，会把一个任务变成一个既跑不完
        又管不了的僵尸。收口这件事不能整个押在模型记不记得调工具上。

        做法：看见 ``agent_settled`` 而任务**没有** finished 标记时，平台自己
        按「已收口」处理（记标记 + 收进程），让它走正常的收尾分流——有产物
        就交给打包（打包器对「靠时间窗兜底发现」的产物本来就不自动放行），
        没产物就得到一句「计算结束了，但没有找到任何产物」。

        **已经报过收口的（正常路径）什么都不做**：那条路已经收口了。
        **中止过的也不动**：中止的结局由中止那条路决定。
        """
        if ap.settled or ap.aborted:
            return
        task_id = ap.task_id
        try:
            if store.has_finished(task_id):
                return                          # 正常收口：finish 那条路已经处理了
            reply = (ap.last_reply or "").strip()
            store.add_event(
                task_id, "note", "system",
                "计算进程自己结束了，没有报收口（没调 finish）："
                "平台按已收口处理，接着打包产物"
                + (f"；模型最后的回复：{reply[:200]}" if reply else ""))
            if reply:
                # 摘要是给人看的一句话：模型答了什么，这里就写什么。
                # 不写的话这条任务只剩一句「没报收口」，用户连它答了什么
                # 都不知道——而那可能正是他要的答案。
                store.update_task(task_id, summary=reply[:2000])
            store.record_finished(task_id, actor="system")
        except Exception:                     # noqa: BLE001
            log.exception("处理 %s 的自行收口时出错", task_id)
            return
        # 收进程：关 stdin → agent 看到 EOF 退出 → 走正常的收尾分流。
        self.wind_down(task_id)

    def _deliver_response(self, ap: AgentProc, obj: dict) -> None:
        with ap.cond:
            waiter = ap.responses.get(obj.get("id"))
        if waiter is not None:
            try:
                waiter.put_nowait(obj)
            except queue.Full:
                pass
            return
        # 没人等的响应别静默吞掉：扔进事件流，管理员一眼看得见是谁多回了东西
        self._push_event(ap, obj)

    def _push_event(self, ap: AgentProc, obj: dict) -> None:
        with ap.cond:
            ap.seq += 1
            obj.setdefault("task_id", ap.task_id)
            obj["seq"] = ap.seq
            ap.events.append(obj)
            while len(ap.events) > MAX_EVENTS:
                ap.events.popleft()
                ap.dropped += 1
            ap.cond.notify_all()

    # --- RPC 与事件流 -----------------------------------------------------------

    def _require(self, task_id) -> AgentProc:
        ap = self.running.get(int(task_id))
        if ap is None:
            raise KeyError(f"没有在跑的计算进程：{task_id}")
        return ap

    def rpc(self, task_id: int, command: dict, timeout=None) -> dict:
        """写一行 JSON 命令进 stdin，等回一行 JSON 响应。

        发出去的信封是 ``命令原样 + {"id": ..., "task_id": ...}``：``id`` 用来对响应，
        其余原样透传，agent 侧不用再解一层。
        """
        task_id = int(task_id)
        ap = self._require(task_id)
        if ap.proc.poll() is not None:
            raise AgentGone(f"计算进程 {task_id} 已退出（退出码 {ap.proc.poll()}）")

        mid = uuid.uuid4().hex
        payload = dict(command or {})
        payload["id"] = mid
        payload["task_id"] = task_id
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with ap.cond:
            ap.responses[mid] = waiter
            ap.last_rpc_id = mid
        try:
            ap.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            ap.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            with ap.cond:
                ap.responses.pop(mid, None)
            raise AgentGone(f"计算进程 {task_id} 的 stdin 已关闭：{e}")

        wait = float(timeout) if timeout else self.rpc_timeout
        try:
            resp = waiter.get(timeout=wait)
        except queue.Empty:
            raise RpcTimeout(f"计算进程 {task_id} 在 {wait:.0f} 秒内没有响应")
        finally:
            with ap.cond:
                ap.responses.pop(mid, None)
        if resp is _DEAD:
            raise AgentGone(f"计算进程 {task_id} 在响应到达前已退出")
        if not isinstance(resp, dict):
            raise DispatcherError(f"计算进程 {task_id} 的响应不是 JSON 对象：{resp!r}")
        return resp

    def stream(self, task_id: int, after=None, timeout=None):
        """产出 stdout 里的事件流（响应已被分流走，这里只有事件）。

        :param after: 游标，只产 ``seq`` 更大的；SSE 重连时传上次最后一条的 seq
        :param timeout: 整体等待上限秒数；``None`` 表示一直等到进程退出
        """
        ap = self._require(task_id)
        cursor = int(after or 0)
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            batch = []
            with ap.cond:
                while True:
                    batch = [e for e in ap.events if e["seq"] > cursor]
                    if batch or ap.eof:
                        break
                    ap.cond.wait(timeout=0.2)
                    if deadline is not None and time.monotonic() > deadline:
                        break
            if batch:
                cursor = batch[-1]["seq"]
                for obj in batch:
                    yield obj
                continue
            with ap.cond:
                if ap.eof and not any(e["seq"] > cursor for e in ap.events):
                    return              # 读完且缓冲排空，正常收尾
            if deadline is not None and time.monotonic() > deadline:
                return

    # --- 中止与收尾 -------------------------------------------------------------

    def abort(self, task_id: int, reason: str | None = None) -> bool:
        """中止一个任务。返回是否真的做了动作。

        在跑的：杀整棵进程树再收尾（记为「已中止」）。还在排队的：直接取消，
        否则它下一轮 tick 又会被接走，白起一次进程。
        """
        task_id = int(task_id)
        ap = self.running.get(task_id)
        if ap is None:
            try:
                task = store.get_task(task_id, is_admin=True)
            except store.StoreError:
                return False
            if task.get("status") == "queued":
                self._mark_failed(task_id, reason or "任务在排队中被中止")
                return True
            return False

        ap.aborted = True
        ap.abort_reason = reason or ""
        kill_tree(ap.proc)
        try:
            ap.proc.wait(timeout=KILL_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("中止 %s 后进程 %s 仍在运行", task_id, ap.pid)
        self._settle(ap)
        return True

    def wind_down(self, task_id: int) -> bool:
        """agent 报完 finish 之后，让这个进程**自己走到退出**。返回是否动了手。

        为什么必须有这一步
        ----------------
        收尾（:meth:`_settle`）只在**进程退出**时触发，而 ``finish`` 只是模型调用的
        一个工具：它 POST 完就返回给模型，进程继续挂在 ``runRpcMode`` 上等 stdin。
        于是形成一个死锁——

            agent 调 finish → 标记落库 → 没人关 stdin → 进程不退 → 收尾不跑
            → 状态永远停在「计算中」→ 不进打包 → **用户永远下载不到任何东西**

        这条链上每一环单独看都对，串起来才致命，而且端到端跑一次才会现形。

        做法：**关 stdin**（agent 的 RPC 循环读到 EOF 就该返回、``main()`` 结束、
        node 正常退出），给 :data:`WIND_DOWN_GRACE` 的宽限；还不退就杀整棵进程树。

        刻意**不置** ``ap.aborted``：收尾分流的第一条判据是 ``has_finished``，
        把它标成中止会让一次正常收口被写成「用户中止了任务」。

        整个动作丢到后台线程：/finish 的调用方就是 agent 自己，堵在请求里
        等它退出等于让它等自己。
        """
        task_id = int(task_id)
        ap = self.running.get(task_id)
        if ap is None or ap.settled or ap.aborted:
            return False
        threading.Thread(target=self._wind_down_worker, args=(ap,),
                         name=f"winddown-{task_id}", daemon=True).start()
        return True

    def _wind_down_worker(self, ap: AgentProc) -> None:
        try:
            if ap.proc.stdin is not None:
                try:
                    ap.proc.stdin.close()
                except OSError:
                    pass
            try:
                ap.proc.wait(timeout=WIND_DOWN_GRACE)
            except subprocess.TimeoutExpired:
                log.warning("任务 %s 报完收口后进程 %s 仍在运行，杀掉",
                            ap.task_id, ap.pid)
                kill_tree(ap.proc)
                try:
                    ap.proc.wait(timeout=KILL_TIMEOUT)
                except subprocess.TimeoutExpired:
                    log.error("任务 %s 的进程 %s 杀了也没退", ap.task_id, ap.pid)
        except Exception:                             # noqa: BLE001 - 后台线程不能死
            log.exception("收口 %s 时出错", ap.task_id)
        finally:
            self._settle(ap)

    def _close_handles(self, ap: AgentProc) -> None:
        """收尾：关管道、唤醒还在等响应的调用方。"""
        try:
            if ap.proc.stdin is not None:
                ap.proc.stdin.close()          # 先关 stdin，让 agent 自己看到 EOF 退出
        except OSError:
            pass
        if ap.reader is not None and ap.reader.is_alive():
            ap.reader.join(timeout=READER_JOIN_TIMEOUT)
        for stream in (ap.proc.stdout,):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            if ap.stderr_fh is not None:
                ap.stderr_fh.close()
        except OSError:
            pass
        with ap.cond:
            for waiter in list(ap.responses.values()):
                try:
                    waiter.put_nowait(_DEAD)
                except queue.Full:
                    pass
            ap.responses.clear()
            ap.eof = True
            ap.cond.notify_all()

    def _settle(self, ap: AgentProc, exit_code=None) -> int | None:
        """进程退出后的分流。幂等：重复调用不会插第二条事件。

        **有** ``finished`` 标记 → 正常收口，进 ``packaging`` 交给打包；
        **没有** → 异常结束，进 ``failed``。中止过的也走 failed，除非它已经收口过
        （算完了就别扔结果）。
        """
        with self._lock:
            if ap.settled:
                return ap.exit_code
            ap.settled = True
            self._running.pop(ap.task_id, None)

        code = ap.proc.poll() if exit_code is None else exit_code
        ap.exit_code = code
        revoke_agent_token(ap.task_id)
        # 收尾是唯一的"这个 pid 已经归我处理完了"信号：记录留着的话，下次启动会把
        # 一个早就退出的进程当成孤儿来追（更糟的是 pid 已经被复用，见 _verify_pid）
        self._drop_run_record(ap.task_id)
        self._close_handles(ap)

        try:
            task = store.get_task(ap.task_id, is_admin=True)   # 确认任务还在库里
        except store.StoreError:
            return code                                    # 任务被删了，没什么可收尾的
        except Exception:                                 # noqa: BLE001
            log.exception("收尾 %s 时读任务失败", ap.task_id)
            return code

        # 收尾要同时照顾两个**互相独立**的事实：
        #   ap.aborted —— 进程是我们杀的（该如实记「已杀掉进程树」）
        #   has_finished —— agent 自己报过收口（该走 packaging）
        # 两者可以同时成立：agent 调完 /finish，用户在调度器收尾之前点了中止。
        # 这时候既不该翻回「打包中」，也不该说成「计算进程异常结束」——
        # 一次正常收口被解释成崩溃，用户在详情页看到的是一条不存在的事故。
        # 所以四条分支是**互斥**的，不是一条 if/elif 链。
        #
        # store 层已经规定「system/agent 的自动回写不许离开终态」，这里是调用方
        # 自己的第一道：不试比试了再被拒要有意义。
        already_terminal = task.get("status") in db.TERMINAL_STATUSES
        has_finished = store.has_finished(ap.task_id)

        # **任务可能在收尾这一刻被删掉**（用户删任务，收尾线程还在写）。
        # 那时抛出来的 NotFound 会变成一条**没人处理的线程异常**：报出来的是
        # 「任务不存在」，跟真正的原因（任务确实被删了）不相干，而收尾线程
        # 就此死掉，后面该做的事全落空。所以每条写库的路都自己兜住。
        if has_finished and not already_terminal:
            try:
                store.set_status(ap.task_id, "packaging", actor="agent",
                                 message="计算进程已收口，开始打包产物")
                store.add_event(ap.task_id, "step", "system",
                                f"计算进程正常退出（退出码 {code}）")
            except store.NotFound:
                return code                        # 任务没了，没什么可收尾的
            except Exception:                     # noqa: BLE001
                log.exception("把任务 %s 推进 packaging 失败", ap.task_id)
            _notify()
        elif ap.aborted:
            # 进程确实被杀过，这是事实，跟收口标记没关系，照实记。
            self._mark_failed(ap.task_id, ap.abort_reason or "已由管理员中止")
            try:
                store.add_event(ap.task_id, "log", "admin",
                                f"中止：已杀掉进程树 pid={ap.pid}，stderr 见 {ap.stderr_path.name}")
            except store.NotFound:
                return code
            except Exception:                     # noqa: BLE001
                log.exception("记录任务 %s 的中止事件失败", ap.task_id)
            _notify()
        elif already_terminal:
            # 没有中止也没有崩，任务却已是终态（管理员判失败之类）。
            # 只如实记一句，**不要替没发生过的事编一个原因**，也不动 error 字段。
            log.info("任务 %s 收尾时已是终态（%s）且已收口，不改写", ap.task_id,
                     task.get("status"))
            try:
                store.add_event(ap.task_id, "log", "system",
                                f"计算进程已收口，但任务此前已进入终态"
                                f"（{task.get('status')}），保持不变")
            except store.NotFound:
                return code
            except Exception:                     # noqa: BLE001
                log.exception("记录任务 %s 的收尾事件失败", ap.task_id)
            _notify()
        else:
            self._mark_failed(
                ap.task_id,
                f"计算进程异常结束（退出码 {code}），未收到收口标记；stderr 见 {ap.stderr_path.name}")
        return code

    def _mark_failed(self, task_id: int, reason: str) -> None:
        """标失败 = 写 error 字段 + 状态事件 + 一条 error 事件。

        三样都要：``error`` 字段给详情页，状态事件让流转轨动格，``error`` 事件让
        时间线上直接看得见失败原因（管理员翻时间线时不该去猜字段）。

        **已经是 failed 的不重写。** 用户中止与调度器收尾会先后碰到同一个任务，
        谁后到谁覆盖的话，用户明明点的是中止，最后看到的却是"计算进程异常结束
        （退出码 -1）"。拦住的是状态与 error 字段的覆盖；"已杀掉进程树"那条事件
        由 :meth:`_settle` 单独补，用户仍然看得到进程确实被杀了。
        """
        try:
            task = store.get_task(task_id, is_admin=True)
        except store.StoreError:
            return                                 # 任务被删了，没什么可标记的
        except Exception:                         # noqa: BLE001
            log.exception("读任务 %s 失败，跳过标记", task_id)
            return
        if task.get("status") == "failed":
            return
        try:
            store.update_task(task_id, error=reason)
            store.set_status(task_id, "failed", actor="system", message=reason)
            store.add_event(task_id, "error", "system", reason)
            _notify()
        except Exception:                                 # noqa: BLE001 - 收尾失败只记日志
            log.exception("把 %s 标记为 failed 失败", task_id)


__all__ = [
    "Dispatcher",
    "AgentProc",
    "DispatcherError",
    "AgentGone",
    "RpcTimeout",
    "issue_agent_token",
    "verify_agent_token",
    "revoke_agent_token",
    "agent_token_task",
    "probe_gpu_count",
    "kill_tree",
    "DEFAULT_INTERNAL_BASE",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_AGENT_SCRIPT",
    "AGENT_TOKEN_TTL",
    "MAX_EVENTS",
    "MAX_LINE",
]
