"""ProtForge 技能索引：扫技能目录，给出「每个技能怎么跑、要跑多久」。

ProtForge 23 个技能**没有统一的 runner 契约**，实测分三类：

| 类别 | 数量 | shell_kind | 依据 |
|---|---|---|---|
| bash runner | 17 | `gitbash` | `scripts/*.sh`，跑在 `C:\\Program Files\\Git\\bin\\bash.exe`，会打印 `PROTFORGE_STATUS:` |
| 宿主 `.py` | 1（alphafold-db） | `host` | `scripts/afdb.py`，宿主 Python 直跑 |
| WSL 直调 | 3（esmfold2 / opendde / af3-msa） | `wsl` | SKILL.md 正文里的 `wsl.exe -d AF3Native`，不经过 bash runner |
| 其他 | reglyco / overview | `host` | 无 runner，路由或宿主进程 |

注意：只有 19 个 SKILL.md 提到 `PROTFORGE_STATUS:`，且 23 个里只有 17 个真有
`.sh` runner——**上层不能把它当全局强制契约**，只能按 has_runner 分支处理。

两条降级策略刻意不同，理由写在各自的代码路径上：
- **shell_kind 判定不出 → 静默归 `host` + 默认超时**：一个技能坏掉不许拖垮整个扫描。
- **frontmatter 解析失败 → 抛 `SkillIndexError`**：解析失败会让技能从索引里**静默消失**，
  ProtForge 仓库自带 `test_frontmatter.py` 守的正是这个坑（description 是含
  `Trigger: When ...` 的长自由文本，裸标量写法会让 PyYAML 抛 ScannerError）。
  所以这里绝不 `try/except` 吞异常，宁可炸给维护者看，也不让技能悄悄不见了。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

#: 技能目录。**默认指到旁边那个技能仓的 skills/**（``../protforge/skills``）：
#: 技能仓是另一个仓库（github.com/dy12021/protforge），clone 在旁边就能用。
#: 指到别处用 ``PROTFORGE_WEB_SKILLS_DIR`` 覆盖（本机的写法放在 ``.env`` 里）。
PROTFORGE_SKILLS_DIR = str(
    Path(__file__).resolve().parent.parent.parent / "protforge" / "skills")

# 计划给出的超时表（秒）。档位越短越容易被腰斩，越长挂死任务收口越晚。
DEFAULT_TIMEOUT_S = 14400          # 4h
TIMEOUT_FAST_S = 600                # 10min：秒级的几何/描述符工具
TIMEOUT_MED_S = 7200                # 2h：对接类
TIMEOUT_SLOW_S = 28800              # 8h：生成类（扩散/折叠/采样）

_TIMEOUT_BY_SKILL: dict[str, int] = {
    # 600s
    "freesasa": TIMEOUT_FAST_S,
    "chemprop": TIMEOUT_FAST_S,
    "discotope3": TIMEOUT_FAST_S,
    "fpocket": TIMEOUT_FAST_S,
    "p2rank": TIMEOUT_FAST_S,
    # 7200s
    "vina-gpu": TIMEOUT_MED_S,
    "gnina": TIMEOUT_MED_S,
    "autodock-gpu": TIMEOUT_MED_S,
    "diffdock": TIMEOUT_MED_S,
    # 28800s
    "rfdiffusion": TIMEOUT_SLOW_S,
    "bindcraft": TIMEOUT_SLOW_S,
    "gromacs": TIMEOUT_SLOW_S,
    "proteinmpnn": TIMEOUT_SLOW_S,
    # 实测（2026-10-16 本机，Docker 已起、容器已热）：
    #   pesto  1ubq.pdb（76 残基）        16.6 s  -> 11 hotspots，STATUS: OK
    #   esm    1ubq 序列（73 残基）embed  36.9 s  -> dim=1280，STATUS: OK
    # 这两个数是热容器 + 小输入，所以不拿它们当上限（esm 喂 2000 残基是这里的
    # 27 倍），只用来定档：原来都挂在 DEFAULT_TIMEOUT_S(4h) 上，那个值是猜的，
    # 代价是挂死任务占着并发名额和一张卡干等 4 小时。降到 MED 档——比实测宽两个
    # 数量级，收口时间砍一半。
    "pesto": TIMEOUT_MED_S,
    "esm": TIMEOUT_MED_S,
    # parasurf 实测（2026-10-21 本机，CPU torch，223 残基的 Fv）：约 9 分钟。
    # 这份镜像装的是 CPU 版 torch，网格采样和力场特征全在 CPU 上跑，比看上去慢得多。
    # 档位取 MED（2h）：比默认 4h 早收口两小时，同时比实测宽一个数量级，
    # 大输入的 Fv 也不会被腰斩。
    "parasurf": TIMEOUT_MED_S,
}

# 档位未经验证的技能，供上层在 UI/日志上标出来，别让人当成实测值引用。
UNVERIFIED_TIMEOUTS = frozenset()      # pesto / esm 已于 2026-10-16 实测并归档

_PREFIX = "protforge-"
_WSL_RE = re.compile(r"wsl\.exe\s+-d", re.IGNORECASE)
# 技能提到外部输入目录 → 需要任务工作区。
_INPUTS_RE = re.compile(r"data/inputs", re.IGNORECASE)
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


class SkillIndexError(RuntimeError):
    """技能索引不可信（frontmatter 解析失败等）。不允许静默丢弃技能。"""


@dataclass(frozen=True)
class ToolParam:
    """工具的一个参数（契约）。

    ``name`` 必须与入口脚本的**位置参数**同名——手动模式按契约拼 argv，
    契约里没有的名字不许传（防「参数名写错→静默用默认值」）。
    """

    name: str
    type: str                # string | int | float | bool | path
    required: bool
    default: str
    label: str
    description: str

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type,
                "required": self.required, "default": self.default,
                "label": self.label or self.name,
                "description": self.description}


@dataclass(frozen=True)
class SkillInfo:
    """单个技能的静态索引信息。"""

    name: str
    description: str
    has_runner: bool
    runner_path: str | None
    shell_kind: str          # gitbash | wsl | host
    timeout_s: int
    reads_workspace: bool
    # --- 工具市场那几项（来自可选的 tool.yaml，缺文件就是空值）---
    tool_id: str = ""              # 空 = 不是「可直接调用的工具」
    version: str = ""
    display_name: str = ""
    category: str = ""
    batch: bool = False
    params: tuple = ()             # tuple[ToolParam, ...]
    #: **能不能被「直接调用」**：有入口脚本 + 声明了 tool_id。
    #: 说明书型的技能（sandwich-antibody / opendde 那些「路由器」）
    #: 没有可直接调用的入口，只能由 agent 编排——它们在工具市场里
    #: 会被标成「需要 agent 辅助」，而不是伪装成一个能点的工具。
    directly_callable: bool = False


def _bare(name: str) -> str:
    """`protforge-freesasa` → `freesasa`；不带前缀的原样返回。"""
    return name[len(_PREFIX):] if name.startswith(_PREFIX) else name


def timeout_for(name: str) -> int:
    """按技能名查超时（秒）。认带不带 `protforge-` 前缀，未知技能给默认值。"""
    return _TIMEOUT_BY_SKILL.get(_bare(name), DEFAULT_TIMEOUT_S)


#: 工具元数据文件名（放在技能目录里，跟技能一起版本化）。
TOOL_MANIFEST_NAME = "tool.yaml"

#: 允许的参数类型。**不在表内的类型直接拒**——手动模式要拿这个类型
#: 去校验用户填的值，放行未知类型等于把校验漏掉。
_PARAM_TYPES = ("string", "int", "float", "bool", "path")


def _load_tool_manifest(skill_dir: Path, skill_name: str) -> dict:
    """读 ``tool.yaml``。**没有就返回空字典**——不是每个技能都是工具。

    解析失败抛 :class:`SkillIndexError`（与 frontmatter 同一套规矩）：
    契约文件坏掉就静默丢掉，等于「这个工具存在但参数不校验」。
    """
    path = skill_dir / TOOL_MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except yaml.YAMLError as e:
        raise SkillIndexError(
            f"技能 {skill_name} 的 {TOOL_MANIFEST_NAME} 不是合法 YAML：{e}") from e
    except OSError:
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SkillIndexError(
            f"技能 {skill_name} 的 {TOOL_MANIFEST_NAME} 必须是映射（键值对），"
            f"实际是 {type(data).__name__}")
    return data


def _parse_params(raw, skill_name: str) -> tuple:
    """把 tool.yaml 的 params 变成 :class:`ToolParam` 序列。

    **每个参数都必须有名字**——没名字的参数在手动模式里没法拼 argv。
    类型不在白名单里直接拒：手动模式要按类型校验用户填的值。
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SkillIndexError(f"技能 {skill_name} 的 params 必须是列表")
    out = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise SkillIndexError(f"技能 {skill_name} 的某个参数不是映射：{item!r}")
        name = str(item.get("name") or "").strip()
        if not name:
            raise SkillIndexError(f"技能 {skill_name} 有参数没写 name")
        if name in seen:
            raise SkillIndexError(f"技能 {skill_name} 的参数 {name} 重复")
        seen.add(name)
        ptype = str(item.get("type") or "string").strip().lower()
        if ptype not in _PARAM_TYPES:
            raise SkillIndexError(
                f"技能 {skill_name} 的参数 {name} 类型 {ptype!r} 不在白名单 {_PARAM_TYPES}")
        default = item.get("default")
        out.append(ToolParam(
            name=name,
            type=ptype,
            required=bool(item.get("required", False)),
            default="" if default is None else str(default),
            label=str(item.get("label") or ""),
            description=str(item.get("description") or ""),
        ))
    return tuple(out)


