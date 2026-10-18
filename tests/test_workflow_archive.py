"""回写 xlsx：原子替换、被占降级、越界记失败（规格 7.1 / 7.2 / 7.3）。

openpyxl **不支持增量写**：每次都是整表 load → 改几格 → 整表 save。
所以「每归档一行写一次」的代价是 N 行 = N 次整表重写，这个任务不做优化，
但必须保证：一次慢重写不会拖死调度（Task 5 负责隔离），
以及被 Excel 占住时不重试（规格 7.1）。

末尾三条是 brief 的 10 条围不到、但规格明确要求的行为（标 ``[补充]``）：
缺 ``<dimension>`` 的表、写崩一半、**空 jobs 也不许碰文件**。
"""
from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

import store
import workflow


def _src(tmp_path, rows=8):
    p = Path(tmp_path) / "source.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "序列", "状态", "说明", "原因", "产物"])
    for i in range(rows):
        ws.append([f"ID{i}", f"SEQ{i}", "", "", "", ""])
    wb.save(p)
    return p


def test_write_back_fills_cells(tmp_path):
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    workflow.write_back(src, dest, "S1", 1, [
        (0, workflow.OutputCol(2, "status"), "done"),
        (0, workflow.OutputCol(3, "summary"), "算完了"),
    ])
    ws = load_workbook(dest)["S1"]
    assert ws.cell(row=2, column=3).value == "done"
    assert ws.cell(row=2, column=4).value == "算完了"


def test_write_back_preserves_source_columns(tmp_path):
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    workflow.write_back(src, dest, "S1", 1,
                        [(0, workflow.OutputCol(2, "status"), "done")])
    ws = load_workbook(dest)["S1"]
    assert ws.cell(row=2, column=1).value == "ID0"
    assert ws.cell(row=3, column=1).value == "ID1"


def test_write_back_multiple_rows(tmp_path):
    """归档 pass 必须能把一批多行一次写完，不许只处理第一行就 break。"""
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    workflow.write_back(src, dest, "S1", 1, [
        (0, workflow.OutputCol(2, "status"), "done"),
        (1, workflow.OutputCol(2, "status"), "failed"),
        (2, workflow.OutputCol(2, "status"), "done"),
    ])
    ws = load_workbook(dest)["S1"]
    assert [ws.cell(row=r, column=3).value for r in (2, 3, 4)] == \
        ["done", "failed", "done"]

def test_write_back_is_atomic(tmp_path):
    """写一半时下载，拿到的必须是完整的旧版或新版，不能是半个文件。"""
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    for i in range(3):
        workflow.write_back(src, dest, "S1", 1,
                            [(i, workflow.OutputCol(2, "status"), "done")])
    # 注意 glob 的形状：临时文件叫 result.tmp.xlsx（**不是** result.tmp）。
    # 写成 glob("*.tmp") 的话这条断言恒真——临时文件根本不在匹配范围内，
    # 于是清理写错了也照样绿。这正是「断言恒真」最常见的一种伪装。
    leftovers = list(Path(tmp_path).glob("*.tmp.xlsx"))
    assert leftovers == [], f"临时文件没清掉：{leftovers}"
    assert load_workbook(dest)["S1"].cell(row=4, column=3).value == "done"


def test_write_back_raises_permissionerror_when_locked(tmp_path, monkeypatch):
    """文件被占 → 抛 PermissionError，**调用方降级**，这里不重试。

    重试到调度 tick 里的话，工作流会被一个文件锁卡住，而用户看到的
    只是「停在第 N 行」。
    """
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)

    import openpyxl
    real_save = openpyxl.Workbook.save

    def boom(self, path):
        raise PermissionError(13, "另一个程序正在使用此文件")

    monkeypatch.setattr(openpyxl.Workbook, "save", boom)
    with pytest.raises(PermissionError):
        workflow.write_back(src, dest, "S1", 1,
                            [(0, workflow.OutputCol(2, "status"), "done")])
    monkeypatch.setattr(openpyxl.Workbook, "save", real_save)
    # 表没被破坏：还是旧版。
    # 空单元格读回来是 None 而不是 ""（实测 openpyxl 3.1.5：写入的 "" 在
    # 保存时按空单元格落盘，read 回来是 None），所以两种都算「没被动过」。
    assert load_workbook(dest)["S1"].cell(row=2, column=3).value in ("", None)


def test_write_back_out_of_range_is_reported(tmp_path):
    """目标行不存在 → 记一条失败原因，**不抛**。

    抛异常会经调度器的 except 变成一行静默日志，工作流悄悄卡住。
    """
    src = _src(tmp_path, rows=2)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    with pytest.raises(store.Invalid):
        workflow.write_back(src, dest, "S1", 1,
                            [(99, workflow.OutputCol(2, "status"), "done")])


def test_write_back_no_jobs_is_noop(tmp_path):
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    workflow.write_back(src, dest, "S1", 1, [])
    assert dest.exists()


# --- 上传落盘：staging -> 定名（规格 4.5 ①③）-------------------------------

