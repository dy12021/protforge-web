"""产物发现覆盖「任务工作目录」的钉死项。

这一路的由来：联调实测 agent 把结果写进 work/<id>/，而 discover 只扫 ProtForge 的
outputs 根——那个目录是空的，于是任务 done 却没有 zip 可下载。

行为与 outputs 根那一路的关键差别：work/<id>/ **每个任务独占**，里面的东西必然属于
这个任务，不需要前缀或时间窗去猜，所以一律 verified=1、可直接下载。
"""
from __future__ import annotations

import zipfile

import pytest

import artifacts
import auth
import db
import store


@pytest.fixture
def owner():
    return auth.create_user("owner1", "password-123", role="user")


@pytest.fixture
def work_outputs(monkeypatch, tmp_path):
    """ProtForge 的 outputs 根指到临时目录，返回 (outputs_root, work_dir)。"""
    root = tmp_path / "protforge-data" / "outputs"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROTFORGE_DATA", str(tmp_path / "protforge-data"))
    work = db.data_root() / "work"
    return root, work


def _task(owner):
    return store.create_task(title="t", brief="b", owner_id=owner["id"])["id"]


def _write_work_file(work, tid, name, body="结果\n"):
    d = work / str(tid)
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(body, encoding="utf-8")
    return f


def test_work_dir_files_are_discovered_and_downloadable(owner, work_outputs):
    _root, work = work_outputs
    tid = _task(owner)
    _write_work_file(work, tid, "sasa_by_atom_type.txt", "CB 590.4\n")
    _write_work_file(work, tid, "per_residue.json", "{}\n")

    found = artifacts.discover(tid)

    by_name = {r["name"]: r for r in found}
    assert "sasa_by_atom_type.txt" in by_name, by_name
    assert "per_residue.json" in by_name, by_name
    assert by_name["sasa_by_atom_type.txt"]["source"] == "workdir"
    # 工作目录是任务独占的，不需要管理员确认就能下载
    assert by_name["sasa_by_atom_type.txt"]["verified"] == 1
    names = [r["name"] for r in artifacts.downloadable(tid)]
    assert "sasa_by_atom_type.txt" in names, names


def test_work_dir_scan_excludes_non_artifacts(owner, work_outputs):
    """模型配置、需求原文、打包参数都不是产物，绝不能跟着 zip 发出去。"""
    _root, work = work_outputs
    tid = _task(owner)
    d = work / str(tid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "brief.md").write_text("需求原文\n", encoding="utf-8")
    (d / "pack_spec.json").write_text("{}\n", encoding="utf-8")
    (d / "result.txt").write_text("真产物\n", encoding="utf-8")
    # 模型配置目录：里面有 auth.json / models.json
    pi = d / ".pi-agent"
    pi.mkdir(parents=True, exist_ok=True)
    (pi / "models.json").write_text('{"providers":{}}\n', encoding="utf-8")
    (pi / "auth.json").write_text("{}\n", encoding="utf-8")

    found = {r["name"] for r in artifacts.discover(tid)}

    assert "result.txt" in found
    for banned in ("brief.md", "pack_spec.json"):
        assert banned not in found, f"{banned} 不该被当成产物"
    assert not any(n.startswith(".pi-agent") for n in found), found


def test_nested_work_dir_files_are_found(owner, work_outputs):
    """结果放在子目录里也要认，不能只扫顶层。"""
    _root, work = work_outputs
    tid = _task(owner)
    sub = work / str(tid) / "tables"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "residue.csv").write_text("a,b\n", encoding="utf-8")

    found = {r["name"]: r for r in artifacts.discover(tid)}

    # 登记名是压平的（store.artifacts.add 禁止 name 里带分隔符，它会变成 zip 的
    # arcname，放行等于开路径穿越）；目录结构由打包时按真实相对路径补回。
    assert "residue.csv" in found, found
    artifacts.discover(tid)
    out = artifacts.package(tid)
    with zipfile.ZipFile(out) as z:
        assert "tables/residue.csv" in z.namelist(), z.namelist()


