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
import hmac
import json
import logging
import os
import queue
import secrets
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import db
import guards
import sse
import store

log = logging.getLogger(__name__)

# --- 常量 ----------------------------------------------------------------------------

DEFAULT_INTERNAL_BASE = "http://127.0.0.1:8475"     # 内部 API 端口（plan Global Constraints）
DEFAULT_AGENT_SCRIPT = Path(__file__).resolve().parent.parent / "agent" / "run.mjs"
DEFAULT_PROTFORGE_DATA = r"../protforge/data"            # 与 guards.DEFAULT_PROTFORGE_ROOT 同源

#: 探测不到 GPU 时的兜底并发数。**有下限不是默认值**：宁可一次只跑一个，也不要
#: 因为 nvidia-smi 不在 PATH 就把平台判死。
DEFAULT_MAX_CONCURRENCY = 1

AGENT_TOKEN_TTL = 12 * 60 * 60                     # 令牌最长活 12 小时（兜底，正常几小时就退出）
RPC_TIMEOUT = 30.0                                 # 管理员 steer 等响应的默认上限
POLL_INTERVAL = 1.0                                # 队列轮询 + 退出检测的间隔
GPU_PROBE_TIMEOUT = 10.0
KILL_TIMEOUT = 15.0
READER_JOIN_TIMEOUT = 5.0
MAX_LINE = 1 << 20                                 # 单行 stdout 上限 1 MB，防 agent 吐大 blob 撑爆内存
MAX_EVENTS = 5000                                  # 事件流缓冲上限，溢出丢最旧并计数

_INTERNAL_ENV = "PROTFORGE_WEB_INTERNAL"
_MAX_CONCURRENCY_ENV = "PROTFORGE_WEB_MAX_CONCURRENCY"
_NODE_ENV = "PROTFORGE_WEB_NODE"
_AGENT_SCRIPT_ENV = "PROTFORGE_WEB_AGENT_SCRIPT"

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


def kill_tree(proc: subprocess.Popen, timeout: float = KILL_TIMEOUT) -> int | None:
    """杀掉进程**整棵树**，返回 taskkill 的退出码（拿不到返回 None）。

    Windows 上 ``proc.terminate()`` 只杀直接子进程：agent 起的 ``docker exec`` /
    ``wsl.exe`` 会脱离出去继续烧 GPU，而且没人再管它。``taskkill /T /F`` 才是
    「连子孙一起杀」，``/F`` 是为了不让它有机会留下半截输出文件。
    """
    pid = proc.pid
    if os.name == "nt":
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        try:
            out = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                                 capture_output=True, text=True, timeout=timeout, **kwargs)
            return out.returncode
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("taskkill %s 失败，退回 terminate：%s", pid, e)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return 0
        except (OSError, AttributeError) as e:
            log.warning("killpg %s 失败，退回 terminate：%s", pid, e)
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
    settled: bool = False                    # 已被收尾（幂等闸）
    exit_code: int | None = None
    eof: bool = False                        # stdout 读完
    seq: int = 0                             # 事件自增序号（stream 的游标）
    dropped: int = 0                         # 事件缓冲溢出丢弃数
    last_rpc_id: str | None = None

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
        self.gpu_count = max(0, int((gpu_probe or probe_gpu_count)()))
        self.limit = self._resolve_limit(max_concurrency)

        self._lock = threading.RLock()
        self._running: dict[int, AgentProc] = {}
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stopping = False

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
        """起后台线程轮询队列。重复调用是空操作。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop, name="dispatcher", daemon=True)
            self._thread.start()
        log.info("调度器启动：并发上限 %s（探测到 %s 张 GPU）", self.limit, self.gpu_count)

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
        """扫一轮：先收尾已退出的进程，再按并发上限接走排队任务。返回接走的 task_id。"""
        self._reap()
        return self._admit()

    def _reap(self) -> None:
        for ap in list(self.running.values()):
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
            try:
                self._spawn(task_id, row.get("brief", ""), work_dir)
            except Exception as e:                  # noqa: BLE001 - 起不来必须记账，不能悄悄留着
                log.exception("spawn %s 失败", task_id)
                self._mark_failed(task_id, f"计算进程启动失败：{e}")
                continue
            try:
                store.set_status(task_id, "running", actor="system", message="计算进程已启动")
            except Exception:                       # noqa: BLE001
                log.exception("把 %s 置为 running 失败，杀掉刚起的进程", task_id)
                self.abort(task_id, reason="状态写入失败，已回滚")
                continue
            _notify()
            admitted.append(task_id)
        return admitted

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
        return ap

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
        except (OSError, ValueError) as e:
            log.warning("读取 %s 的 stdout 结束：%s", ap.task_id, e)
        finally:
            with ap.cond:
                ap.eof = True
                ap.cond.notify_all()

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
        kill_tree(ap.proc)
        try:
            ap.proc.wait(timeout=KILL_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("中止 %s 后进程 %s 仍在运行", task_id, ap.pid)
        self._settle(ap)
        return True

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
        self._close_handles(ap)

        try:
            store.get_task(ap.task_id, is_admin=True)     # 确认任务还在库里
        except store.StoreError:
            return code                                    # 任务被删了，没什么可收尾的
        except Exception:                                 # noqa: BLE001
            log.exception("收尾 %s 时读任务失败", ap.task_id)
            return code

        if store.has_finished(ap.task_id):
            store.set_status(ap.task_id, "packaging", actor="agent",
                             message="计算进程已收口，开始打包产物")
            store.add_event(ap.task_id, "step", "system",
                            f"计算进程正常退出（退出码 {code}）")
            _notify()
        elif ap.aborted:
            self._mark_failed(ap.task_id, "已由管理员中止")
            store.add_event(ap.task_id, "log", "admin",
                            f"中止：已杀掉进程树 pid={ap.pid}，stderr 见 {ap.stderr_path.name}")
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
        """
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
    "probe_gpu_count",
    "kill_tree",
    "DEFAULT_INTERNAL_BASE",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_AGENT_SCRIPT",
    "AGENT_TOKEN_TTL",
    "MAX_EVENTS",
    "MAX_LINE",
]