def test_staging_path_is_not_user_controlled(tmp_path):
    p = workflow.staging_path(Path(tmp_path))
    assert p.parent.name == "staging"
    assert p.suffix == ".xlsx"


def test_finalize_moves_to_fixed_name(tmp_path):
    st = workflow.staging_path(Path(tmp_path))
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_bytes(b"PK\x03\x04 fake")
    final = workflow.finalize_upload(Path(tmp_path), st, 7)
    assert final.name == "source.xlsx"
    assert final.parent.name == "7"
    assert not st.exists()


# --- [补充] brief 的 10 条围不到、但规格要求的行为 ---------------------------

def _without_dimension(p: Path) -> Path:
    """造一个缺 ``<dimension>`` 元素的 xlsx（与 test_workflow_xlsx 同法）。

    非 Excel 工具（WPS 另存、Google Sheets 导出、部分生成库）写出来的 xlsx
    真的会缺这个元素，openpyxl 读不出 max_column / max_row。
    """
    out = p.with_name(p.stem + "_nodim.xlsx")
    with zipfile.ZipFile(p) as src:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    xml = data.decode("utf-8")
                    stripped = re.sub(r"<dimension[^>]*/>", "", xml, count=1)
                    # 夹具自证：元素名若变了，这里会静默造出一个正常的表
                    assert stripped != xml, "夹具没找到 <dimension>，围不到东西"
                    data = stripped.encode("utf-8")
                dst.writestr(item, data)
    return out


def test_write_back_reports_dimensionless_table_as_invalid(tmp_path):
    """缺 ``<dimension>`` 的表 → **沿用 Task 3 的 store.Invalid**，不是 TypeError。

    write_back 内部调 read_source，不许自己另开一条读表路径（那会把 Task 3
    修好的用户可见文案又漏回 500）。越界同理：抛 Invalid 之后调用方才有
    「记失败原因」的素材，吞掉就是静默丢一行。
    """
    src = _without_dimension(_src(tmp_path))
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    # 先证夹具真的造出了目标状态，否则下面围的是一个正常表
    wb = load_workbook(src, read_only=True, data_only=True)
    try:
        assert wb[wb.sheetnames[0]].max_column is None
        assert wb[wb.sheetnames[0]].max_row is None
    finally:
        wb.close()

    with pytest.raises(store.Invalid):
        workflow.write_back(src, dest, "S1", 1,
                            [(0, workflow.OutputCol(2, "status"), "done")])
    # 抛错发生在写盘之前：不许留下临时文件，也不许碰 dest
    assert list(Path(tmp_path).glob("*.tmp.xlsx")) == []
    assert load_workbook(dest)["S1"].cell(row=2, column=1).value == "ID0"


def test_write_back_keeps_old_file_when_save_crashes_midway(tmp_path, monkeypatch):
    """save 写到一半崩 → dest 还是**完整旧版**，不是半个文件。

    ``test_write_back_is_atomic`` 只在调用**结束后**看结果，围不到
    「直接 wb.save(dest)」这条退化路：那样写同样不残留临时文件、结果同样对，
    测试照绿，可用户下载时随时可能拿到半个文件。要围住它只能在崩的那一瞬
    检查 dest 本身。
    """
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)

    import openpyxl

    def half_written(self, path):
        Path(path).write_bytes("PK\x03\x04 写到一半".encode("utf-8"))
        raise OSError(28, "设备未就绪")

    monkeypatch.setattr(openpyxl.Workbook, "save", half_written)
    with pytest.raises(OSError):
        workflow.write_back(src, dest, "S1", 1,
                            [(0, workflow.OutputCol(2, "status"), "done")])
    ws = load_workbook(dest)["S1"]
    assert ws.cell(row=2, column=1).value == "ID0"
    assert ws.cell(row=2, column=3).value in ("", None)


def test_write_back_empty_jobs_does_not_touch_dest(tmp_path):
    """空 jobs **连碰都不碰** dest。

    openpyxl 的 save 永远是整表重写（见模块头注），所以「空 jobs 也走一遍
    load+save」在 1000 行的表上就是白扔一秒 CPU，而 brief 的
    ``test_write_back_no_jobs_is_noop`` 只断言 ``dest.exists()``——那对
    「重写了 1000 行」和「什么都没做」一视同仁，恒真。围在 mtime 上。
    """
    src = _src(tmp_path)
    dest = Path(tmp_path) / "result.xlsx"
    shutil.copyfile(src, dest)
    os.utime(dest, (946684800, 946684800))      # 2000-01-01：假装很久没动过
    before = dest.stat().st_mtime
    workflow.write_back(src, dest, "S1", 1, [])
    assert dest.stat().st_mtime == before, "空 jobs 也把 dest 整表重写了一遍"


def test_staging_path_names_do_not_collide(tmp_path):
    """staging 名字**自己生成**：两次调用不能撞名。

    撞名 = 后一个上传把前一个还没解析的文件覆盖掉，而用户只会看到
    「第一个文件解析成了第二个的内容」。
    """
    a = workflow.staging_path(Path(tmp_path))
    b = workflow.staging_path(Path(tmp_path))
    assert a != b
