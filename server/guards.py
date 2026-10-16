"""文件路径守卫（L2 文件边界）。

把 agent 能碰的文件限制在四个区域里：

    data\\work\\<task_id>\\      读写
    data\\uploads\\<task_id>\\   只读（原始上传不可篡改）
    ../protforge/data\\outputs\\     只读（仅对 agent 的文件工具）
    ../protforge/data\\inputs\\      只读

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

# plan Global Constraints 的字面值，环境变量只是覆盖，不是改默认。
DEFAULT_DATA_ROOT = r"data"
DEFAULT_PROTFORGE_ROOT = r"../protforge/data"

WORK = "work"
READ_ONLY_REGIONS = ("uploads", "outputs", "inputs")


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


__all__ = [
    "Violation",
    "resolve",
    "regions",
    "WORK",
    "READ_ONLY_REGIONS",
    "DEFAULT_DATA_ROOT",
    "DEFAULT_PROTFORGE_ROOT",
]
