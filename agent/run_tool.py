# -*- coding: utf-8 -*-
"""手动模式：**平台直接跑一个工具脚本**，不经 agent。

它做四件事：

1. 按契约把参数拼成 argv，跑那个入口脚本，**把它的输出原样转成 stdout**；
2. 跑的过程中周期性上报「正在跑什么」（网页上那句话要活着，否则看着像卡死）；
3. 跑完把结果摘要报给平台（finish 标记）→ 这一行就算「正常收口」；
4. 出错就把 stderr 转过去、以非零退出 → 平台按「异常结束」收尾。

**为什么不经 agent**
------------------
工具是**固定入口 + 固定契约**。让模型去「按要求调用工具」，等于把确定性的活
交给不确定的环节：它可能自己写一段替代脚本（2026-10-24 那条跑了 4 小时 42 分的
O(n²) 碰撞消解就是这么来的），还多烧一次模型调用。

直接跑脚本：快、可复现、**不会自己发明慢算法**。

需要模型的时候只有一处：把一句自然语言要求翻译成契约参数
（平台侧的「填参数」动作），翻译完还是由这里执行。

环境变量
--------
``PROTFORGE_WEB_TOOL_SPEC``  JSON：
    ``{"tool_id": ..., "display_name": ..., "argv": [...], "cwd": ...,
       "timeout": 600, "summary_hint": "..."}``
``PROTFORGE_AGENT_TOKEN`` / ``PROTFORGE_TASK_ID`` / ``PROTFORGE_WEB_INTERNAL``
    与 agent 进程同一套（回传用）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 上报间隔（秒）。与 agent 的心跳同一量级：长命令跑着的时候，
#: 网页上那句话要能续上，否则用户看着像卡死。
HEARTBEAT_S = 20.0


def _post(base: str, task_id: str, token: str, kind: str, body: dict) -> bool:
    """回传一步。**失败不抛**：回传是观测手段，不是任务的一部分。"""
    url = f"{base.rstrip('/')}/api/internal/agent/{task_id}/{kind}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    # **X-Agent-Token**，不是 Authorization: Bearer——
    # 内部端点的鉴权依赖认的是这个头（server/app.py 的 require_agent）。
    # 用错了就是 401「agent_token 无效或已过期」，而任务会被判成「异常结束」。
    req.add_header("X-Agent-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return 200 <= res.status < 300
    except Exception:                              # noqa: BLE001
        return False


def _heartbeat(base: str, task_id: str, token: str, text: str,
               stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_S):
        _post(base, task_id, token, "step", {"text": text})


def _outputs_root() -> Path:
    """技能约定的产物目录。"""
    data = (os.environ.get("PROTFORGE_DATA") or "").strip()
    return Path(data) / "outputs" if data else Path("outputs")


def _snapshot_outputs(root: Path) -> set:
    """拍一张 outputs 目录的快照（相对路径 -> mtime）。"""
    out = set()
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        try:
            if p.is_file():
                out.add(str(p.relative_to(root)))
        except OSError:
            continue
    return out


def main() -> int:
    raw = (os.environ.get("PROTFORGE_WEB_TOOL_SPEC") or "").strip()
    if not raw:
        print("缺少 PROTFORGE_WEB_TOOL_SPEC", file=sys.stderr)
        return 2
    try:
        spec = json.loads(raw)
    except ValueError as exc:
        print(f"PROTFORGE_WEB_TOOL_SPEC 不是合法 JSON：{exc}", file=sys.stderr)
        return 2

    argv = list(spec.get("argv") or [])
    if not argv:
        print("工具规格里没有 argv", file=sys.stderr)
        return 2

    cwd = spec.get("cwd") or os.environ.get("PROTFORGE_WORK_DIR") or os.getcwd()
    timeout = float(spec.get("timeout") or 600)
    tool_id = str(spec.get("tool_id") or "?")
    display = str(spec.get("display_name") or tool_id)
    task_id = str(os.environ.get("PROTFORGE_TASK_ID") or "")
    token = str(os.environ.get("PROTFORGE_AGENT_TOKEN") or "")
    base = str(os.environ.get("PROTFORGE_WEB_INTERNAL") or "")

    def out(line: str) -> None:
        """转成 stdout。**非 JSON 行在平台那边会被当成普通日志事件**——
        这是有意的：工具的输出就是要给人看的。"""
        try:
            sys.stdout.write(line.rstrip("\n") + "\n")
            sys.stdout.flush()
        except Exception:                          # noqa: BLE001
            pass

    out(f"[tool] 开始运行 {display}（{tool_id}）")
    out(f"[tool] 命令：{' '.join(argv)}")

    stop = threading.Event()
    hb = None
    if base and task_id and token:
        hb = threading.Thread(target=_heartbeat,
                              args=(base, task_id, token,
                                    f"正在运行 {display}", stop),
                              daemon=True)
        hb.start()

    out_root = _outputs_root()
    before = _snapshot_outputs(out_root)

    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace",
                                bufsize=1)
    except Exception as exc:                      # noqa: BLE001
        stop.set()
        out(f"[tool] 起不来：{exc}")
        return 2

    timed_out = False
    try:
        for line in proc.stdout:                  # type: ignore[union-attr]
            out(line)
            if time.monotonic() - started > timeout:
                timed_out = True
                break
        if timed_out:
            out(f"[tool] 超过 {timeout:.0f} 秒还没结束，杀掉")
            try:
                proc.kill()
            except Exception:                      # noqa: BLE001
                pass
        proc.wait(timeout=30)
    except Exception as exc:                      # noqa: BLE001
        out(f"[tool] 读取输出时出错：{exc}")
    finally:
        stop.set()
        if hb is not None:
            hb.join(timeout=2.0)

    rc = proc.returncode
    elapsed = time.monotonic() - started
    out(f"[tool] 结束：退出码 {rc}，耗时 {elapsed:.1f} 秒")

    if rc != 0:
        if timed_out:
            return 124                        # 与 timeout(1) 的退出码一致
        return rc if isinstance(rc, int) and rc > 0 else 1

    # **自报产物清单**：跑前跑后的差集。验收时拿它核对「说有、盘上找不到」。
    produced = sorted(_snapshot_outputs(out_root) - before)[:50]
    if produced:
        out("[tool] 产出：%s" % "、".join(produced[:10]))

    # **收口**：只有跑成功才报 finish。平台据此进打包阶段。
    if base and task_id and token:
        summary = (spec.get("summary_hint")
                   or f"手动运行工具 {display}（{tool_id}），"
                        f"退出码 0，耗时 {elapsed:.1f} 秒")
        ok = _post(base, task_id, token, "finish",
                   {"summary": summary, "artifacts": produced})
        if not ok:
            out("[tool] 收口回传失败（平台会按异常结束收尾）")
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
