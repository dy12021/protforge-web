"""文件路径守卫（L2 文件边界）。

把 agent 能碰的文件限制在四个区域里：

    data\\work\\<task_id>\\      读写
    data\\uploads\\<task_id>\\   只读（原始上传不可篡改）
    <ProtForge 数据区>\\outputs\\     只读（仅对 agent 的文件工具）
    <ProtForge 数据区>\\inputs\\      只读

**结果包（data\\packages\\task_<id>.zip）不在其中**，它是 `resolve_package()` 单独
管的一条：那一区是用户的交付物，不是 agent 的工作面。理由见那个函数的 docstring。

**这是尽力而为的防误操作层，不是强安全隔离。** 它只做两件事：挡掉明显的路径形态
（UNC、驱动器相对、.. 上跳）和挡掉明显的越界目录。它挡不住刻意构造的绕过
（TOCTOU 换链、WSL 内部、bash 子进程的写操作），spec §6.5 也明说了这一点。

判定顺序是「先 resolve 展开，再判区域」：resolve 会把 8.3 短名（PROTFO~1）展开成
全名、把 junction/symlink 跟到真实目标，所以不能用 startswith(根) 来判——
那样短名和 reparse point 都能绕过。
"""

from __future__ import annotations

import os
from pathlib import Path

# 默认值都是**跟着仓库走的相对位置**，不是某台机器的盘符：
# 数据盘根 = 仓库旁的 data/；ProtForge 自己的地盘 = 旁边那个技能仓的 data/。
# 真实部署用 PROTFORGE_WEB_DATA_ROOT / PROTFORGE_DATA 指过去（写在 .env 里）。
# 环境变量只是覆盖，不是改默认。
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = str(_REPO_ROOT / "data")
DEFAULT_PROTFORGE_ROOT = str(_REPO_ROOT.parent / "protforge" / "data")

WORK = "work"
READ_ONLY_REGIONS = ("uploads", "outputs", "inputs")
PACKAGES = "packages"


class Violation(Exception):
    """路径越界：形态非法、越出所有允许区域，或往只读区写。"""


def _data_root() -> Path:
    return Path(os.environ.get("PROTFORGE_WEB_DATA_ROOT") or DEFAULT_DATA_ROOT)


def _protforge_root() -> Path:
    return Path(os.environ.get("PROTFORGE_DATA") or DEFAULT_PROTFORGE_ROOT)


def _check_task_id(task_id) -> str:
    """task_id 拼进目录名，必须是纯数字，否则它自己就是一条穿越路径。"""
    if isinstance(task_id, bool) or not isinstance(task_id, (int, str)):
        raise Violation(f"非法的 task_id：{task_id!r}")
    s = str(task_id)
    if not s.isdigit():
        raise Violation(f"非法的 task_id：{task_id!r}")
    return s


def regions(task_id) -> dict[str, Path]:
    """返回 task_id 的四个区域根目录。纯路径计算，不建目录、不做 I/O。"""
    tid = _check_task_id(task_id)
    web, pf = _data_root(), _protforge_root()
    return {
        WORK: web / WORK / tid,
        "uploads": web / "uploads" / tid,
        "outputs": pf / "outputs",
        "inputs": pf / "inputs",
    }


def _classify(s: str) -> str:
    """按形态给字符串分类：unc / rooted / drive_relative / absolute / relative。

    形态判定只看字符，不碰文件系统——这样驱动器相对路径（`C:foo`）能在这里被拦下，
    它不以分隔符开头，`startswith` 抓不到，而 `os.path.isabs` 在不同实现上结论不一致。
    """
    if s.startswith("\\\\") or s.startswith("//"):  # UNC、设备路径 \\?\ \\.\、扩展 UNC
        return "unc"
    if s[0] in "/\\":  # 根相对：\Windows\... 或 /etc/passwd
        return "rooted"
    if len(s) >= 2 and s[1] == ":":
        # `C:\foo` 是绝对；`C:foo` / `C:` 是驱动器相对，依赖当前盘符目录，会逃出工作区
        return "absolute" if len(s) > 2 and s[2] in "/\\" else "drive_relative"
    return "relative"


def _resolve(p: Path) -> Path:
    """resolve() 但把它的所有失败都收敛成 Violation，避免原始异常漏过调用方的 except。"""
    try:
        return p.resolve()
    except (OSError, ValueError, RuntimeError) as e:  # RuntimeError: symlink 环
        raise Violation(f"路径无法解析：{p}（{e}）") from e


def _within(cand: Path, base: Path) -> bool:
    """cand 是否等于 base 或落在 base 之下。

    Windows 上 PurePath 比较本身大小写不敏感；这里再补一层 normcase 前缀判断，
    让同一套测试在 POSIX 下也成立，并覆盖 base 末尾分隔符的差异。
    """
    if cand == base or base in cand.parents:
        return True
    c = os.path.normcase(str(cand))
    b = os.path.normcase(str(base)).rstrip("\\/")
    return c == b or c.startswith(b + os.sep) or c.startswith(b + "/")


