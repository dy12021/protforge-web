"""读表 / 取值 / 渲染，全部是纯函数，不碰数据库（规格 4.5 / 5.1 / 5.2 / 5.3）。

本任务最容易写错的三处，各有一条专门的用例：

- **公式必须回退**：openpyxl 自己写的公式，用 ``data_only=True`` 读回来是 None。
  不回退的表现是「整列提示词全是 None，而任务照跑不误，只是算了个空」。
- **全空数据行要剔除**：真实 Excel 尾部常有一两行格式残留，``ws.max_row``
  会把它们算成数据行——不剔的话工作流会跑出一堆「输入全空」的行任务。
- **context 列必须整列恒定**：不恒定就报错，不能悄悄取第一行。
- **整表只许读一遍**：read_only 工作簿上按行号或按列号定位都要重开 XML 流、
  从流头重扫。逐行定位是 O(n^2)（800 行 19.8 秒），逐格 ``cell()`` 更狠
  （800 行 253.85 秒）——而 Task 5 每放行一行就调一次 read_source。
  这条数**重开流的次数**，不测时间（CI 上必抖）：开 N 个工作簿就只许开
  N 次流——上界跟着实际工作簿数走，不写死，免得加工作簿时误红一个正确的改动。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet._read_only import ReadOnlyWorksheet

import store
import workflow


def _wb(tmp_path, rows, name="t.xlsx"):
    p = Path(tmp_path) / name
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    for r in rows:
        ws.append(list(r))
    wb.save(p)
    return p


# --- 上传校验（规格 4.5 ②）-------------------------------------------------

def test_check_upload_accepts_real_xlsx(tmp_path):
    p = _wb(tmp_path, [["编号", "序列"], ["1", "AAAA"]])
    workflow.check_upload("表.xlsx", p.read_bytes())      # 不抛即通过


def test_check_upload_rejects_empty():
    """空文件这条分支只能用**错误文案**围栏：`b""` 是唯一能进这个分支的载荷，
    而它必然被后面的魔数检查再拦一次——所以删掉空文件检查后，
    报错会从「文件是空的」变成「不是 xlsx」，断言依然要红。"""
    with pytest.raises(store.Invalid, match="空的"):
        workflow.check_upload("空.xlsx", b"")


def test_check_upload_rejects_shell_by_extension():
    """载荷自带 zip 头：删掉扩展名检查就**根本不抛**，而不是换个报错。"""
    with pytest.raises(store.Invalid, match="只支持"):
        workflow.check_upload("evil.sh", b"PK\x03\x04" + b"junk")


def test_check_upload_rejects_wrong_magic():
    """扩展名对但内容不是 zip——只看扩展名等于没校验。"""
    with pytest.raises(store.Invalid):
        workflow.check_upload("假.xlsx", b"not a zip at all")


def test_check_upload_rejects_oversize():
    with pytest.raises(store.Invalid):
        workflow.check_upload("大.xlsx", b"PK\x03\x04" + b"0" * (workflow.UPLOAD_MAX + 1))


def test_safe_display_name_strips_path():
    """落盘只用固定名 source.xlsx，但展示名要去掉路径分隔符。"""
    assert "/" not in workflow.safe_display_name("../../evil.xlsx")
    assert "\\" not in workflow.safe_display_name("..\\..\\evil.xlsx")
    assert len(workflow.safe_display_name("x" * 500 + ".xlsx")) <= 120


def test_safe_display_name_strips_control_chars():
    """控制字符必须剥掉：它会进日志、进 HTTP 头、也可能糊掉终端。"""
    got = workflow.safe_display_name("a\x00b\x07c\x1bd.xlsx")
    assert got == "abcd.xlsx", "控制字符没剥干净：%r" % got
    assert "<" not in workflow.safe_display_name('a<b>c.xlsx')


# --- 读表 -------------------------------------------------------------------

def test_read_source_basics(tmp_path):
    p = _wb(tmp_path, [["编号", "序列"], ["1", "AAA"], ["2", "BBB"]])
    v = workflow.read_source(p)
    assert v.sheet == "S1"
    assert v.headers == ["编号", "序列"]
    assert v.col_count == 2
    assert len(v.row_numbers) == 2


def test_read_source_drops_trailing_blank_rows(tmp_path):
    p = Path(tmp_path) / "t.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["编号", "序列"])
    ws.append(["1", "AAA"])
    ws.append([None, None])      # 真实表格尾部常有这种格式残留
    ws.append([None, "只有序列"])  # 首列空但整行不空：这是数据行，不能剔
    wb.save(p)
    v = workflow.read_source(p)
    assert v.row_numbers == [2, 4]
    # 内容用 row_values 验，不碰 preview——preview 有条数上限，
    # 拿它断言等于让别的用例也绑死在 PREVIEW_ROWS 上
    assert workflow.row_values(p, v, 1) == {0: "", 1: "只有序列"}


def test_read_source_header_row_is_configurable(tmp_path):
    p = _wb(tmp_path, [["说明行"], ["编号", "序列"], ["1", "AAA"]])
    v = workflow.read_source(p, header_row=2)
    assert v.headers == ["编号", "序列"]
    assert len(v.row_numbers) == 1
    # 行号从 1 起：header_row=0 时表头读成空 → 列名全塌成「列N」，
    # 真表头行还会被当成第一条数据行，不报错但整张表认错列
    with pytest.raises(store.Invalid, match="表头行号"):
        workflow.read_source(p, header_row=0)


def test_read_source_truncates_long_cells(tmp_path):
    long = "x" * (workflow.CELL_MAX + 500)
    p = _wb(tmp_path, [["编号", "备注"], ["1", long]])
    v = workflow.read_source(p)
    assert len(v.preview[0][1]) == workflow.CELL_MAX
    assert workflow.CELL_MAX == 8000      # 规格值，跟着常量走的断言验不到常量本身


def test_read_source_rejects_empty_sheet(tmp_path):
    p = Path(tmp_path) / "empty.xlsx"
    Workbook().save(p)
    with pytest.raises(store.Invalid):
        workflow.read_source(p)


def test_read_source_caps_preview_rows(tmp_path):
    """预览只留前 50 行，但 row_numbers 必须是**全部**数据行——
    少收会漏任务，多收会把几百行预览塞进前端。"""
    n = workflow.PREVIEW_ROWS + 10
    p = _wb(tmp_path, [["编号"]] + [[str(i)] for i in range(n)])
    v = workflow.read_source(p)
    assert len(v.row_numbers) == n
    # 写死 50 而不是只比 workflow.PREVIEW_ROWS：常量本身被改小的话，
    # 跟着常量走的断言会跟着一起变小，等于什么都没验。
    assert workflow.PREVIEW_ROWS == 50
    assert len(v.preview) == 50
    assert v.preview[0][0] == "0"
    assert v.preview[-1][0] == "49"
    assert v.row_numbers[-1] == n + 1


def test_read_source_names_unnamed_headers(tmp_path):
    """无名表头占位成「列N」，不能塌成空串——前端列表要拿它当列名。"""
    p = _wb(tmp_path, [["编号", None, None], ["1", "AAA", "CCC"]])
    v = workflow.read_source(p)
    assert v.headers == ["编号", "列2", "列3"]
    # 纯空白（"  "）同样占位成「列N」，不是空串：塌成空串会往前端塞一个
    # 空列名，用户看到的是一列表头里有一列是空的，还以为程序没读到。
    p2 = _wb(tmp_path, [["编号", "  "], ["1", "AAA"]], "ws.xlsx")
    assert workflow.read_source(p2).headers == ["编号", "列2"]
    # 空串、只有制表符也要占位——它们同样是「这一列没有表头」
    p3 = _wb(tmp_path, [["编号", ""], ["1", "AAA"]], "es.xlsx")
    assert workflow.read_source(p3).headers == ["编号", "列2"]
    p4 = _wb(tmp_path, [["编号", "\t"], ["1", "AAA"]], "tab.xlsx")
    assert workflow.read_source(p4).headers == ["编号", "列2"]


# --- 缺 <dimension> 的表（用户可见行为）---------------------------------------

def _without_dimension(p: Path) -> Path:
    """造一个**缺 ``<dimension>`` 元素**的 xlsx：解开 zip 改 XML 再重新打包。

    非 Excel 工具（WPS 另存、Google Sheets 导出、部分生成库）写出来的 xlsx
    真的会缺这个元素。openpyxl 只在 ``_get_size()`` 里解析它，元素不在时
    ``max_column`` 与 ``max_row`` **双双为 None**（同一次 parse_dimensions
    调用出来的），所以只判其中一个等于漏了另一个。
    """
    out = p.with_name(p.stem + "_nodim.xlsx")
    with zipfile.ZipFile(p) as src:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    xml = data.decode("utf-8")
                    stripped = re.sub(r"<dimension[^>]*/>", "", xml, count=1)
                    # 围栏围的是「读不出列数」这个状态；元素名若变了，
                    # 夹具会静默造出一个正常的表，下面 assert 会替我们喊出来
                    assert stripped != xml, "夹具没找到 <dimension>，围不到东西"
                    data = stripped.encode("utf-8")
                dst.writestr(item, data)
    return out


def test_read_source_reports_missing_dimension_as_invalid(tmp_path):
    """缺 ``<dimension>`` 要抛 ``store.Invalid``，不是 TypeError。

    HTTP 层会把 ``store.Invalid`` 转成 422 + 人话；TypeError 漏出去是
    **500 Internal Server Error**——用户上传一个非 Excel 工具生成的文件，
    看到的是「服务器错误」而不是「这张表读不了，请另存为 xlsx 再传」。
    """
    p = _without_dimension(_wb(tmp_path, [["编号", "序列"], ["1", "AAA"]]))
    # 先证夹具真的造出了目标状态：否则下面断言围的是一个正常表，空转。
    wb = load_workbook(p, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        assert ws.max_column is None, "夹具没生效：max_column=%r" % (ws.max_column,)
        assert ws.max_row is None, "夹具只清了列数，max_row=%r" % (ws.max_row,)
    finally:
        wb.close()

    with pytest.raises(store.Invalid) as ei:
        workflow.read_source(p)
    msg = str(ei.value)
    # 围**文案**而不只围异常类型：TypeError 那版即使换成别的消息，
    # 用户照样拿到 500——所以「说了什么、能怎么办」才是可验收的行为。
    assert "dimension" in msg, "没说清为什么读不了：%s" % msg
    assert "另存为" in msg, "没告诉用户该怎么办：%s" % msg


# --- 读表不许退化成按行/按格定位（性能围栏，规格 5.1）---------------------------

def _count_stream_opens(monkeypatch):
    """数 workflow 发起了多少次**定位**（每次定位重开一次 read_only XML 流）。

    计数点是 ``openpyxl.worksheet._read_only.ReadOnlyWorksheet._get_source``——
    openpyxl 每定位一次（不论按行还是按格）都调它一次：重开 archive 流 + 新建
    一个 WorkSheetParser 从流头重扫。

    **为什么不能只数 iter_rows**：read_only 工作表的 ``cell(row=, column=)``
    根本不经过 iter_rows（它内部直接调 ``_cells_by_row``），只围 iter_rows
    的话，把 read_source 改成逐格 ``cell()`` **四条围栏会全绿**——而那条路
    比逐行定位还慢两个数量级（实测 800 行 253.85 秒 vs 单遍 0.085 秒）。
    围在 ``_get_source`` 上，两条退化路径一次抓住。

    **扣掉 ``_get_size`` 的那一次**：``ReadOnlyWorksheet.__init__`` 会调
    ``_get_size()`` 探一次 ``<dimension>``，它也重开一次流，但那是「打开这个
    sheet」的固定开销，与行数、列数都无关。不扣的话每次计数都多个常数偏移，
    跟「开了几个工作簿」没法直接比。扣掉之后计数只剩「定位次数」，
    一个工作簿单遍读完就是 1——这样才能钉住不变式「**开 N 次工作簿就开
    N 次流**」，而不用把 2 写死当上限（配 ``_count_workbook_loads`` 用）。

    **这是 openpyxl 的私有 API，随版本可能改名或挪位置**（本次实测版本
    3.1.5）。升级 openpyxl 时这条围栏**要先验证仍能命中**再提交：
    ``test_stream_fence_hits_per_cell_cell_access`` 就是那个验证，它围的是
    计数点本身。计数点失效时下面几条会**恒绿**，比没有围栏更危险。

    **不测时间**——CI 上必然抖动。数次数才是确定的，且精确指向根因。
    """
    calls: list[int] = []
    real_source = ReadOnlyWorksheet._get_source
    real_size = ReadOnlyWorksheet._get_size
    probing: list[int] = [0]

    def probing_size(self):
        probing[0] += 1
        try:
            return real_size(self)
        finally:
            probing[0] -= 1

    def counted(self):
        if not probing[0]:
            calls.append(1)
        return real_source(self)

    monkeypatch.setattr(ReadOnlyWorksheet, "_get_size", probing_size)
    monkeypatch.setattr(ReadOnlyWorksheet, "_get_source", counted)
    return calls


def _count_workbook_loads(monkeypatch):
    """数 workflow 调了几次 ``load_workbook``——也就是**开了几个工作簿**。

    **为什么不再把 2 写死当上限**：``read_source`` 开两个工作簿
    （``data_only=True`` 取值 / ``False`` 取公式），所以 ``<= 2`` 恰好
    等于工作簿数——但那是**巧合式的硬编码**。将来有人加第三个工作簿
    （比如更细的容错、读 defined names），计数变 3 而上限还是 2，
    围栏会误红一个**正确的**改动。

    改成数 ``load_workbook`` 的次数，两件事就解耦了：不变式从
    「不许超过 2」变成「**每个工作簿只开一次流**」。加工作簿时
    两边一起涨，断言自动跟随；真正的退化（同工作簿定位两次、
    逐格 ``cell()``、逐行 ``iter_rows``）才红。

    另外各处都留一条 ``len(loads) >= 2``：只写 ``calls == loads`` 时，
    把两个工作簿砍成一个的话两边都变成 1，等式照样成立、围栏空转。
    """
    calls: list[int] = []
    real = workflow.load_workbook

    def counted(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(workflow, "load_workbook", counted)
    return calls


def _big_xlsx(tmp_path, n, name="big.xlsx"):
    """n 行数据：项目/批次/版本三列恒定（可当 context），序列列逐行变。"""
    rows = [["编号", "项目", "批次", "版本", "序列"]]
    rows += [[str(i + 1), "XBR", "B1", "v2", "SEQ%d" % (i + 1)]
             for i in range(n)]
    return _wb(tmp_path, rows, name)


def _wide_xlsx(tmp_path, cols, n, name):
    """cols 列 × n 行，用来验「表头不是按列逐个定位」。"""
    rows = [["表头%d" % (i + 1) for i in range(cols)]]
    rows += [["v%d-%d" % (i + 1, r) for i in range(cols)] for r in range(n)]
    return _wb(tmp_path, rows, name)


def test_stream_fence_hits_per_cell_cell_access(tmp_path, monkeypatch):
    """围栏的**正向对照**：计数点本身必须能抓住逐格 cell()。

    这条围的不是 workflow，是**围栏自己**。`_get_source` 是私有 API：升级
    openpyxl 之后它可能改名、被挪位置，或者 ReadOnlyWorksheet.cell() 不再
    经过它——那时下面四条会恒绿，而表已经慢到不能用了。
    这里直接数一遍逐格读取，开流次数必须与读取格数**逐个相等**，数错就红。
    """
    p = _big_xlsx(tmp_path, 20, "bad.xlsx")
    calls = _count_stream_opens(monkeypatch)
    wb = load_workbook(p, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        n = 0
        for r in range(1, ws.max_row + 1):
            for c in range(1, ws.max_column + 1):
                ws.cell(row=r, column=c)
                n += 1
    finally:
        wb.close()
    assert n == 21 * 5, "夹具行数变了，%d" % n
    assert len(calls) == n, (
        "计数点没抓住逐格 cell()：读了 %d 格，只数到 %d 次开流。" % (n, len(calls)))


def test_stream_fence_hits_per_row_iter_rows(tmp_path, monkeypatch):
    """反向对照：计数点也必须能抓住 iter_rows(min_row=r) 逐行定位。

    和上面那条一起，说明计数点覆盖的是两条退化路径，不只是其中一条。
    """
    p = _big_xlsx(tmp_path, 20, "bad2.xlsx")
    calls = _count_stream_opens(monkeypatch)
    wb = load_workbook(p, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        n = 0
        for r in range(1, ws.max_row + 1):
            list(ws.iter_rows(min_row=r, max_row=r, values_only=True))
            n += 1
    finally:
        wb.close()
    assert n == 21
    assert len(calls) == n, (
        "计数点没抓住逐行定位：读了 %d 行，只数到 %d 次开流。" % (n, len(calls)))


def test_read_source_scans_the_sheet_once(tmp_path, monkeypatch):
    calls = _count_stream_opens(monkeypatch)
    loads = _count_workbook_loads(monkeypatch)
    v = workflow.read_source(_big_xlsx(tmp_path, 100))
    assert len(v.row_numbers) == 100
    # 上界跟着实际工作簿数走，不是写死的 2
    assert len(loads) >= 2, "只开了 %d 个工作簿：公式回退那一份呢" % len(loads)
    assert len(calls) == len(loads), (
        "开了 %d 个工作簿却重开流 %d 次：每个工作簿只许单遍读完一次"
        % (len(loads), len(calls)))


def test_read_source_scan_count_does_not_grow_with_rows(tmp_path, monkeypatch):
    calls = _count_stream_opens(monkeypatch)
    loads = _count_workbook_loads(monkeypatch)
    workflow.read_source(_big_xlsx(tmp_path, 100, "small.xlsx"))
    small, small_loads = len(calls), len(loads)
    calls.clear()
    loads.clear()
    workflow.read_source(_big_xlsx(tmp_path, 800, "big.xlsx"))
    big, big_loads = len(calls), len(loads)
    assert small_loads >= 2
    assert small == small_loads, (
        "100 行：开了 %d 个工作簿却重开流 %d 次" % (small_loads, small))
    assert big_loads >= 2
    assert big == big_loads, (
        "800 行：开了 %d 个工作簿却重开流 %d 次" % (big_loads, big))
    assert big == small, "重开流次数随行数增长：100 行 %d 次 → 800 行 %d 次" % (small, big)


def test_read_source_header_shares_the_single_pass(tmp_path, monkeypatch):
    """表头不许另开一遍流逐列读——那是按列定位，同一类退化，只是短。

    原来的 ``wsv.cell(row=header_row, column=col)`` 每列重开一次流：
    40 列的表就是 40 次。列数本身拉不高单次耗时，但它是「按坐标定位」那颗
    种子——留着它，下一个人顺手改成 ``min_row=r`` 就是 O(n^2)。
    """
    calls = _count_stream_opens(monkeypatch)
    loads = _count_workbook_loads(monkeypatch)
    v5 = workflow.read_source(_wide_xlsx(tmp_path, 5, 100, "n5.xlsx"))
    narrow, narrow_loads = len(calls), len(loads)
    calls.clear()
    loads.clear()
    v40 = workflow.read_source(_wide_xlsx(tmp_path, 40, 100, "n40.xlsx"))
    wide, wide_loads = len(calls), len(loads)
    assert (v5.col_count, len(v5.headers)) == (5, 5)
    assert (v40.col_count, len(v40.headers)) == (40, 40)
    assert narrow_loads >= 2
    assert narrow == narrow_loads
    assert wide == wide_loads
    assert wide == narrow, (
        "40 列比 5 列多开了 %d 次流：表头还在逐列定位" % (wide - narrow))


def test_check_context_columns_scans_the_sheet_once(tmp_path, monkeypatch):
    """一列 context 就扫一次全表，三列也不能变成三遍全表。"""
    calls = _count_stream_opens(monkeypatch)
    loads = _count_workbook_loads(monkeypatch)
    p = _big_xlsx(tmp_path, 200)
    v = workflow.read_source(p)
    calls.clear()
    loads.clear()
    workflow.check_context_columns(p, v, [1, 2, 3])
    assert len(loads) >= 2, "只开了 %d 个工作簿" % len(loads)
    assert len(calls) == len(loads), (
        "开了 %d 个工作簿却重开流 %d 次：逐列逐行重新定位了" % (len(loads), len(calls)))


def test_check_context_columns_scan_count_does_not_grow(tmp_path, monkeypatch):
    calls = _count_stream_opens(monkeypatch)
    loads = _count_workbook_loads(monkeypatch)
    p1 = _big_xlsx(tmp_path, 100, "small.xlsx")
    v1 = workflow.read_source(p1)
    calls.clear()
    loads.clear()
    workflow.check_context_columns(p1, v1, [1, 2, 3])
    small, small_loads = len(calls), len(loads)
    calls.clear()
    loads.clear()
    p2 = _big_xlsx(tmp_path, 800, "big.xlsx")
    v2 = workflow.read_source(p2)
    calls.clear()
    loads.clear()
    workflow.check_context_columns(p2, v2, [1, 2, 3])
    big, big_loads = len(calls), len(loads)
    assert small_loads >= 2
    assert small == small_loads, (
        "100 行：开了 %d 个工作簿却重开流 %d 次" % (small_loads, small))
    assert big_loads >= 2
    assert big == big_loads, (
        "800 行：开了 %d 个工作簿却重开流 %d 次" % (big_loads, big))
    assert big == small, "重开流次数随行数增长：100 行 %d 次 → 800 行 %d 次" % (small, big)


# --- 取值 -------------------------------------------------------------------

def test_row_values_rejects_row_out_of_range(tmp_path):
    """两侧都要拦。上界漏了会 IndexError，下界漏了 -1 会**静默返回最后一行**。"""
    p = _wb(tmp_path, [["编号", "序列"], ["1", "AAA"], ["2", "BBB"]])
    v = workflow.read_source(p)
    with pytest.raises(store.Invalid, match="行号越界"):
        workflow.row_values(p, v, -1)
    with pytest.raises(store.Invalid, match="行号越界"):
        workflow.row_values(p, v, len(v.row_numbers))


def test_row_values_returns_one_row(tmp_path):
    p = _wb(tmp_path, [["编号", "序列"], ["1", "AAA"], ["2", "BBB"]])
    v = workflow.read_source(p)
    assert workflow.row_values(p, v, 1) == {0: "2", 1: "BBB"}


# --- 公式回退（规格 5.3）----------------------------------------------------

def test_value_falls_back_to_formula_text(tmp_path):
    p = Path(tmp_path) / "f.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["编号", "长度"])
    ws.append(["1", "=LEN(B1)"])       # openpyxl 自己写的公式，没有缓存值
    wb.save(p)
    v = workflow.read_source(p)
    got = workflow.row_values(p, v, 0)
    assert got[1] == "=LEN(B1)", "回退失效：提示词里会喂进 None"
    assert got[1] is not None


def test_value_prefers_cached_result(tmp_path):
    p = _wb(tmp_path, [["编号", "长度"], ["1", "42"]])
    v = workflow.read_source(p)
    assert workflow.row_values(p, v, 0)[1] == "42"


def test_read_source_preview_falls_back_to_formula_text(tmp_path):
    """read_source 里的公式回退是**另一处独立代码**，preview 会直接显示它。

    不回退的表现：预览里整列公式显示成空白，用户以为列是空的。
    """
    p = Path(tmp_path) / "f2.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "长度", "序列"])
    ws.append(["1", "=LEN(C1)", "AAA"])     # openpyxl 自己写的公式，没有缓存值
    wb.save(p)
    v = workflow.read_source(p)
    assert v.preview[0][1] == "=LEN(C1)", "回退失效：预览里是空白"
    assert v.preview[0][1].startswith("=")
    assert v.preview[0][2] == "AAA"
    assert v.row_numbers == [2]


def test_header_cell_falls_back_to_formula_text(tmp_path):
    """**表头**里的公式也要回退，它走的是同一次迭代的另一个分支。

    不回退的表现：列名塌成占位的「列1」，而同一张表的数据列有回退——
    用户看到的是「表头叫列1，下面那列明明有内容」。
    """
    p = Path(tmp_path) / "hf.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["=LEN(B1)", "b"])     # 表头里的公式，没有缓存值
    ws.append(["1", "AAA"])
    wb.save(p)
    v = workflow.read_source(p)
    assert v.headers == ["=LEN(B1)", "b"], "表头公式没回退：列名=%r" % (v.headers,)
    assert v.headers[0].startswith("="), "列名退化成占位名：%r" % (v.headers[0],)


def test_header_cell_wholly_blank_falls_back_before_naming(tmp_path):
    """回退和占位的顺序：公式回退先做，strip 后仍空才占位成「列N」。

    顺序反了的话，公式表头会先被判成空、占位成「列N」，等于没回退。
    """
    p = Path(tmp_path) / "hb.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["编号", "  "])         # 第 2 列：纯空白
    ws.append(["1", "AAA"])
    wb.save(p)
    v = workflow.read_source(p)
    assert v.headers == ["编号", "列2"]


# --- context 列恒定性（规格 5.1）--------------------------------------------

def test_context_column_must_be_constant(tmp_path):
    p = _wb(tmp_path, [["编号", "项目", "序列"],
                       ["1", "XBR", "AAA"], ["2", "XBR", "BBB"]])
    v = workflow.read_source(p)
    workflow.check_context_columns(p, v, [1])         # 恒定，不抛
    with pytest.raises(store.Invalid):
        workflow.check_context_columns(p, v, [2])     # 序列在变，当 context 要报错


def test_context_check_catches_change_after_second_row(tmp_path):
    """只比前两行的实现会让「第 3 行起才变」的表蒙混过关——5000 行的表里
    漏掉最后一行出错，用户拿到的就是一次静默的串味。"""
    p = _wb(tmp_path, [["编号", "项目"], ["1", "XBR"], ["2", "XBR"],
                       ["3", "XBR"], ["4", "XBR"], ["5", "ZZZ"]])
    v = workflow.read_source(p)
    with pytest.raises(store.Invalid, match="第 5 行"):
        workflow.check_context_columns(p, v, [1])


def test_context_check_ignores_blank_cells(tmp_path):
    """空单元格不算「变了」——否则整列只有一个值、其余空着反而报错。"""
    p = _wb(tmp_path, [["编号", "项目"], ["1", "XBR"], ["2", None]])
    v = workflow.read_source(p)
    workflow.check_context_columns(p, v, [1])


# --- 渲染 -------------------------------------------------------------------

def test_render_substitutes_slots():
    out = workflow.render("查 {{seq}}，项目 {{proj}}",
                          {"seq": "AAA", "proj": "XBR"})
    assert out == "查 AAA，项目 XBR"


def test_render_allows_repeated_slot():
    out = workflow.render("{{seq}} / {{seq}}", {"seq": "A"})
    assert out == "A / A"


def test_render_missing_slot_becomes_blank_not_error():
    """空单元格渲染成空串、不报错——空表是常态，不是用户的错。"""
    assert workflow.render("查 [{{seq}}]", {}) == "查 []"


def test_extract_slots_dedupes_and_strips():
    got = workflow.extract_slots("{{a}} {{ b }} {{a}}")
    assert got == ["a", "b"]


def test_validate_binding_rejects_unbound_slot():
    # 夹具里必须有一列 key：否则「必须有且只有一列标识列」会先抛，
    # 这条用例就变成被错误的分支满足了（撤掉下面的校验照样全绿）。
    cols = [{"col_index": 0, "role": "key"},
            {"col_index": 1, "role": "input", "slot": "seq"}]
    with pytest.raises(store.Invalid, match="没有列绑定"):
        workflow.validate_binding("查 {{seq}} 与 {{proj}}", cols)


def test_validate_binding_rejects_unused_column():
    cols = [{"col_index": 0, "role": "key"},
            {"col_index": 1, "role": "input", "slot": "seq"},
            {"col_index": 2, "role": "context", "slot": "没人用"}]
    with pytest.raises(store.Invalid, match="没用到"):
        workflow.validate_binding("查 {{seq}}", cols)


def test_validate_binding_accepts_healthy_binding():
    cols = [{"col_index": 0, "role": "key"},
            {"col_index": 1, "role": "input", "slot": "seq"},
            {"col_index": 2, "role": "context", "slot": "proj"},
            {"col_index": 3, "role": "output", "field": "summary"}]
    workflow.validate_binding("查 {{seq}}，项目 {{proj}}", cols)


def test_output_column_needs_a_known_field():
    cols = [{"col_index": 0, "role": "key"},
            {"col_index": 1, "role": "output", "field": "随便写的"}]
    with pytest.raises(store.Invalid, match="不认识"):
        workflow.validate_binding("x", cols)


def test_validate_binding_needs_exactly_one_key_column():
    cols = [{"col_index": 0, "role": "input", "slot": "seq"}]
    with pytest.raises(store.Invalid, match="标识列"):
        workflow.validate_binding("查 {{seq}}", cols)