def _read_text(path: Path) -> str:
    """读文本；读不了就当没有。**不让一个坏文件炸掉整个扫描**。

    注意：这里返回空串不会让技能消失——它照样进索引，只是描述为空，
    上层能看见「这个技能没读到 SKILL.md」，而不是查不到它。
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_frontmatter(text: str, skill_name: str) -> dict:
    """用**真 YAML 解析器**取 frontmatter。

    绝不用正则抠字段、也绝不 `try: yaml.safe_load() except: pass`——
    SKILL.md 的 description 是含 `Trigger: When ...` 的长自由文本，
    裸标量里的 ': ' 会让 PyYAML 抛 ScannerError，技能就此静默消失。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise SkillIndexError(
            f"技能 {skill_name} 的 SKILL.md frontmatter 不是合法 YAML，拒绝静默丢弃该技能：{e}"
        ) from e
    return data if isinstance(data, dict) else {}


def _first_script(skill_dir: Path, suffix: str) -> Path | None:
    """取 scripts/ 下第一个指定后缀的脚本（按名字排序，保证结果稳定）。"""
    scripts = skill_dir / "scripts"
    if not scripts.is_dir():
        return None
    hits = sorted(p for p in scripts.glob(f"*{suffix}") if p.is_file())
    return hits[0] if hits else None


