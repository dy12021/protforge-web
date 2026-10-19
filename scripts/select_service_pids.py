"""挑出「跑着本仓三个服务」的进程——**包括那些不占端口的**。

为什么要有这个文件
------------------
``scripts\\stop-services.ps1`` 原来只按**监听端口**杀进程。于是有一种进程
杀不掉：它跑的是本仓的服务入口（``server\\app.py`` / ``admin\\app_admin.py``），
却已经不占端口了——端口被别人拿走了、它自己还没绑上、或者它是上一轮留下的
子进程。

这种进程留着是有后患的：下一次 ``start-services.ps1`` 看到「端口已占用」就
跳过启动，于是**旧版本继续对外服务**，而日志上写的是「already listening」，
看起来一切正常。脚本开头担心的正是这件事，只是当时只按端口防。

所以「算不算我们的服务进程」这条判断单独拎出来，做成一条**能测的纯函数**：
改坏了能测出来，而不是等到某个晚上发现界面还是旧版。

命令行怎么认
------------
启动器是这么起的（相对路径，``-WorkingDirectory`` 是仓库根）::

    "C:\\ProgramData\\anaconda3\\python.exe" server\\app.py public
    "C:\\ProgramData\\anaconda3\\python.exe" server\\app.py internal
    "C:\\ProgramData\\anaconda3\\python.exe" admin\\app_admin.py

所以认的是**命令行里的一个完整参数**恰好是（或者以 ``\\`` 结尾是）
``server\\app.py`` / ``admin\\app_admin.py``。三条约束缺一不可：

1. 可执行文件得是 python —— 别的程序（比如 node）参数里带同样的路径也不能杀；
2. 入口得是**整个参数** —— ``-c "print('server/app.py')"`` 这种不是；
3. 入口得**逐段对上** —— ``myserver\\app.py`` 不是 ``server\\app.py``。

命令行里出现中文是常事，所以**不走控制台编码那条路**：JSON 从 stdin 进来，
按 UTF-8 解码（PowerShell 那边要把 ``$OutputEncoding`` 设成 UTF-8 才有意义）。
也可以给一个文件路径，从那儿读。

用法::

    python select_service_pids.py            # 从 stdin 读 JSON，打印要杀的 pid
    python select_service_pids.py <进程列表.json>
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

#: 本仓三个服务的入口。相对路径（启动器就是这么起的），比较时按段对。
ENTRY_POINTS = ("server\\app.py", "admin\\app_admin.py")

#: 只认 python 系的可执行文件。别的程序（node 之类）参数里带同样的路径也不杀。
_PY_NAMES = ("python", "pythonw")


def _tokens(cmdline: str) -> list[str]:
    """把命令行切成参数。引号去掉，前后的空白去掉。

    按空白切对**入口参数**是够的：入口路径里没有空格。真带空格的路径
    （``C:\\Program Files\\...``）切开后，后半截仍以 ``\\server\\app.py`` 收尾，
    照样认得出来。
    """
    if not cmdline:
        return []
    return [t.strip('"').strip("'") for t in re.split(r"\s+", cmdline) if t]


def _is_entry(token: str, entry: str) -> bool:
    """这个参数是不是**就是**入口（逐段对上）。

    ``server\\app.py`` ✓；``.\\server\\app.py`` ✓；
    ``myserver\\app.py`` ✗；``server\\app.py.bak`` ✗。
    """
    t = token.replace("/", "\\").lower()
    e = entry.replace("/", "\\").lower()
    return t == e or t.endswith("\\" + e)


def select_service_pids(rows, entry_points=ENTRY_POINTS) -> list[int]:
    """挑出该杀的 pid。**保持传入顺序**，去重。

    ``rows`` 是可迭代的字典，每条至少有 ``pid`` / ``name`` / ``cmdline``。
    """
    out: list[int] = []
    for row in rows:
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid == os.getpid():
            continue
        name = str(row.get("name") or "").lower()
        if not any(name.startswith(p) for p in _PY_NAMES):
            continue
        for tok in _tokens(str(row.get("cmdline") or "")):
            if any(_is_entry(tok, e) for e in entry_points):
                if pid not in out:
                    out.append(pid)
                break
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        try:
            raw = Path(argv[0]).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"读不到进程列表 {argv[0]}：{exc}", file=sys.stderr)
            return 1
    else:
        # stdin 走管道：**按 UTF-8 解码**。命令行里常有中文，
        # 管道那头没设 $OutputEncoding 的话，到这里已经是乱码了。
        # 用 utf-8-sig：带 BOM 也能解（PowerShell 那头有时会多吐一个 BOM）。
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    if not raw.strip():
        return 0
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"进程列表不是合法 JSON：{exc}", file=sys.stderr)
        return 1
    if isinstance(rows, dict):                 # PowerShell 单元素数组会退化成对象
        rows = [rows]
    for pid in select_service_pids(rows):
        print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
