"""用户面板前端的**静态**检查。

**为什么只有静态检查**：这三个文件是零构建的原生 HTML/CSS/JS，没有打包器、
没有组件树，pytest 里没有任何东西能"渲染"它们——真渲染要么得引浏览器驱动
（违反零外链/零依赖），要么得把 DOM 结构复制一份到 Python 里（那就成了测副本，
改前端时不跟着变，测了等于没测）。

所以这里只守那些**错了不报错、只在页面上"看起来不对"**的坑——也就是用户已经
被坑过的那几类：

1. `[hidden]` 被 `display:flex` 盖掉 → 保存后输入框仍在，页面同一段内容出现两次
2. 外链（CDN 字体/图标库）→ CSP `default-src 'self'` 之下整页瘫掉
3. 界面口径出现"AI/助手/大模型" → 产品定位已拍板
4. 状态色缺一个 → 流转轨上有一站和别的站分不出来
5. 流转轨少了圆点 → 只剩一根线，读不出卡在第几站
6. 详情区开局就是编辑态 → 用户明确纠正过："默认就是编辑模式…跟调试模式似的"

规则本身是断言，样式本身是事实。改前端时请连这个文件一起读。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
# 单文件的旧别名，保留给可能从外部引用它们的用例；**下面的检查一律走
# _html()/_js()**，不然新文件（workflow.html/js）会整片落在检查范围外。
HTML = WEB / "index.html"
CSS = WEB / "style.css"
JS = WEB / "app.js"
# 工作流页是**新文件**，而这套检查原本只扫三个写死的文件——不把新文件加进
# _files()，它就完全在检查范围外：禁用词、外链、状态色几类一个都不查。
# 而工作流页必然要写「计算任务/计算进程」这类文案，正是最容易顺手写出
# 禁用词的地方（规格 8.4）。
_HTML_FILES = ["index.html", "workflow.html"]
_CSS_FILES = ["style.css"]
_JS_FILES = ["app.js", "workflow.js"]

# 界面禁用词。**"AI" 必须大小写敏感**：app.js 里全是 detail / chain / main 这类
# 小写词，用小写去查会误报一片。真正的风险是文案里混进大写的 AI。
BANNED_WORDS = ("AI", "助手", "大模型")


def _read(p: Path) -> str:
    assert p.exists(), f"缺文件：{p}"
    return p.read_text(encoding="utf-8")


def _html(name: str) -> str:
    return _read(WEB / name)


def _js(name: str) -> str:
    return _read(WEB / name)


def _files() -> dict[str, str]:
    out = {}
    for name in _HTML_FILES + _CSS_FILES + _JS_FILES:
        out[name] = _read(WEB / name)
    return out


# --- 零外链 / 零构建 -----------------------------------------------------


def test_no_external_resource_links():
    """只准引用同源的 style.css / app.js，外加 data: 空 favicon。

    CSP 头是 `default-src 'self'`，任何一个 http(s) 外链都会被浏览器当场拦掉，
    而且拦掉的表现是"图标不显示"这种静默降级，不报错。
    """
    for name in _HTML_FILES:
        html = _html(name)
        refs = re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', html)
        assert refs, f"{name} 一个资源引用都没有，八成是写坏了"
        for r in refs:
            assert not r.startswith(("http://", "https://", "//")), \
                f"{name} 外链资源：{r}"
            # 白名单里刻意只列这几个同源文件，**不放宽成「任意同源路径」**——
            # 放宽了等于放弃「首页只准引这几个」这条当初为了拦外链 CDN 而加的
            # 约束。index.html 是工作流页「回到看板」的入口，与 Step 6 的
            # 页面骨架一致。
            assert r in ("style.css", "app.js", "workflow.js", "workflow.html",
                         "index.html", "data:,"), f"{name} 意外的引用：{r}"


def test_no_css_import_or_webfont():
    """CSS 里不许 @import 拉远端样式或字体。"""
    css = _read(CSS)
    assert "@import" not in css
    for host in ("fonts.googleapis", "fonts.gstatic", "cdn.", "unpkg", "jsdelivr"):
        assert host not in css, f"CSS 里出现了外链来源：{host}"


def test_user_ui_files_match_disk_both_ways():
    """白名单里有而磁盘上没有 → 红；磁盘上有而白名单里没有 → 红。

    后一种就是线上 404 而所有测试全绿的形状（规格 8.4）。
    """
    import sys
    if str(REPO / "server") not in sys.path:
        sys.path.insert(0, str(REPO / "server"))
    import app as svc

    whitelisted = set(svc.USER_UI_FILES)
    on_disk = {p.name for p in WEB.iterdir()
               if p.suffix in (".html", ".js", ".css")}
    assert whitelisted <= on_disk, f"白名单里有磁盘上没有的：{whitelisted - on_disk}"
    assert on_disk <= whitelisted, f"磁盘上有白名单里没有的：{on_disk - whitelisted}"


# --- 坑 1：[hidden] 必须显式压回 display:none ----------------------------


def test_hidden_attribute_is_forced_to_display_none():
    """凡是会被 JS 用 hidden 开关、又自己带 display 规则的元素，都必须有 [hidden] 伴生规则。

    **只写一条全局 [hidden]{display:none} 是不够的**：`.eb-edit{display:flex}`
    与 `[hidden]` 特异性相同（都 0,1,0），样式表里谁在后面谁赢——全局那条写在文件
    开头，会被后面的 display 整条盖掉。于是"存完回只读"时 textarea 仍然可见，
    页面上同一段内容出现两次。不报任何异常，只是"看起来没切换"。

    新增一个用 hidden 开关的元素时，往下面这个表里加一行。
    """
    css = _read(CSS)
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*;", css), (
        "缺全局 [hidden] { display: none } 兜底规则"
    )
    toggled = [
        ".eb-edit",        # 只读 <-> 编辑 两态的输入容器
        ".eb-view",        # 只读视图（编辑时让位）
        ".card-step",      # running 卡片的当前步骤行
        ".card-fail",      # failed 卡片的失败原因行
        ".drawer",         # 抽屉整体
        ".drawer-msg",     # 抽屉提示条
        ".board-msg",      # 看板顶部横幅
        ".login-error",    # 登录失败提示
        ".wf-pane",        # 工作流三步向导的步骤面板
        ".wf-colpick",     # 点表头弹出的角色选择层
    ]
    for sel in toggled:
        # 只要求出现 `选择器[hidden]` 即可，不要求紧跟 `{`：
        # 伴生规则经常合并成一条 `.a[hidden], .b[hidden] { … }`，只有最后一个
        # 选择器后面才直接是 `{`。
        assert re.search(re.escape(sel) + r"\[hidden\]", css), (
            f"{sel} 会被 hidden 切换却没有伴生的 {sel}[hidden] 规则"
        )


def test_edit_block_starts_hidden():
    """编辑容器开局必须是 hidden：抽屉详情默认只读。"""
    js = _js("app.js")
    m = re.search(r"function editable\((.*?)\n}", js, re.S)
    assert m, "找不到 editable()：只读/编辑两态的骨架没了"
    body = m.group(1)
    assert "edit.hidden = true" in body, "编辑容器开局可见 = 详情区默认就是编辑态"


# --- 坑 2/3：确认表与全局 Esc -------------------------------------------


def test_confirm_dialog_finish_is_guarded_and_unbinds():
    """<dialog> 复用：收尾必须走 answered 守卫的幂等 finish() 并解绑监听。

    否则第二次确认会被第一次的闭包直接 resolve 掉——那不是"取消"，是中止/删错。
    """
    js = _js("app.js")
    m = re.search(r"function confirmAction\((.*?)\n}", js, re.S)
    assert m, "找不到两步确认"
    body = m.group(1)
    assert "answered" in body, "确认表收尾没有幂等守卫"
    assert "removeEventListener" in body, "确认表收尾没有解绑监听"
    assert body.count("finish(") >= 3, "确认/取消/close 三条路径都要收敛到同一个 finish()"
    assert 'addEventListener("close"' in body, "确认表要监听 close 而不是 cancel"


def test_global_escape_ignores_open_dialog():
    """全局 Esc 遇 dialog[open] 一律不插手。

    否则确认表开着按 Esc：底下的抽屉一起关掉，而 cancel 事件又没正常走到，
    Promise 永远不 resolve，按钮卡在 disabled 再也点不动。
    """
    js = _js("app.js")
    assert 'dialog[open]' in js, "全局 Esc 没有给模态 dialog 让路"


def test_no_native_prompt_confirm_or_alert():
    """不用 window.prompt / confirm / alert：原生弹窗样式不受控且一关输入就没了。

    这是用户在创意面板上明确纠正过的第三件事。
    """
    for name in _JS_FILES:
        js = _js(name)
        for fn in ("window.prompt", "prompt(", "window.confirm", "confirm(",
                   "alert("):
            assert fn not in js, f"{name} 用了原生弹窗：{fn}"

# --- 界面口径 -----------------------------------------------------------


def test_no_banned_wording_in_any_web_file():
    """三个文件（含注释）都不许出现 AI / 助手 / 大模型。"""
    for name, text in _files().items():
        for w in BANNED_WORDS:
            assert w not in text, f"{name} 里出现了禁用词「{w}」"


def test_platform_wording_present():
    """反过来也要钉住正面口径：统一称「自动化计算平台」「计算任务」。"""
    text = "\n".join(_files().values())
    assert "自动化计算平台" in text
    assert "计算任务" in text


# --- 令牌与状态色 -------------------------------------------------------


def test_paper_tokens_present():
    """亮色纸面冷调（不是暖米色）+ 主文字，四件套齐全。"""
    css = _read(CSS).upper()
    for v in ("#EDF0F4", "#F6F8FA", "#FFFFFF", "#DBE1E9", "#15181D", "#69717E"):
        assert v in css, f"缺设计令牌 {v}"


def test_all_five_status_colors_present():
    """四站主链 + 失败，五个色值一个都不能少（少一个就有一站和别站分不出来）。"""
    css = _read(CSS).upper()
    for v in ("#8A93A2", "#D4860B", "#7B4DD6", "#1B9A67", "#D33F35"):
        assert v in css, f"缺状态色 {v}"


def test_status_colors_bound_to_css_variables():
    """五个色值必须挂在 :root 的变量上，JS 只引变量不写死色值。"""
    css = _read(CSS)
    for v in ("--st-queued", "--st-running", "--st-packaging", "--st-done", "--st-failed"):
        assert v in css, f"缺变量 {v}"
    js = _js("app.js")
    assert 'style.setProperty("--seg", "var(--st-' in js, "流转轨应当只引 CSS 变量"


# --- 流转轨 -------------------------------------------------------------


def test_rail_has_a_dot_per_station():
    """每一站都必须有圆点元素。

    缺了站就只剩一根线，读不出卡在第几站——那流转轨就没有存在意义。
    """
    css = _read(CSS)
    assert ".rail-dot" in css, "流转轨没有圆点元素"
    js = _js("app.js")
    assert "rail-dot" in js, "JS 没有为每一站生成圆点"


def test_rail_has_exactly_four_stations_and_excludes_failed():
    """轨只画四站；failed 是终点，不占一站（否则轨会读成五段进度条）。"""
    js = _js("app.js")
    m = re.search(r"const STATUS_ORDER\s*=\s*\[([^\]]*)\]", js)
    assert m, "找不到 STATUS_ORDER"
    stations = [x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()]
    assert stations == ["queued", "running", "packaging", "done"], stations


def test_failed_colors_only_the_current_station():
    """failed 时红只落在当前站，线不整条涂红。

    整线变红会丢掉"走到哪一步"，正是流转轨存在的意义。
    """
    js = _js("app.js")
    assert re.search(r"failed\s*&&\s*i\s*===\s*idx", js), (
        "failed 必须只给当前站染色（而不是整条线涂红）"
    )


def test_running_card_shows_step():
    """running 的卡片要在轨下方显示 task.step（后端已按 80 码点截断）。"""
    js = _js("app.js")
    assert re.search(r"t\.step|step\)\s*\{", js), "卡片没有渲染 step"
    assert "step" in js


# --- 抽屉 / 只读两态 ---------------------------------------------------


def test_drawer_starts_hidden():
    """抽屉开局关着，且 JS 在拿到数据前不主动打开。"""
    html = _html("index.html")
    m = re.search(r"<aside[^>]*>", html)
    assert m and 'id="drawer"' in m.group(0), "找不到抽屉"
    assert "hidden" in m.group(0), "抽屉开局就是打开的"


def test_drawer_full_keeps_a_reading_column():
    """全屏后内容仍收在居中阅读列（铺满一行中文难换行）。"""
    css = _read(CSS)
    assert re.search(r"max-width:\s*5\drem", css), "全屏态没有收窄成阅读列"


def test_save_uses_server_echoed_value():
    """保存后用服务端回填值重绘，不能拿本地输入值。

    服务端会规范化（去首尾空白、截断 step），不回填就会出现「存了但看着没变」。

    **不要按变量名或行距来钉。** 原来这条断言写的是"PATCH 的下一行出现
    done("、或字面量 "done(got."，于是它同时被两件无关的事弄坏：把响应包进
    detailTask 会多出两行，变量从 got 改名成 saved 又会让字面量对不上——
    而这两种改法都是**对的**。一条会因为正确重构而失败的断言，比没有断言更糟：
    它训练人把红当成噪声。

    现在钉的是真正的不变量：**同一个变量**先从 PATCH 的返回值来，随后被交给
    done() 重绘。中间隔着几行、变量叫什么，都不影响它成立。
    """
    js = _js("app.js")
    saved = re.compile(
        r"const\s+(\w+)\s*=\s*detailTask\(\s*await api\(\s*\"PATCH\".*?\);"
        r".*?done\(\s*\1\s*\.",
        re.S)
    hits = saved.findall(js)
    assert hits, (
        "保存后没有用服务端回填值重绘：找不到「PATCH 的返回值被交给 done()」"
        "这一对同一个变量的引用")

    # 反向断言：保存回调内部不能把**本地输入值**交给 done()。
    # 那是"存了但界面还显示你自己输入的那一份"的写法。
    # 只看保存回调：`}, () => done(value))` 是**取消**回调，那里回原值是对的，
    # 全文件扫会把正确的写法一起判成错的。
    for body in _save_callbacks(js):
        for local in ("done(ta.value)", "done(input.value)", "done(value)"):
            assert local not in body, (
                f"保存后拿本地值 {local} 重绘，服务端做的规范化（去空白、截断）"
                f"会被悄悄丢掉")


def _save_callbacks(js: str) -> list[str]:
    """取出所有含 PATCH 的 `async () => { … }` 保存回调的函数体。

    按花括号配对取，别用正则截——函数体里有 try/catch 与嵌套调用，
    朴素匹配会在函数中间截断，断言"函数体里没有 X"就变成了"我没看全"。
    """
    bodies = []
    for m in re.finditer(r"async\s*\(\s*\)\s*=>\s*\{", js):
        depth, i = 0, m.end() - 1
        while i < len(js):
            if js[i] == "{":
                depth += 1
            elif js[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = js[m.end():i]
        if 'api("PATCH"' in body or "api('PATCH'" in body:
            bodies.append(body)
    assert bodies, "找不到任何调用 PATCH 的保存回调，前端结构变了？"
    return bodies


# --- 数据层 -------------------------------------------------------------


def test_api_base_is_overridable_and_same_origin_by_default():
    """api() 的 base 走同源 /api，开发时可用 PROTFORGE_WEB_BASE 覆盖。"""
    js = _js("app.js")
    assert "PROTFORGE_WEB_BASE" in js
    assert '"/api"' in js


def test_token_is_bearer_in_header_not_cookie():
    """鉴权走 Authorization: Bearer（不放 Cookie），localStorage 取 token。"""
    js = _js("app.js")
    assert "Bearer" in js
    assert "localStorage" in js


def test_brief_upper_bound_is_enforced_client_side():
    """brief 上限 20000 字符，超出返回 422 —— 前端先挡住，别让用户白提交一次。

    断言必须**认准需求说明那个 textarea**：标题也有 maxlength（200），
    只找"第一个 maxlength"会把标题的当成 brief 的。
    """
    html = _html("index.html")
    m = re.search(r"<textarea[^>]*id=\"c-brief\"[^>]*>", html)
    assert m, "提交表单里没有需求说明输入框"
    bound = re.search(r"maxlength=\"(\d+)\"", m.group(0))
    assert bound, "需求说明输入框没有长度约束"
    assert int(bound.group(1)) >= 20000, "brief 的 maxlength 不到后端上限 20000"


def test_stream_is_read_with_fetch_not_eventsource():
    """Bearer 走请求头，EventSource 带不了 Authorization —— 必须用 fetch 读流。"""
    js = _js("app.js")
    assert "new EventSource" not in js, "EventSource 发不了 Authorization 头"
    assert "getReader" in js
    assert "Last-Event-ID" in js


@pytest.mark.parametrize("js_name,html_name",
                         [("app.js", "index.html"),
                          ("workflow.js", "workflow.html")])
def test_every_dom_id_the_script_touches_exists_in_the_html(
        js_name, html_name):
    """脚本引用的 id 必须在它自己那个 HTML 里（或是它自己动态挂上去的）。

    少一个 id 的话，`document.getElementById("x").oninput = ...` 就是
    null.oninput —— 顶层直接抛异常，后面的脚本一行都不执行。
    而页面看着是活的（HTML 渲染得好好的），其实一个按钮都没绑事件，
    排查时最容易误判成"后端没返回数据"。
    """
    js, html = _js(js_name), _html(html_name)
    used = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    used |= set(re.findall(r'\$\(\s*["\']#([\w-]+)["\']\s*\)', js))
    have = set(re.findall(r'id="([^"]+)"', html))
    made = set(re.findall(r'\.id\s*=\s*"([^"]+)"', js))   # JS 自己建出来的（如抽屉提示条）
    missing = used - have - made
    assert not missing, f"{js_name} 引用了不存在的 id：{sorted(missing)}"
    # 反过来也钉一下：HTML 上的控件没人引用，多半是只写了壳忘了绑事件
    assert not (have - used), \
        f"{html_name} 里有没人用的 id：{sorted(have - used)}"


# --- 工作流页（规格 8.2 / 8.3 / 8.4）-----------------------------------
#
# 下面每一条都在补 Task 9 漏掉的规格条目。漏掉它们的后果分两类：
# 一类是**死路**（界面上点了暂停就再也回不去），一类是**静默失效**
# （页面能开、能点、能跑，就是少了规格点名的那几样东西，测试全绿）。
#
# 第二类更坏：没有围栏就等于没有测试，所以每条都必须真的红过一次。


def test_workflow_page_shows_the_real_slot_syntax():
    """提示词模板的示例必须真的是双花括号。

    这条不是洁癖：Task 9 那一版整份 HTML 一个花括号都没有（多半是通过
    Python 之类的格式化把它吃掉了），而**写错了服务端不报错**——
    `extract_slots()` 扫不到槽位 → `validate_binding()` 没东西可拒 →
    `render()` 原样返回，于是每一行都拿同一句提示词去跑，
    表里一行的结果原封不动出现在下一行，全程零报错。

    服务端槽位语法见 server/workflow.py 的 _SLOT_RE。
    """
    html = _html("workflow.html")
    assert "{{" in html and "}}" in html, (
        "工作流页没有出现双花括号 —— 变量语法示例被吞了。"
        "服务端槽位语法是 {{名称}}（server/workflow.py 的 _SLOT_RE）")
    js = _js("workflow.js")
    assert "{{" in js, "workflow.js 里也没有双花括号，界面上无处可查这个语法"


def test_workflow_page_has_all_four_controls():
    """开始 / 暂停 / 继续 / 中止整批（规格 8.2 详情「控制」那条）。

    少「继续」是一条死路：界面上点完暂停就再也回不去了，只能改库或者
    自己调接口——而服务端 WORKFLOW_ACTIONS 本来就认 resume。
    """
    html = _html("workflow.html")
    for b in ("wf-start", "wf-pause", "wf-resume", "wf-abort"):
        assert f'id="{b}"' in html, f"缺控制按钮 {b}"
    js = _js("workflow.js")
    for act in ("start", "pause", "resume", "abort"):
        assert f'control("{act}")' in js, f"JS 里没有接 {act}"


def _func_body(src: str, name: str) -> str:
    """按花括号配对取某个具名函数的函数体。

    **不能正则截到第一个 `}`**：这几个函数体里都有 if 块与模板字符串，
    截出来的「函数体」不含后半段，于是「函数体里有 X」既可能恒真也可能恒假——
    两种都学不到任何东西。
    """
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"找不到函数 {name}"
    i = src.index("{", m.end())
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.end():i]
        i += 1
    raise AssertionError(f"{name} 的函数体没有闭合")


def _py_func_body(src: str, name: str) -> str:
    """按缩进取 Python 函数的函数体（到下一个同级或更浅的 def 为止）。

    同样不能正则截：服务端那几个函数的判断都在返回之前，正则截出来的多半是
    一句 def 行，断言就成了「它有没有把花括号写在同一行」。
    注意 `def` 可能缩进在函数里（路由处理器就是），所以按**缩进宽度**收尾，
    不能按「顶格」收尾。
    """
    m = re.search(r"^([ \t]*)def\s+" + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"找不到函数 {name}"
    base = len(m.group(1).expandtabs(4))
    out = []
    started = False
    for line in src[m.start():].split("\n"):
        if not line.strip():
            out.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if not started:
            out.append(line)
            started = line.rstrip().endswith(":")
            continue
        if indent <= base:
            break
        out.append(line)
    assert started, f"{name} 没取到函数体"
    return "\n".join(out)


def _handler_bodies(src: str, event: str) -> list[str]:
    """取出所有 addEventListener("<event>", (…) => { … }) 的回调体。"""
    out = []
    pat = re.compile(r'addEventListener\(\s*"' + event + r'"\s*,\s*'
                     r'(?:async\s*)?\([^)]*\)\s*=>\s*\{')
    for m in pat.finditer(src):
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    out.append(src[m.end():i])
                    break
            i += 1
    assert out, f"没有任何 addEventListener(\"{event}\", …)"
    return out


def _server(name: str) -> str:
    return _read(REPO / "server" / name)


def test_workflow_card_shows_the_failed_row_count_and_the_number_is_real():
    """U1：卡片上要有失败数（规格 8.2 列表那条 + 7.4「失败行数放到最显眼处」）。

    **这一条取代了旧版「卡片上不许编失败数」那条断言。** 旧版的前提是列表端点
    只有 `SELECT * FROM workflows`，而 workflows 表里没有失败计数——那个前提
    当时是对的，于是当时的结论是「照实说明、不做 UI」。现在改成真算：列表端点
    从行任务聚合一并给出，前端于是不必编。

    所以这里断的是三件互相咬住的事，缺一件都红：
      ① 卡片上真的有失败数的位置，且排在进度条**上面**（显眼）；
      ② 那个数取自服务端给的 failed_rows，不是前端自己数出来的；
      ③ 服务端真的算得出它（一条 GROUP BY，而不是每张卡片打一次详情接口）。
    ①②③ 断任何一条，剩下两条都可能各自是绿的——所以必须一起断。
    """
    js, html = _js("workflow.js"), _html("workflow.html")
    card = _func_body(js, "loadList")

    # ① 位置与显眼度：卡片模板里失败数在进度条之前
    at_failed = card.find('class="wf-card-failed"')
    at_bar = card.find('class="wf-bar"')
    assert at_failed > 0, "卡片上没有失败数的位置"
    assert 0 < at_failed < at_bar, \
        "失败数没有排在进度条上面——规格 7.4 要的是第一眼能看见"
    assert re.search(r'failedBox\.className\s*=[^;]*is-bad', card), \
        "有失败行时不标红，等于没放到显眼处"

    # ② 数据来源：取的是列表端点给的 failed_rows，且**显示出去的那个数**就是它。
    # 只断「loadList 里出现过 wf.failed_rows」是恒真的：同一函数里那句
    # 「没算出来」的判据也要读同一个键，把它换成常量而显示仍然读旧值也照样绿。
    assert re.search(r"const\s+failed\s*=\s*Number\(\s*wf\.failed_rows\s*\)\s*\|\|\s*0",
                     card), \
        "显示出去的失败数不是从 wf.failed_rows 算出来的（前端编了一个数）"
    assert re.search(r"failedBox\.textContent\s*=[^;]*\bfailed\b", card, re.S), \
        "失败数那个格子建了却没人往里写字"
    assert re.search(r'failed_rows\s*!==\s*undefined', card), \
        "缺 failed_rows 时要照实说「没算出来」，不能当成 0 显示成「没有失败行」"

    # ③ 服务端：一次 GROUP BY，且列表端点真的把统计挂上去
    srv = _server("app.py")
    stats = _py_func_body(srv, "_row_stats")
    assert "COUNT(*)" in stats, "失败数没有真的数出来"
    assert re.search(r'==\s*"failed"', stats), \
        "聚合没有按 failed 过滤（数的是行数而不是失败行数）"
    listed = _py_func_body(srv, "list_workflows")
    assert "_row_stats" in listed, "列表端点没有带上行级统计"
    assert "list_workflow_rows" not in listed, \
        "列表端点逐个工作流取行数据 = 每张卡片一次详情查询，不要这么写"
    # 前后端用同一个键名：任何一边改名，这条都红（而不是界面默默显示 0）
    assert re.search(r'out\["failed_rows"\]\s*=', srv), \
        "服务端没有把 failed_rows 放进工作流视图"


def test_workflow_current_row_is_on_the_card_too():
    """规格 8.2 列表那条里的「当前在跑第几行」，与失败数同属卡片。

    只报行号不报状态是不够的：规格 7.5 要求「界面必须显示『第 N 行停在打包中』」，
    只写「第 4 行」的话，packaging 卡住与计算中在卡片上长得一模一样——
    而这两种情况用户要做的完全不同（一个是等，一个是去点重新打包）。
    """
    card = _func_body(_js("workflow.js"), "loadList")
    assert re.search(r"wf\.current_row", card), "卡片上没有「当前在跑第几行」"
    assert re.search(r"wf\.current_row[\s\S]{0,400}wf\.current_status", card), \
        "只报了行号没报状态（打包中与计算中会长得一样）"
    assert _server("app.py").count("current_status") >= 1, \
        "服务端没给 current_status，卡片那个状态无从显示"


def test_column_role_is_picked_on_the_table_header_not_a_separate_form():
    """U2：列角色做在表头上——点表头弹出选角色，不另开映射表单（规格 8.2）。

    这一条同时断三件：
      ① 表头那格真的绑着「点它就配列」；
      ② 角色选择器就长在那个弹层里（不是点开另一个表单）；
      ③ 旧的映射列表 **不在了**。只断 ①② 的话，把弹层挪回独立列表照样全绿。
    """
    js, html = _js("workflow.js"), _html("workflow.html")
    head = _func_body(js, "renderHead")
    assert 'className = "wf-th"' in head, \
        "表头那一格没有自己的类（既没法挂样式也没法说它可点）"
    assert re.search(r'addEventListener\("click",\s*\(\)\s*=>\s*openColPick\(', head), \
        "表头没绑「点它选角色」"
    pick = _func_body(js, "openColPick")
    assert re.search(r'\$\("#wf-colpick"\)\.hidden\s*=\s*false', pick), \
        "点开之后没有出现选角色的东西"
    assert re.search(r'\$\("#wf-roles"\)\.children', pick), \
        "弹层里没有那一排角色按钮"
    # 误触：点开弹层本身不许改任何一列——只有点角色才改
    assert not re.search(r"colsModel\[\s*picked\s*\]\s*\.\w+\s*=", pick), \
        "点开弹层就改动了列配置——手滑点一下表头会把某一列的角色弄丢"
    # 旧的那个独立映射列表必须已经拆掉
    assert 'id="wf-cols"' not in html and "#wf-cols" not in js, \
        "独立的列映射表单还在：规格要的是在表头上选，不另开表单"
    # 读回列组只有一个来源：列模型。界面上不存在第二份能与之不一致的副本。
    assert "colsModel" in _func_body(js, "readCols"), \
        "保存读的不是列模型，而是另一处 DOM——两份配置迟早对不上"


def test_preview_updates_right_away_without_pressing_anything():
    """U3：改完**立刻**在下方显示「第 N 行渲染出来的提示词」（规格 8.2）。

    「立刻」的判据是不点任何按钮下面那一行就变了，所以这里断的是
    **哪个输入框的哪个事件会重画预览**——绑 change 等于要用户先失焦，
    而规格要的是改完就看见。

    同时断住「拿服务端的 rendered 直接贴上去」这条退路：那个字段渲染的是
    **已保存**的配置，用户还在改的时候它给的一直是旧句子。
    """
    js = _js("workflow.js")
    typed = [b for b in _handler_bodies(js, "input") if "renderHere" in b]
    assert len(typed) >= 2, \
        "模板框与变量名框都要在 input 上重画预览（只绑 change 要先失焦）"
    assert "renderHere" in _func_body(js, "setRole"), \
        "换列角色之后预览没有立刻重画"

    here = _func_body(js, "renderHere")
    assert "api(" not in here and "fetch(" not in here, \
        "预览去打了接口：没保存的改动服务端看不见，而且每敲一个字打一次会打爆"
    assert "replace(" in here and "previewValues" in here, \
        "预览没有就地按当前行的值渲染"

    lp = _func_body(js, "loadPreview")
    assert "renderHere()" in lp, "取回当前行的值之后没有重画预览"
    assert not re.search(r'\$\("#wf-preview"\)\.textContent\s*=\s*d\.rendered', lp), \
        "把服务端那份 rendered 直接贴上去：它是已保存的配置，改到一半看着像没生效"

    # 槽位语法与服务端同一套。两份实现（本地预览 / 服务端渲染）最容易悄悄分家，
    # 分家之后预览给的那句话与真正跑下去的那句话不是同一句。
    assert re.search(r"const\s+SLOT_RE\s*=\s*/\\\{\\\{", js), \
        "本地预览没有声明与服务端同构的槽位语法"
    srv = _server("workflow.py")
    assert r"\{\{\s*([^{}\s]+)\s*\}\}" in srv, \
        "服务端槽位语法变了：本地预览的 SLOT_RE 要跟着改"


def test_new_workflow_is_a_real_three_step_wizard():
    """U4：新建是 3 步向导——上传 → 选列配模板 → 确认开跑（规格 8.2）。

    这一条钉的是**真的分步**，不是步骤指示条：每一步有自己的面板、
    面板跟着当前步显隐、进下一步有条件、并且回得去。
    上一轮只有一条指示条时它照样是绿的——那正是漏掉的地方。
    """
    js, html = _js("workflow.js"), _html("workflow.html")

    # ① 三块面板，且与步骤条那三个名字一一对应（换序/漏一个都红）
    panes = re.findall(r'data-step="([^"]+)"', html)
    assert len(panes) == 3, f"详情页没有三块步骤面板：{panes}"
    assert re.search(r"const\s+WF_STEPS\s*=\s*\[([^\]]*)\]", js), "找不到 WF_STEPS"
    labels = re.findall(r'"([^"]*)"', re.search(
        r"const\s+WF_STEPS\s*=\s*\[([^\]]*)\]", js).group(1))
    assert panes == labels, \
        f"面板顺序与步骤名对不上：{panes} vs {labels}"
    assert re.search(r'STEP_PANES\s*=\s*\[\s*\$\("#wf-pane-1"\),'
                     r'\s*\$\("#wf-pane-2"\),\s*\$\("#wf-pane-3"\)\s*\]', js), \
        "三块面板没有在 JS 里按顺序列出来（显隐就无从对应到步骤）"

    # ② 一次只露当前这一步
    show = _func_body(js, "showStep")
    assert re.search(r"STEP_PANES\.forEach\([^;]*hidden\s*=\s*i\s*!==\s*stepNow", show), \
        "面板没有跟着当前步显隐，三步是并排摊着的"

    # ③ 进下一步有条件：没配好不许进第 3 步，且要写出缺什么
    assert re.search(r"const\s+gap\s*=\s*missingConfig\(\)", show), \
        "「下一步」不看配置状态——没配模板也能一路点到开跑"
    assert re.search(r"next\.disabled\s*=[^;]*\bgap\b", show), \
        "缺配置时那一步仍然点得进去"
    gap = _func_body(js, "missingConfig")
    assert "PROMPT_PLACEHOLDER" in gap, \
        "上传时那句占位模板被当成了用户写好的提示词"
    assert re.search(r"\$\(\"#wf-next-hint\"\)\.textContent\s*=\s*gap", show), \
        "缺什么没有告诉用户"

    # ④ 回得去：步骤条能点回去，「上一步」也在
    assert re.search(r'\$\("#wf-prev"\)\.disabled\s*=\s*stepNow\s*===\s*0', show), \
        "「上一步」没有边界，第 1 步也能往回退"
    steps_fn = _func_body(js, "renderSteps")
    assert re.search(r'addEventListener\("click",\s*\(\)\s*=>\s*gotoStep\(', steps_fn), \
        "步骤条不可点，就只能一路往前走"
    assert re.search(r'\$\("#wf-prev"\)\.addEventListener\("click",[^;]*gotoStep\(', js), \
        "「上一步」没有绑事件"
    assert re.search(r"Math\.max\(0,\s*Math\.min\(", _func_body(js, "gotoStep")), \
        "切步骤没有夹在 0 与最后一步之间，点过头会露出不存在的面板"

    # ⑤ 第 3 步才是「确认开跑」：开始按钮在第 3 块面板里，不是常驻在顶上
    pane3 = re.search(r'id="wf-pane-3".*?</div>\s*</div>', html, re.S)
    assert pane3 and 'id="wf-start"' in pane3.group(0), \
        "「开始」不在确认开跑这一步里"


def test_workflow_table_is_styled_as_the_spec_demands():
    """表格预览的四件：行号列、表头粘顶、当前行高亮、状态色点（规格 8.2 详情第一条）。

    **断言一律带作用域**（.wf-table / .wf-rownum / .wf-dot[data-status=...]）。
    写成 `assert "sticky" in css` 是恒真的：.toolbar 早就有 position: sticky
    （style.css 的工具条那条），A4 一点没修它也是绿的——这种围栏比没有更坏，
    它让人以为这件事有人管。
    """
    css = _read(CSS)
    html = _html("workflow.html")
    js = _js("workflow.js")
    assert 'class="wf-table"' in html, \
        "表格没有工作流自己的类，样式无处可挂（首页的 .card/.chip 都不是它）"
    assert re.search(r"\.wf-table[^{]*thead[^{]*\{[^}]*position:\s*sticky", css), \
        "表头没有粘顶"
    assert re.search(r"\.wf-rownum[^{]*\{[^}]*position:\s*sticky", css), \
        "行号列没有横向粘住"
    assert re.search(r"\.wf-table[^{]*is-current[^{]*\{[^}]*background", css), \
        "当前行没有高亮"
    assert re.search(
        r'\.wf-dot\[data-status="failed"\][^{]*\{[^}]*var\(--st-failed\)', css), \
        "状态色点没有绑到既有的 --st-failed 上（不许另起一套色值）"
    assert "wf-rownum" in js and "wf-dot" in js, \
        "JS 没有给行号与状态点挂上这两个类，样式等于没挂"


def test_homepage_links_to_the_workflow_page():
    """两边都要有入口：首页进得去（规格 8.2「独立页面怎么挂上去」），工作流页回得来。

    上一版是零入口——页面能打开，但没人找得到。
    """
    assert 'href="workflow.html"' in _html("index.html"), \
        "首页没有工作流入口，页面只能靠手敲 URL 到达"
    assert 'href="index.html"' in _html("workflow.html"), \
        "工作流页没有回到首页的链接"


def test_workflow_detail_can_filter_to_failed_rows_only():
    """规格 8.3 第二条：行任务的故障排查走工作流详情页，那里有「只看失败行」的筛选。"""
    html = _html("workflow.html")
    assert 'id="wf-only-failed"' in html, "详情页没有「只看失败行」的开关"
    js = _js("workflow.js")
    assert re.search(r'onlyFailed\s*&&\s*row\.status\s*!==\s*"failed"', js), \
        "开关没有真的在筛行（不是只染个色或者只在文案里写「只看失败行」）"
    assert re.search(r'\$?\(\s*["\']#wf-only-failed["\']\s*\)', js) \
        and "change" in js, "开关没有绑 change 事件，点了不重绘"


def test_workflow_download_carries_the_bearer_token():
    """「下载结果表」不能是 <a> 直接导航过去。

    实机踩到的：令牌在 localStorage 不在 Cookie 里，而导航请求不带
    Authorization 头 —— 点了只会拿到一个 401 的 JSON，界面上**不报任何错**，
    看着就是「按钮点不动」。同一个地址带上头就是 200，所以这条是死路。
    """
    js = _js("workflow.js")
    m = re.search(r"/api/workflows/\$\{wfId\}/result\.xlsx", js)
    assert m, "找不到结果表的地址"
    tail = js[m.end(): m.end() + 200]
    assert "authHeaders()" in tail, \
        "结果表是用导航去取的，没有 Authorization 头 —— 实机点它只会拿到 401"
    assert re.search(r'\$\(\s*["\']#wf-download["\']\s*\)', js) \
        and "preventDefault" in js, \
        "下载链接没有拦下默认导航，改成 fetch 取字节"


def test_workflow_detail_shows_the_three_step_indicator():
    """规格 8.2「新建是 3 步向导」的步骤条本身。

    真的分步（面板、进入条件、返回）由
    test_new_workflow_is_a_real_three_step_wizard 断；这条只断名字与位置，
    免得有人把「上传 → 选列配模板 → 确认开跑」改写成别的三个词而没人发现。
    """
    assert 'id="wf-steps"' in _html("workflow.html"), "详情页没有步骤指示"
    js = _js("workflow.js")
    m = re.search(r"const\s+WF_STEPS\s*=\s*\[([^\]]*)\]", js)
    assert m, "找不到 WF_STEPS"
    steps = m.group(1)
    for label in ("上传表格", "配列与模板", "确认开跑"):
        assert label in steps, f"步骤条少了「{label}」"
