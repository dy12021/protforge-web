"""打包子进程的 stderr 背压（缺陷 A：_run_pack_worker 只读 stdout 才读 stderr → 死锁）。

钉死的是这个回归：打包子进程往 stderr 写超过管道缓冲区的量——打包 20 GB 时一条
traceback / warning 就够填满 Windows 匿名管道那 4 KB——写满后子进程阻塞在 stderr 写
上、再也吐不出 stdout；父进程却还卡在 ``for line in proc.stdout`` 等 EOF。两边互等，
永久挂起，不是"慢"而是"再也不回来"。

**这些用例起的是真子进程**（``python server/artifacts.py --pack <spec>``），不用 mock
假装进程：噪声由子进程在解释器启动阶段经 ``sitecustomize`` 直接写 fd 2，所以它会
真去占满父进程的 stderr 管道。回归时这几条是真的会挂死，不是演出来的。

**父进程侧有硬超时兜底**：``_run_with_deadline`` 到点就判失败，并用子进程自己记下的
pid 把走时的进程杀掉，所以即使修复被撤掉，pytest 也是几十秒内红，不会跟着一起吊住。

``outputs`` 根用 ``PROTFORGE_DATA`` 指向 tmp_path，**测试绝不读真实的 E 盘 outputs**
（那里有用户既有的计算结果）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import artifacts  # noqa: E402
import store  # noqa: E402

# 挂死保护上限。修复到位时整条用例是秒级；死锁时到点即判失败。
_HANG_LIMIT = 60.0
# 超时用例给的业务超时：必须远小于 _HANG_LIMIT，好让"超时被杀"与"挂死"两种结局
# 不会混成同一个现象。
_PACK_TIMEOUT = 3.0

# 子进程启动时跑的注入脚本模板。噪声直写 fd 2（绕开 Python 的 stderr 缓冲），
# 所以写满管道这件事和 Python 的缓冲策略无关，任何解释器上都成立。
_SITECUSTOMIZE = '''# 由测试注入：让打包子进程在解释器启动阶段就往 stderr 倒大量噪音。
import os
import time

_noise = {noise!r}
_sleep = {sleep!r}
_pidfile = {pidfile!r}

if _pidfile:
    with open(_pidfile, "w") as _f:
        _f.write(str(os.getpid()))
if _noise:
    _chunk = ("stderr 背压回归噪音 line " * 40 + "\\n").encode("utf-8")
    _written = 0
    while _written < _noise:
        # 按**实际写入量**累加：管道满时 os.write 会短写，按请求量算会少写一大截，
        # "远超缓冲区"就成了自己以为的。
        _written += os.write(2, _chunk)
if _sleep:
    time.sleep(_sleep)
'''


@pytest.fixture
def outputs(tmp_path, monkeypatch) -> Path:
    """假的 ProtForge outputs 根（与 test_artifacts.py 同法）。"""
    monkeypatch.setenv("PROTFORGE_DATA", str(tmp_path / "pf"))
    root = tmp_path / "pf" / "outputs"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def task() -> dict:
    return store.create_task("设计一个结合蛋白", "要能表达的小分子结合蛋白", owner_id=1)


def _noisy_child(tmp_path, monkeypatch, *, noise=0, sleep=0.0) -> Path:
    """给**子进程**注入启动噪音/挂起，返回记录 pid 的文件路径。

    走 PYTHONPATH + sitecustomize 而不是 monkeypatch ``subprocess.Popen``：改 Popen
    就等于把"子进程与管道"这半边也换成了假的，死锁复现就不作数了。这里只换子进程的
    启动环境，进程、管道、阻塞全是真的。
    """
    site_dir = tmp_path / "site"
    site_dir.mkdir(exist_ok=True)
    pid_file = tmp_path / "child.pid"
    (site_dir / "sitecustomize.py").write_text(
        _SITECUSTOMIZE.format(noise=int(noise), sleep=float(sleep),
                              pidfile=str(pid_file)),
        encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(site_dir), prepend=os.pathsep)
    return pid_file


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。Windows 上不能用 ``os.kill(pid, 0)``——那是**终止**信号。"""
    if sys.platform == "win32":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        return f'"{int(pid)}"' in (r.stdout or "")
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _kill_child(pid_file: Path) -> None:
    """收掉走时的打包子进程（回归时会卡在写不动的 stderr 上，不杀就漏进程）。"""
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, text=True)
    elif _pid_alive(pid):
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _run_with_deadline(fn, pid_file: Path, limit: float = _HANG_LIMIT):
    """在守护线程里跑 fn，超时即判挂死（并杀掉子进程），不让 CI 跟着吊住。"""
    box: dict = {}

    def _target():
        try:
            box["result"] = fn()
        except BaseException as e:      # noqa: BLE001 —— 传回调用方再断言
            box["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        _kill_child(pid_file)
        pytest.fail(f"打包调用 {limit:.0f}s 仍未返回：_run_pack_worker 死锁了"
                    "（子进程的 stderr 管道写满后，两边互等）")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _make_job(outputs: Path, task_id: int, files: int = 3) -> Path:
    d = outputs / f"t{task_id}_rfdiffusion"
    d.mkdir(parents=True)
    for i in range(files):
        (d / f"part{i}.pdb").write_text("ATOM      1  N   MET A   1\n", encoding="utf-8")
    return d


# ---------------------------------------------------------------- 死锁本体


def test_heavy_stderr_child_does_not_deadlock_packaging(outputs, task, tmp_path,
                                                        monkeypatch):
    """子进程往 stderr 写 512 KB（远超 4 KB 管道缓冲），同时照常报进度。

    修复前：子进程阻塞在 stderr 写 → 父进程卡在等 stdout EOF → 永不返回。
    修复后：stderr 被并发抽干，打包正常跑完并产出 zip。
    """
    _noisy_child(tmp_path, monkeypatch, noise=512 * 1024)
    pid_file = tmp_path / "child.pid"
    _make_job(outputs, task["id"])
    artifacts.discover(task_id=task["id"])

    seen: list[dict] = []
    out = _run_with_deadline(
        lambda: artifacts.package(task["id"], progress_cb=seen.append,
                                  timeout=_PACK_TIMEOUT + 55),
        pid_file)

    assert Path(out).exists()
    assert zipfile.ZipFile(out).namelist(), "打包必须真的产出内容"
    assert {p["event"] for p in seen} >= {"start", "done"}, "进度没回来：说明 stdout 侧被饿死了"


def test_stderr_never_starves_progress_when_child_is_chatterbox(outputs, task, tmp_path,
                                                                 monkeypatch):
    """几百 KB 噪音 + 进度同时来时，进度事件必须一条不少地回到调用方。

    防的是另一种"修法"：把 stderr 改成丢弃、或把 stdout 也丢进队列后不再 emit——
    死锁是没了，进度条也一起没了。
    """
    _noisy_child(tmp_path, monkeypatch, noise=256 * 1024)
    pid_file = tmp_path / "child.pid"
    _make_job(outputs, task["id"], files=5)
    artifacts.discover(task_id=task["id"])

    seen: list[dict] = []
    _run_with_deadline(
        lambda: artifacts.package(task["id"], progress_cb=seen.append,
                                  timeout=_PACK_TIMEOUT + 55),
        pid_file)

    start = next(p for p in seen if p["event"] == "start")
    done = next(p for p in seen if p["event"] == "done")
    assert done["files"] == start["total"] == 5
    assert [p["done"] for p in seen if p["event"] == "progress"][-1] == 5


# ---------------------------------------------------------------- 诊断信息


def test_failure_message_still_carries_stderr(task, tmp_path, monkeypatch):
    """子进程非 0 退出时，stderr 的关键内容仍然要出现在中文报错里。

    防"修死锁顺手把 stderr 丢了"：诊断信息一丢，线上打包失败就只剩一句退出码。
    """
    _noisy_child(tmp_path, monkeypatch, noise=4096)
    pid_file = tmp_path / "child.pid"
    out = tmp_path / "res.zip"
    spec = tmp_path / "spec.json"
    # 源不存在 → worker 走 error 分支，退出码 2
    spec.write_text(json.dumps({"out_zip": str(out),
                                "sources": [{"src": str(tmp_path / "missing"),
                                             "arcname": "missing"}]}),
                    encoding="utf-8")

    with pytest.raises(artifacts.PackageError) as ei:
        _run_with_deadline(
            lambda: artifacts._run_pack_worker(task["id"], spec, out, None,
                                               _PACK_TIMEOUT + 25),
            pid_file)

    msg = str(ei.value)
    tail_part = msg[msg.rfind("：") + 1:]
    # 只断言 sitecustomize 直写 fd 2 的那段 UTF-8 噪音：worker 自己 stdout 的中文
    # 是另一码事（子进程按 Windows 管道默认 cp936 写、这里按 UTF-8 解，本来就是
    # 乱码，不在本缺陷范围内，也不能拿它当断言）。
    assert "stderr 背压回归噪音 line" in tail_part, f"报错里没有 stderr 内容：{msg!r}"
    assert "missing" in msg, "worker 自己的错误说明丢了"
    assert len(tail_part) <= 300, f"stderr 没有截断到 300 字符：{len(tail_part)}"


def test_stderr_tail_buffer_is_bounded():
    """定长尾巴缓冲：倒 1 MB 也只留 limit，且留下的是**最后**那段。

    防"修死锁"时改成无脑 append——子进程吵起来能把主服务内存吃穿。
    """
    tail = artifacts._Tail(64)
    for _ in range(1024):                 # 1024 × ~1 KB ≈ 1 MB
        tail.append("x" * 1024 + "\n")
    text = tail.text()

    assert len(text) <= 64, len(text)
    assert text.endswith("\n"), "留的必须是尾巴，traceback 的最后几行才有诊断价值"


# ---------------------------------------------------------------- 超时与回收


def test_timeout_kills_the_pack_worker_process(outputs, task, tmp_path, monkeypatch):
    """子进程赖着不走时，timeout 必须真的触发，且进程树被杀掉。

    旧实现的 timeout 只罩住 stdout EOF **之后**的 wait()，死锁时根本轮不到它——
    这条就是钉住"timeout 是覆盖整个操作的，不是摆设"。
    """
    _noisy_child(tmp_path, monkeypatch, sleep=600)
    pid_file = tmp_path / "child.pid"
    _make_job(outputs, task["id"])
    artifacts.discover(task_id=task["id"])

    with pytest.raises(artifacts.PackageError) as ei:
        _run_with_deadline(
            lambda: artifacts.package(task["id"], progress_cb=lambda p: None,
                                      timeout=_PACK_TIMEOUT),
            pid_file, limit=_HANG_LIMIT)

    assert "超时" in str(ei.value), str(ei.value)
    assert pid_file.exists(), "子进程没起来，超时用例没测到东西"
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(50):                   # 最多等 10s 让 taskkill 生效
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    assert not _pid_alive(pid), "超时的打包进程还活着：没杀进程树，zip 会一直占着磁盘"


def test_pack_worker_leaves_no_living_child_on_success(outputs, task, tmp_path,
                                                       monkeypatch):
    """正常收尾也不留活着的子进程（finally 里的 wait 收尸路径被走到）。"""
    _noisy_child(tmp_path, monkeypatch, noise=128 * 1024)
    pid_file = tmp_path / "child.pid"
    _make_job(outputs, task["id"])
    artifacts.discover(task_id=task["id"])

    _run_with_deadline(
        lambda: artifacts.package(task["id"], timeout=_PACK_TIMEOUT + 25), pid_file)

    pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert not _pid_alive(pid), "打包成功了子进程却还在：wait 漏了"
