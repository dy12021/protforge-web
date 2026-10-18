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