def resolve(path, task_id, write: bool = False) -> Path:
    """把 path 解析成允许区域内的绝对路径；越界抛 Violation。

    write=True 时只允许落在 work 区。
    """
    root = regions(task_id)
    s = os.path.expandvars(str(path).strip())

    if not s or s == ".":
        return _resolve(root[WORK])  # 空路径 = 工作区根
    if "\x00" in s:
        raise Violation(f"不允许的路径形态：{path}")

    kind = _classify(s)
    if kind in ("unc", "rooted", "drive_relative"):
        raise Violation(f"不允许的路径形态：{path}")

    # 绝对路径允许存在——outputs/inputs 在工作区树之外，只能用绝对路径抵达，
    # 是否合法交给下面的区域判定；相对路径一律按工作区解析。
    cand = _resolve(Path(s) if kind == "absolute" else root[WORK] / s)

    for name, base in root.items():
        b = _resolve(base)
        if _within(cand, b):
            if write and name != WORK:
                raise Violation(f"{name} 区只读：{path}")
            return cand
    raise Violation(f"越出所有允许区域：{path}")


def _same_path(a: Path, b: Path) -> bool:
    """两个路径是不是同一个（Windows 下大小写与末尾分隔符都不算数）。"""
    if a == b:
        return True
    return os.path.normcase(str(a)).rstrip("\\/") == os.path.normcase(str(b)).rstrip("\\/")


def package_root() -> Path:
    """结果包目录。**刻意不在 regions() 里**：那四个区是 agent 的文件边界。"""
    return _data_root() / PACKAGES


def package_file(task_id) -> Path:
    """task_id 自己的结果包应该落在哪。与 artifacts.default_package_path 同一份布局。

    artifacts 依赖 guards，所以这个"反向"的纯路径计算放在这里而不是那边：
    谁算出什么不许各写一份，两个函数算出两个路径就是又一次"包在哪"的争议。
    """
    return package_root() / f"task_{_check_task_id(task_id)}.zip"


def resolve_package(task_id, path) -> Path:
    """把 tasks.package_path 解析成**本任务自己的那一个结果包**，否则抛 Violation。

    为什么单独一个函数，而不是给 regions() 加第五个键：

    - `regions()` 是 agent 能碰的文件边界（调度器起进程、打包取产物都走它）。
      结果包是交给用户的交付物，一个 agent 读自己任务的 zip 没有任何正当理由；
      塞进去等于给每个 agent 开一条"读别人任务几十 GB 压缩包"的路。
    - 真按区域放行也不够。download 原来用的就是 `resolve(write=False)`，那等于
      放行 work / uploads / outputs / **inputs** 里的任意文件——inputs 放的是别的
      任务的输入结构文件。而 packages 又恰恰不在四区里，于是生产写出的包一律 409：
      任务跑完、包生成、点下载永远是"结果包不在允许的目录里"。

    判定因此比 resolve() 严得多：**只认这一个文件**——
    ``<data_root>\\packages\\task_<task_id>.zip``，父目录必须正好是 packages、
    文件名必须正好是本任务那一个。所以 work/uploads/outputs/inputs 里的文件、
    packages 下的别的文件（别人的包、leftover.zip）、子目录里的同名文件一律拒。
    比较发生在 resolve() 之后（短名、junction、上跳都先被展开），形态花招一样过不去。

    文件名由 `_check_task_id` 校验过的纯数字拼出，不含分隔符、不含 `..`，
    因此也不会撞上 `store.artifacts.add` 那条"名字里不能含 / \\ .."的约束。
    """
    tid = _check_task_id(task_id)
    s = os.path.expandvars(str(path).strip())
    if not s or "\x00" in s:
        raise Violation(f"不允许的路径形态：{path}")

    kind = _classify(s)
    if kind in ("unc", "rooted", "drive_relative"):
        raise Violation(f"不允许的路径形态：{path}")

    # 绝对路径才是生产形态（package_path 存的就是 artifacts.default_package_path 的
    # 绝对串）。相对路径按工作区解析，它必然落在 packages 之外，于是被拒。
    cand = _resolve(Path(s) if kind == "absolute"
                    else _data_root() / WORK / tid / s)
    if not _same_path(cand.parent, _resolve(package_root())) or \
            cand.name != f"task_{tid}.zip":
        raise Violation("结果包不是本任务自己的那个文件")
    return cand


__all__ = [
    "Violation",
    "resolve",
    "resolve_package",
    "regions",
    "package_root",
    "package_file",
    "WORK",
    "PACKAGES",
    "READ_ONLY_REGIONS",
    "DEFAULT_DATA_ROOT",
    "DEFAULT_PROTFORGE_ROOT",
]