def test_work_dir_files_can_be_packaged(owner, work_outputs):
    """散文件也要能打进 zip——package 原来只认目录。"""
    _root, work = work_outputs
    tid = _task(owner)
    src_file = _write_work_file(work, tid, "summary.txt", "算完了\n")
    artifacts.discover(tid)

    out = artifacts.package(tid)

    with zipfile.ZipFile(out) as z:
        assert "summary.txt" in z.namelist(), z.namelist()
        # 跟磁盘上的字节一致（Windows 上 write_text 写的是 CRLF，别拿 \n 去比）
        assert z.read("summary.txt") == src_file.read_bytes()


def test_discover_is_idempotent_on_work_dir(owner, work_outputs):
    """重扫不能重复登记，更不能把已确认的结论冲掉。"""
    _root, work = work_outputs
    tid = _task(owner)
    _write_work_file(work, tid, "a.txt")
    first = artifacts.discover(tid)
    second = artifacts.discover(tid)

    assert [r["id"] for r in first] == [r["id"] for r in second]
    assert len(store.artifacts.list(tid)) == 1


def test_other_tasks_work_dir_is_never_scanned(owner, work_outputs):
    """A 任务的产物绝不能出现在 B 任务里——工作目录是唯一的隔离保证。"""
    _root, work = work_outputs
    a = _task(owner)
    b = _task(owner)
    _write_work_file(work, a, "only_a.txt")
    _write_work_file(work, b, "only_b.txt")

    names_b = {r["name"] for r in artifacts.discover(b)}

    assert "only_b.txt" in names_b
    assert "only_a.txt" not in names_b, "串任务了"


def test_virtualenv_is_not_an_artifact(owner, work_outputs):
    """agent 为了算这一行自己装的 venv **不是交付物**。

    真机事故（2026-10-20）：一行算完打包出来 94 MB，里面九成是
    ``venv_calc/Lib/site-packages`` 里的 wheel（3489 个文件、260 MB）。
    一批 61 行按这个打法的包加起来是几个 GB 的重复环境——用户下完还得自己删。

    判据用**结构标记**（``pyvenv.cfg``），不用目录名：名字可以随便起，
    有 pyvenv.cfg 的才是虚拟环境。
    """
    _root, work = work_outputs
    tid = _task(owner)
    d = work / str(tid)
    site = d / "venv_calc" / "Lib" / "site-packages" / "numpy"
    site.mkdir(parents=True, exist_ok=True)
    (d / "venv_calc" / "pyvenv.cfg").write_text("home = C:/Python\n", encoding="utf-8")
    (site / "__init__.py").write_text("# numpy\n", encoding="utf-8")
    (site / "core.py").write_text("# core\n", encoding="utf-8")
    _write_work_file(work, tid, "deliverables.txt", "真结果\n")

    found = {r["name"] for r in artifacts.discover(tid)}

    assert "deliverables.txt" in found, found
    assert "__init__.py" not in found, "虚拟环境里的文件被当成产物了"
    assert "core.py" not in found, found
    assert not any("numpy" in n for n in found), found

    # 打包出来的 zip 里也不能有
    out = artifacts.package(tid)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert "deliverables.txt" in names, names
    assert not any(n.startswith("venv_calc/") for n in names), names


def test_pycache_is_not_an_artifact(owner, work_outputs):
    """编译出来的字节码不是交付物。"""
    _root, work = work_outputs
    tid = _task(owner)
    d = work / str(tid) / "scripts" / "__pycache__"
    d.mkdir(parents=True, exist_ok=True)
    (d / "fold.cpython-313.pyc").write_bytes(b"\x00\x01")
    _write_work_file(work, tid, "fold.py", "print(1)\n")

    found = {r["name"] for r in artifacts.discover(tid)}

    assert "fold.py" in found, found
    assert "fold.cpython-313.pyc" not in found, found
