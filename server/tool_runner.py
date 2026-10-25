"""手动模式：工具契约的校验与 argv 拼装。

**工具 = 固定入口 + 固定契约。** 这一模块负责两件事：

1. **按契约校验参数**：契约外的参数名一律拒（写错了会静默用默认值，
   用户以为传了、其实跑的是另一回事）；类型对不上也拒——不猜。
2. **按契约拼 argv**：位置参数只能按顺序传，所以顺序必须是契约里的顺序。

平台的两头都用它：API 侧建任务前校验，调度器侧起进程前拼命令。
**只有一份实现**——两份拼法漂移起来没人发现。
"""
from __future__ import annotations

import os
import shutil
import sys


class ToolParamError(ValueError):
    """参数不合法。**消息要直接说清哪里不对**——它会原样给用户看。"""


def coerce_value(p: dict, raw):
    """按契约类型把值转成对的形态。转不了就报错——不猜。"""
    t = p.get("type") or "string"
    if t in ("int", "float"):
        try:
            return int(raw) if t == "int" else float(raw)
        except (TypeError, ValueError):
            raise ToolParamError(f"参数 {p['name']} 要的是{t}") from None
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ToolParamError(f"参数 {p['name']} 要的是 true/false")
    return str(raw)


def validate_params(tool: dict, values: dict) -> dict:
    """按契约校验参数并填上默认值。**不认识的名字一律拒。**"""
    spec = {p["name"]: p for p in (tool.get("params") or [])}
    unknown = [k for k in (values or {}) if k not in spec]
    if unknown:
        raise ToolParamError(
            f"这个工具没有这些参数：{'、'.join(sorted(unknown))}")
    out = {}
    for name, p in spec.items():
        raw = (values or {}).get(name, None)
        if raw is None or (isinstance(raw, str) and raw == ""):
            if p.get("required"):
                raise ToolParamError(f"缺少必填参数：{name}")
            # **默认值也要按类型转**：契约里写 default: "3" 而类型是 int，
            # 存下去就该是 3 而不是 "3"——不然拼进命令行时类型信息就丢了。
            dflt = p.get("default")
            out[name] = coerce_value(p, dflt) if dflt not in (None, "") else ""
            continue
        out[name] = coerce_value(p, raw)
    return out


def find_bash() -> str:
    """找 bash。**顺序必须和平台（pi 的 getShellConfig）一致**。

    Windows 上 PATH 里的 ``bash.exe`` 常常是 **WSL 的**
    （在 system32 底下）——它看不见 Windows 路径，
    跑工作目录里的 .sh 直接 127 command not found。
    Git bash（MSYS）才看得见。所以先找 Git bash，再退到 PATH。
    """
    if os.name == "nt":
        cands = []
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                cands.append(os.path.join(base, "Git", "bin", "bash.exe"))
        for c in cands:
            if os.path.isfile(c):
                return c
    found = shutil.which("bash")
    if found:
        return found
    raise ToolParamError("这台机器上找不到 bash，跑不了这个工具")


def build_argv(tool: dict, params: dict) -> list[str]:
    """把契约参数拼成 argv。**顺序 = 契约里的顺序。**

    位置参数只能按顺序传，顺序错了整个调用就错了位。
    """
    runner = tool.get("runner_path")
    if not runner:
        raise ToolParamError("这个工具没有入口脚本")
    args = []
    for p in (tool.get("params") or []):
        # 兜底填默认值：正常路径上 validate_params 已经填过了，
        # 但 build_argv 也可能被直接调用（测试、以后别的入口）。
        v = params.get(p["name"], None)
        if v is None or v == "":
            v = p.get("default") or ""
        args.append("" if v is None else str(v))
    kind = tool.get("shell_kind")
    if kind == "host":
        return [sys.executable, str(runner)] + args
    return [find_bash(), str(runner)] + args


def tool_spec(tool: dict, params: dict, cwd: str = "") -> dict:
    """给 ``agent/run_tool.py`` 的规格（JSON）。"""
    return {
        "tool_id": str(tool.get("id") or ""),
        "display_name": str(tool.get("display_name") or tool.get("id") or ""),
        "argv": build_argv(tool, params),
        "cwd": cwd,
        "timeout": int(tool.get("timeout_s") or 600),
        "params": params,
    }
