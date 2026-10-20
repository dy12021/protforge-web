"""guards.py 的测试（L2 文件边界）。

只依据 plan 的 Global Constraints 与 Task 4 两段。
绝不碰真实的 <数据盘根> 与 <ProtForge 数据区>——两个根都由环境变量指到 tmp_path。
sys.path 自带 server 目录，不依赖（也不去创建）别人可能正在写的 tests/conftest.py。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import guards  # noqa: E402


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    """把所有数据根指到 tmp_path，并在其下铺好四个区域（让 resolve 走真实磁盘语义）。"""
    web = tmp_path / "data"
    pf = tmp_path / "protforge"
    (web / "work" / "1").mkdir(parents=True)
    (web / "uploads" / "1").mkdir(parents=True)
    (web / "work" / "2").mkdir(parents=True)
    (pf / "outputs").mkdir(parents=True)
    (pf / "inputs").mkdir(parents=True)
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(web))
    monkeypatch.setenv("PROTFORGE_DATA", str(pf))
    return web, pf


# --- plan Step 1 的原始用例 -------------------------------------------------------

ESCAPE_FORMS = [
    r"..\..\..\Windows\System32",
    r"C:/Windows/System32",
    r"C:foo\bar",  # 驱动器相对：不以 \ 开头，startswith 拦不住，但确实能逃出当前目录
    r"\\?\C:\Windows",
    r"\\server\share\x",
]


@pytest.mark.parametrize("bad", ESCAPE_FORMS)
def test_escape_forms_rejected(bad):
    with pytest.raises(guards.Violation):
        guards.resolve(bad, task_id=1)


def test_write_to_outputs_rejected_but_read_ok():
    out = guards.regions(1)["outputs"]
    assert guards.resolve(str(out / "x.pdb"), 1, write=False)
    with pytest.raises(guards.Violation):
        guards.resolve(str(out / "x.pdb"), 1, write=True)


def test_work_is_writable():
    p = guards.resolve("sub/result.pdb", 1, write=True)
    assert guards.regions(1)["work"].resolve() in p.parents


# --- 补充形态 ---------------------------------------------------------------------

EXTRA_ESCAPE_FORMS = [
    r"C:\Windows\System32",
    r"D:foo",  # 另一个驱动器的相对路径
    "C:",  # 只有盘符
    r"C:foo/bar",  # 驱动器相对 + 正斜杠
    r"\\.\C:\Windows",  # 设备路径
    r"\\?\UNC\server\share",  # 扩展 UNC
    r"\Windows\System32",  # 根相对
    "/etc/passwd",  # 根相对（正斜杠）
    "//server/share/x",  # UNC（正斜杠）
    r"..\..\..",
    "..",
    r"sub\..\..\escape",
    r"..\..\..\Windows\System32\config",
]


@pytest.mark.parametrize("bad", EXTRA_ESCAPE_FORMS)
@pytest.mark.parametrize("write", [False, True])
def test_more_escape_forms_rejected(bad, write):
    with pytest.raises(guards.Violation):
        guards.resolve(bad, task_id=1, write=write)


def test_null_byte_rejected():
    with pytest.raises(guards.Violation):
        guards.resolve("a\x00b", 1)


# --- 区域权限 ---------------------------------------------------------------------

@pytest.mark.parametrize("name", ["uploads", "outputs", "inputs"])
def test_read_only_regions_readable_not_writable(name):
    base = guards.regions(1)[name]
    target = base / "x.pdb"
    assert guards.resolve(str(target), 1) == target.resolve()
    with pytest.raises(guards.Violation):
        guards.resolve(str(target), 1, write=True)


def test_read_only_violation_names_region():
    with pytest.raises(guards.Violation) as e:
        guards.resolve(str(guards.regions(1)["uploads"] / "x.pdb"), 1, write=True)
    assert "uploads" in str(e.value)


def test_other_task_work_dir_rejected(roots):
    """task 1 不能碰 task 2 的工作区。"""
    other = roots[0] / "work" / "2" / "x.pdb"
    with pytest.raises(guards.Violation):
        guards.resolve(str(other), 1)
    with pytest.raises(guards.Violation):
        guards.resolve(str(other), 1, write=True)
    with pytest.raises(guards.Violation):
        guards.resolve(r"..\2\x.pdb", 1, write=True)


def test_task_id_cannot_be_used_for_traversal():
    with pytest.raises(guards.Violation):
        guards.regions("../../evil")


# --- 解析细节 ---------------------------------------------------------------------

def test_dot_segments_normalized():
    base = guards.regions(1)["work"].resolve()
    assert guards.resolve("./sub/../b.pdb", 1, write=True) == base / "b.pdb"
    assert guards.resolve(".", 1, write=True) == base
    assert guards.resolve("", 1) == base


def test_write_into_new_subdir_allowed():
    base = guards.regions(1)["work"].resolve()
    p = guards.resolve("a/b/c/out.pdb", 1, write=True)
    assert p == base / "a" / "b" / "c" / "out.pdb"
    assert not p.exists()  # 守卫不建目录，纯路径判定


def test_env_var_in_path_expanded(roots):
    web = roots[0]
    os.environ["PFW_TEST_ROOT"] = str(web)
    try:
        p = guards.resolve(r"%PFW_TEST_ROOT%\work\1\x.pdb", 1, write=True)
    finally:
        del os.environ["PFW_TEST_ROOT"]
    assert p == (web / "work" / "1" / "x.pdb").resolve()


# --- 8.3 短名与 junction：必须先 resolve 展开再判定 ---------------------------------

def _short_path(p: Path) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
    return buf.value if 0 < n < 1024 else ""


def test_8_3_short_name_expanded_before_judgement(roots):
    pf = roots[1]
    outputs = pf / "outputs"
    short = _short_path(outputs)
    if not short or Path(short) == outputs:
        pytest.skip("该卷未启用 8.3 短名创建，无法构造短名用例")
    # 短名访问 outputs：读通、写仍拒 → 说明短名被展开后再判只读
    assert guards.resolve(str(Path(short) / "x.pdb"), 1) == (outputs / "x.pdb").resolve()
    with pytest.raises(guards.Violation):
        guards.resolve(str(Path(short) / "x.pdb"), 1, write=True)
    # 短名不能当逃逸通道：短名根之上/之下的兄弟目录仍然越界
    with pytest.raises(guards.Violation):
        guards.resolve(str(Path(short).parent / "work"), 1, write=True)


def test_junction_out_of_work_rejected(roots):
    web = roots[0]
    target = web / "outside"
    target.mkdir()
    link = web / "work" / "1" / "escape"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip(f"无法创建 junction: {made.stderr or made.stdout}")
    with pytest.raises(guards.Violation):
        guards.resolve("escape/x.pdb", 1, write=True)


# --- 布局 -------------------------------------------------------------------------

def test_regions_layout(roots):
    web, pf = roots
    r = guards.regions(1)
    assert set(r) == {"work", "uploads", "outputs", "inputs"}
    assert r["work"] == web / "work" / "1"
    assert r["uploads"] == web / "uploads" / "1"
    assert r["outputs"] == pf / "outputs"
    assert r["inputs"] == pf / "inputs"


def test_regions_follow_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(tmp_path / "elsewhere"))
    r = guards.regions(7)
    assert r["work"] == tmp_path / "elsewhere" / "work" / "7"
    assert r["uploads"] == tmp_path / "elsewhere" / "uploads" / "7"


def test_default_roots_are_portable(monkeypatch):
    """**默认路径必须跟着仓库走**，不能写死某台机器的盘符。

    写死的话，公开出去的仓库就带着作者本机的磁盘布局；而换台机器 clone 下来
    还会把数据写到一个莫名其妙的位置。真实部署用环境变量指过去
    （本机的写法在仓库里的 .env，那份不入版本库）。
    """
    monkeypatch.delenv("PROTFORGE_WEB_DATA_ROOT", raising=False)
    monkeypatch.delenv("PROTFORGE_DATA", raising=False)
    r = guards.regions(1)
    repo = Path(guards.__file__).resolve().parent.parent
    assert r["work"] == repo / "data" / "work" / "1"
    assert r["uploads"] == repo / "data" / "uploads" / "1"
    assert r["outputs"] == repo.parent / "protforge" / "data" / "outputs"
    assert r["inputs"] == repo.parent / "protforge" / "data" / "inputs"


def test_violation_is_an_exception():
    assert issubclass(guards.Violation, Exception)