def _detect_shell(skill_dir: Path, body: str) -> tuple[str, Path | None]:
    """判定 shell_kind 与本平台要调的入口脚本。

    顺序即优先级，不可调换：
    1. 有 `.sh` → `gitbash`。protforge-alphafold3 同时有 `.sh` 和正文里的
       `wsl.exe -d`，计划把它归在 17 个 bash runner 里，所以 `.sh` 必须压过 WSL 正文。
    2. 正文含 `wsl.exe -d` → `wsl`。这 3 个技能是 WSL 直调，**不经过 bash runner**，
       入口写死在 SKILL.md 正文里，所以 runner_path 为 None。
    3. 有 `.py` → `host`（宿主 Python 直跑）。
    4. 都不满足 → `host` + 无入口。**这里不许抛异常**：判定不出就归 host 兜底，
       一个技能坏掉不能拖垮整个扫描。
    """
    sh = _first_script(skill_dir, ".sh")
    if sh is not None:
        return "gitbash", sh
    if _WSL_RE.search(body):
        return "wsl", None
    py = _first_script(skill_dir, ".py")
    if py is not None:
        return "host", py
    return "host", None


def _build(skill_dir: Path) -> SkillInfo:
    name = skill_dir.name
    text = _read_text(skill_dir / "SKILL.md")
    meta = _parse_frontmatter(text, name)

    desc = meta.get("description", "")
    if desc is None:
        desc = ""
    elif not isinstance(desc, str):       # YAML 可能解析出 bool/int/嵌套结构
        desc = str(desc)

    body = text
    shell_kind, runner = _detect_shell(skill_dir, body)

    # 「会不会读工作区」要求**真的会执行**。protforge-overview 是路由文档，
    # 正文里写了 `$PROTFORGE_DATA/inputs/` 只是**描述**目录约定、自己不执行，
    # 所以光靠 data/inputs 子串会把它误判成 True——必须再过一道「可执行」筛。
    # WSL 直调技能没有宿主 runner，但照样执行，所以 shell_kind == "wsl" 也算可执行。
    executable = runner is not None or shell_kind == "wsl"
    reads_workspace = executable and bool(_INPUTS_RE.search(body))

    manifest = _load_tool_manifest(skill_dir, name)
    params = _parse_params(manifest.get("params"), name)
    # **tool.yaml 可以覆盖这个技能的超时档位**：档位表在平台代码里，
    # 但「这个工具的正常耗时」是工具自己的事——它自己最清楚。
    timeout_s = timeout_for(name)
    if manifest.get("timeout_s") is not None:
        try:
            timeout_s = int(manifest["timeout_s"])
        except (TypeError, ValueError):
            raise SkillIndexError(
                f"技能 {name} 的 timeout_s 不是整数：{manifest['timeout_s']!r}") from None
        if timeout_s <= 0:
            raise SkillIndexError(f"技能 {name} 的 timeout_s 必须大于 0")
    tool_id = str(manifest.get("id") or "").strip()
    # **有 tool.yaml 但没 id** = 想当工具却没给身份 → 报错，不是当普通技能。
    if manifest and not tool_id:
        raise SkillIndexError(
            f"技能 {name} 有 {TOOL_MANIFEST_NAME} 但没写 id，"
            f"工具市场里没有身份的工具是不能上架的")
    if tool_id and not tool_id.replace("-", "").replace("_", "").isalnum():
        raise SkillIndexError(f"技能 {name} 的工具 id 只能含字母数字下划线连字符：{tool_id!r}")

    return SkillInfo(
        name=name,
        description=desc,
        has_runner=runner is not None,
        runner_path=str(runner) if runner is not None else None,
        shell_kind=shell_kind,
        timeout_s=timeout_s,
        reads_workspace=reads_workspace,
        tool_id=tool_id,
        version=str(manifest.get("version") or ""),
        display_name=str(manifest.get("display_name") or ""),
        category=str(manifest.get("category") or ""),
        batch=bool(manifest.get("batch", False)),
        params=params,
        # **能直接调用 = 有入口脚本 + 有工具身份**。
        # 说明书型技能（没有 runner）不给上架，免得点下去跑不起来。
        directly_callable=bool(tool_id) and runner is not None,
    )


