"""手动模式：工具契约的校验与 argv 拼装。

**工具 = 固定入口 + 固定契约。** 这一模块负责两件事：

1. **按契约校验参数**：契约外的参数名一律拒（写错了会静默用默认值，
   用户以为传了、其实跑的是另一回事）；类型对不上也拒——不猜。
2. **按契约拼 argv**：位置参数只能按顺序传，所以顺序必须是契约里的顺序。

平台的两头都用它：API 侧建任务前校验，调度器侧起进程前拼命令。
**只有一份实现**——两份拼法漂移起来没人发现。
"""
from __future__ import annotations

import json
import os
import re
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


# ---------------------------------------------------------------- agent 填参数

#: 模型回的一段 JSON 里可能带 ```json 围栏，也可能前后有话。
#: 这里只取**第一个完整的 JSON 对象**，取不到就说取不到——不猜。
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)


def _parse_json_obj(raw: str) -> dict:
    text = str(raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        m = _JSON_OBJ_RE.search(text)
        if not m:
            raise ToolParamError("模型回的不是 JSON 对象") from None
        try:
            data = json.loads(m.group(0))
        except ValueError as exc:
            raise ToolParamError(f"模型回的 JSON 解析不了：{exc}") from None
    if not isinstance(data, dict):
        raise ToolParamError("模型回的不是 JSON 对象")
    return data


def fill_params(tool: dict, requirement: str) -> dict:
    """让模型把一句自然语言要求翻译成**契约参数**。

    这是「手动模式」的辅助层：**执行仍然由脚本做，模型只负责填参数**。
    换句话说，模型没有执行权——它改不了跑什么命令、也写不了产物，
    最多把参数填歪，而填歪的参数会被契约校验挡住。

    返回 ``{"params": {...}, "notes": "...", "missing": [...]}``：
    - ``params``：能填的都填了（类型按契约转过）；
    - ``missing``：**必填但模型没能填出来的**（模型不知道的那些），
      界面把它们标出来让人补——而不是让模型编一个；
    - ``notes``：模型自己说的假设。
    """
    import llm

    req = str(requirement or "").strip()
    if not req:
        raise ToolParamError("先说说你想算什么")
    params = tool.get("params") or []
    if not params:
        raise ToolParamError("这个工具没有参数契约，没什么可填的")

    contract = json.dumps(params, ensure_ascii=False, indent=1)
    system = (
        "你是「自动化计算平台」的工具参数助手。\n"
        "用户说清了他想用某个工具做什么，"
        "你要把这个工具的**参数**按契约填出来。\n\n"
        "硬规则：\n"
        "1. **只用契约里出现过的参数名**，一个都不许新增。\n"
        "2. 值要符合契约里的类型（int 就给整数，path 就给路径）。\n"
        "3. 拿不准的值就给最有合理的默认值，**并在 _notes 里写清你的假设**。\n"
        "4. 用户的描述里没有的信息，**不要编**——留空，让界面标出来让人补。\n"
        "5. 只输出一个 JSON 对象，不要任何解释文字。\n\n"
        "参数契约：\n" + contract)
    user = ("工具：%s（%s）\n工具说明：%s\n\n用户的要求：%s"
            % (tool.get("display_name") or tool.get("id"),
               tool.get("id") or "?",
               (tool.get("description") or "")[:800], req))

    # **思考档位给 low**：这是抽取/映射类的活，不是要它推理。
    raw = llm.chat_completion(user, system=system, reasoning_effort="low")
    data = _parse_json_obj(raw)
    notes = str(data.pop("_notes", "") or "")

    spec = {p["name"]: p for p in params}
    filled = {}
    dropped = []
    for name, value in data.items():
        if name not in spec:
            dropped.append(name)
            continue
        p = spec[name]
        try:
            filled[name] = coerce_value(p, value)
        except ToolParamError:
            dropped.append(name)
    if dropped:
        notes = (notes + "；".join([""] if not notes else [""])
                + "忽略了不在契约里的参数：" + "、".join(dropped)).strip("；")

    missing = [p["name"] for p in params
               if p.get("required") and not filled.get(p["name"])]
    return {"params": filled, "notes": notes, "missing": missing}