def scan(skills_dir: str | Path | None = None) -> dict[str, SkillInfo]:
    """扫描技能目录，返回 `{技能名: SkillInfo}`。

    `skills_dir` 省略时用 `PROTFORGE_SKILLS_DIR`（也可用环境变量
    `PROTFORGE_WEB_SKILLS_DIR` 覆盖），传参主要是给测试用临时目录。

    只扫 `protforge-*` 子目录。目录不存在时返回空字典，不抛异常——
    技能目录是外部依赖，不该让上层服务直接崩掉。
    """
    root = Path(skills_dir or os.environ.get("PROTFORGE_WEB_SKILLS_DIR") or PROTFORGE_SKILLS_DIR)
    if not root.is_dir():
        return {}

    return {p.name: _build(p) for p in sorted(root.glob("protforge-*")) if p.is_dir()}


# ---------------------------------------------------------------- 工具市场

def tool_view(info: SkillInfo) -> dict:
    """一个技能在工具市场里的样子。

    说明书型技能（没有可直接调用的入口）也出现在清单里，但
    ``directly_callable=False``——界面上标成「需要 agent 辅助」，
    **不能假装成一个能点的工具**。
    """
    return {
        "id": info.tool_id or _bare(info.name),
        "skill": info.name,
        "version": info.version,
        "display_name": info.display_name or info.name,
        "description": info.description,
        "category": info.category,
        "batch": info.batch,
        "directly_callable": info.directly_callable,
        "shell_kind": info.shell_kind,
        "runner_path": info.runner_path,
        "timeout_s": info.timeout_s,
        "reads_workspace": info.reads_workspace,
        "params": [p.to_dict() for p in info.params],
    }


def list_tools(skills_dir: str | Path | None = None) -> list[dict]:
    """工具市场清单。按 id 排序。"""
    idx = scan(skills_dir)
    return sorted((tool_view(i) for i in idx.values()), key=lambda t: t["id"])


def get_tool(tool_id: str, skills_dir: str | Path | None = None) -> dict | None:
    """按工具 id 取一个工具。找不到返回 None（不抛）。"""
    want = (tool_id or "").strip()
    if not want:
        return None
    for t in list_tools(skills_dir):
        if t["id"] == want or t["skill"] == want:
            return t
    return None


def tool_budgets(skills_dir: str | Path | None = None) -> dict:
    """``{入口脚本文件名: 该工具的超时预算（秒）}``。

    给 agent 侧的守卫用：命令里出现了哪个工具的入口脚本，就用那个工具的
    预算当上限。**键是文件名**（``proteinmpnn.sh``）而不是绝对路径——
    agent 的命令里入口可能写成 ``${PLUGIN_DIR}/skills/...`` 这种变量形式，
    绝对路径匹配不上。
    """
    out: dict = {}
    for info in scan(skills_dir).values():
        budget = int(info.timeout_s)
        # **技能裸名**也算一个识别键：WSL 直调型技能（opendde / esmfold2 /
        # af3-msa）的入口写在 SKILL.md 正文里、**没有 scripts/ 目录**，
        # 按文件名认不出来。命令里出现技能名时按它的档位给额度。
        bare = _bare(info.name)
        if bare:
            out.setdefault(bare, budget)
        if not info.runner_path:
            continue
        name = Path(info.runner_path).name
        if name:
            out[name] = budget
    return out
