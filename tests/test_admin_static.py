"""值班台前端的**静态**检查（Task 12）。

**为什么只有静态检查**：这三个文件是零构建的原生 HTML/CSS/JS，没有打包器、
没有组件树，pytest 里没有任何东西能"渲染"它们——真渲染要么得引浏览器驱动
（违反零外链/零依赖），要么得把 DOM 结构复制一份到 Python 里（那就成了测副本，
改前端时不跟着变，测了等于没测）。同一个理由见 `tests/test_web_static.py`。

所以这里只守那些**错了不报错、只在页面上"看起来不对"或更糟——悄悄做错事**的坑：

1. `[hidden]` 被 `display:flex` 盖掉 → 关掉干预框之后它还留在卡片上
2. 外链（CDN 字体/图标库）→ CSP `default-src 'self'` 之下整页瘫掉
3. 界面口径出现"AI/助手/大模型" → 产品定位已拍板
4. 状态色缺一个 → 流转轨上有一站和别的站分不出来
5. 流转轨少了圆点 → 只剩一根线，读不出卡在第几站
6. 危险动作不走两步确认 → 中止错任务 / 停错账号
7. **时间窗产物确认区被漏掉** → 用户看到一批永远点不下来的产物，
   而计算进程那部分的算力已经烧掉了（spec §8.2 的兜底机制失效）
8. 审计日志不能导出 → 事后追责失去唯一凭据

规则本身是断言，样式本身是事实。改前端时请连这个文件一起读。
"""
from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "admin-ui"
HTML = UI / "index.html"
CSS = UI / "style.css"
JS = UI / "app.js"

# 界面禁用词。**"AI" 必须大小写敏感**：app.js 里全是 detail / chain / main 这类
# 小写词，用小写去查会误报一片。真正的风险是文案里混进大写的 AI。
BANNED_WORDS = ("AI", "助手", "大模型")

# 值班台必须存在的能力。少一条就是少一块值班台该有的功能：
# ① 运行中任务实时卡片 ② 干预输入框 ③ 审计日志表格 ④ 时间窗产物确认区 ⑤ 用户管理
REQUIRED_PANES = ("live", "pending", "tasks", "audit", "users")

# 危险动作 → 必须两步确认（与用户端的删除同理，SPEC 20.5 / 20.9）
DANGEROUS_ACTIONS = ("doAbort", "doStatus", "doRetry", "doDisable", "doVerify")


def _read(p: Path) -> str:
    assert p.exists(), f"缺文件：{p}"
    return p.read_text(encoding="utf-8")


def _files() -> dict[str, str]:
    return {"index.html": _read(HTML), "style.css": _read(CSS), "app.js": _read(JS)}


def _fn(name: str) -> str:
    """取 JS 里某个函数的源码（到**同缩进的**收尾 `}` 为止）。

    不能用 `\\n\\}`：函数体里常有嵌套的箭头函数与回调（`forEach(x => …)`），
    它们的 `}` 也是行首，朴素匹配会在函数**中间**截断——于是断言"函数体里有
    某个按钮"时，恰恰在按钮之前就返回了后半段看不到的假阴性。
    按缩进配对才是对的：取该函数声明所在行的缩进，收尾是同缩进的 `}`。
    """
    js = _read(JS)
    m = re.search(r"^([ \t]*)(?:async )?function " + re.escape(name) + r"\(", js, re.M)
    assert m, f"找不到函数 {name}()"
    indent = m.group(1)
    tail = js[m.start():]
    end = re.search(r"\n" + re.escape(indent) + r"\}", tail)
    assert end, f"{name}() 的收尾大括号没找到（函数体可能没闭合）"
    return tail[:end.end()]


# --- 零外链 / 零构建 -----------------------------------------------------


def test_no_external_resource_links():
    """只准引用同源的 style.css / app.js，外加 data: 空 favicon。

    CSP 头是 `default-src 'self'`，任何一个 http(s) 外链都会被浏览器当场拦掉，
    而且拦掉的表现是"图标不显示"这种静默降级，不报错。
    """
    html = _read(HTML)
    refs = re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', html)
    assert refs, "index.html 一个资源引用都没有，八成是写坏了"
    for r in refs:
        assert not r.startswith(("http://", "https://", "//")), f"外链资源：{r}"
        assert r in ("style.css", "app.js", "data:,"), f"意外的引用：{r}"


def test_no_css_import_or_webfont():
    """CSS 里不许 @import 拉远端样式或字体。"""
    css = _read(CSS)
    assert "@import" not in css
    for host in ("fonts.googleapis", "fonts.gstatic", "cdn.", "unpkg", "jsdelivr"):
        assert host not in css, f"CSS 里出现了外链来源：{host}"


# --- 坑 1：[hidden] 必须显式压回 display:none ----------------------------


def test_hidden_attribute_is_forced_to_display_none():
    """凡是会被 JS 用 hidden 开关、又自己带 display 规则的元素，都必须有 [hidden] 伴生规则。

    **只写一条全局 [hidden]{display:none} 是不够的**：`.intervene{display:flex}`
    与 `[hidden]` 特异性相同（都 0,1,0），样式表里谁在后面谁赢——全局那条写在
    文件开头，会被后面的 display 整条盖掉。于是"收起干预框"时它仍然可见，
    卡片下面挂着一个空框。不报任何异常，只是"看起来没切换"。

    新增一个用 hidden 开关的元素时，往下面这个表里加一行。

    断言的是**规则体里真的有 display:none**，不只是"出现过这个选择器"——
    只查选择器的话，把伴生规则写成空壳 `.intervene[hidden] { }` 一样能过，
    而那正是这条规则存在的唯一理由。
    """
    css = _read(CSS)
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*;", css), (
        "缺全局 [hidden] { display: none } 兜底规则"
    )
    toggled = [
        ".card-step",     # 运行中卡片的当前步骤行
        ".card-fail",     # 失败原因行
        ".intervene",     # 干预输入框（默认收起）
        ".board-msg",     # 顶部横幅
        ".row-msg",       # 用户区提示条
        ".login-error",   # 登录失败提示
    ]
    # 收集所有以 display:none 收尾的规则体，按选择器逐个配对。
    # 先剥注释：否则 `/* … */` 会被当成选择器文本的一部分粘进 `sel_text`，
    # 于是 `.card-step[hidden]` 变成「一大段注释 + 选择器」而配不上。
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = re.findall(r"([^{}]+)\{([^{}]*display:\s*none[^{}]*)\}", bare)
    hidden_selectors = set()
    for sel_text, _decl in rules:
        if not re.search(r"\[hidden\]", sel_text):
            continue
        for sel in sel_text.split(","):
            sel = sel.strip()
            if sel:
                hidden_selectors.add(sel)

    for sel in toggled:
        assert sel + "[hidden]" in hidden_selectors, (
            f"{sel} 会被 hidden 切换，却没有一条真正写着 display:none 的 "
            f"{sel}[hidden] 伴生规则（写成空壳也藏不住）"
        )


def test_intervene_starts_hidden():
    """干预框开局必须是 hidden：值班台第一眼要看"在干什么"，不是一张空输入框。"""
    body = _fn("buildIntervene")
    assert "box.hidden = true" in body, "干预框开局可见 = 每张卡下面挂着一个空框"


# --- 坑 2/3：确认表与全局 Esc -------------------------------------------


def test_confirm_dialog_finish_is_guarded_and_unbinds():
    """<dialog> 复用：收尾必须走 answered 守卫的幂等 finish() 并解绑监听。

    否则第二次确认会被第一次的闭包直接 resolve 掉——那不是"取消"，
    是**中止错任务 / 停错账号**。
    """
    body = _fn("confirmBody")
    # 守卫要**真的在用**：只查函数体里出现过 "answered" 这个词是不够的——
    # `let answered = false;` 单独一行就能骗过那种检查，而守卫真没了。
    assert re.search(r"if\s*\(\s*answered\s*\)\s*return", body), (
        "确认表收尾没有真正生效的 answered 幂等守卫"
    )
    # 三条解绑都要在 finish() 内部：少了任一条，第二次确认就会被上一次
    # 留下的闭包 resolve 掉。
    finish = re.search(r"function finish\(v\)\s*\{(.*?)\n\s*\}", body, re.S)
    assert finish, "找不到 finish()"
    fin = finish.group(1)
    for target in ("ok", "cancel", "dlg"):
        assert f'{target}.removeEventListener' in fin, (
            f"finish() 里没有解绑 {target} 的监听"
        )
    assert body.count("finish(") >= 4, "确认/取消/close 三条路径都要收敛到同一个 finish()"
    assert 'addEventListener("close"' in body, "确认表要监听 close 而不是 cancel"


def test_confirm_body_rebuilds_the_dialog_each_time():
    """每次都重建 .confirm-inner，而不是往静态节点里填字。

    改状态要下拉、重置密码要输入框——复用静态节点就得记得把它们清干净，
    而"忘了清"的症状是上一次的重置密码框留在改状态表里：**不报错，只是
    看错了值**。重建没有这个分支。
    """
    body = _fn("confirmBody")
    assert 'dlg.textContent = ""' in body, "确认表没有在每次打开前重建表体"
    assert "confirm-inner" in body


def test_global_escape_ignores_open_dialog():
    """全局 Esc 遇 dialog[open] 一律不插手。

    否则确认表开着按 Esc：底下的界面一起响应，而 cancel 事件又没正常走到，
    Promise 永远不 resolve，按钮卡在 disabled 再也点不动。
    """
    js = _read(JS)
    assert 'dialog[open]' in js, "全局 Esc 没有给模态 dialog 让路"


def test_no_native_prompt_confirm_or_alert():
    """不用 window.prompt / confirm / alert：原生弹窗样式不受控且一关输入就没了。"""
    js = _read(JS)
    for fn in ("window.prompt", "prompt(", "window.confirm", "confirm(", "alert("):
        assert fn not in js, f"用了原生弹窗：{fn}"


# --- 危险动作两步确认 ----------------------------------------------------


def test_every_dangerous_action_goes_through_two_step_confirm():
    """中止 / 改状态 / 重跑 / 停用用户 / 排除产物，一律走两步确认。"""
    js = _read(JS)
    for name in DANGEROUS_ACTIONS:
        body = _fn(name)
        assert ("confirmAction(" in body or "confirmBody(" in body), (
            f"{name}() 没有走两步确认"
        )


def test_confirm_lists_what_is_lost():
    """确认表要**列出会丢什么**，不是只问一句"确定吗"。"""
    for name in ("doAbort", "doRetry", "doDisable", "doVerify"):
        body = _fn(name)
        # items 可能是字面量数组，也可能是三元（停用/启用两套说法），
        # 所以条件表达式也算数——要点是它得是**逐条列出来的后果**。
        assert re.search(r"items:\s*(?:\w+\s*\?\s*)?\[", body), (
            f"{name}() 的确认表没有逐条列出后果"
        )


def test_reject_artifact_confirm_says_it_cannot_be_undone():
    """排除产物不可撤销，确认表必须说清这一点。"""
    body = _fn("doVerify")
    assert "没有撤销" in body or "没有撤销入口" in body, (
        "排除产物的确认表没说明不可撤销"
    )


# --- 4 · 时间窗产物确认区（spec §8.2） ------------------------------------


def test_pending_pane_exists_and_is_reachable():
    """待确认产物必须是独立的一块，且有页签能切到它。"""
    html = _read(HTML)
    for pane in REQUIRED_PANES:
        assert f'data-pane="{pane}"' in html, f"值班台缺 {pane} 区域"
    assert 'data-tab="pending"' in html, "待确认产物没有页签入口"
    # 页签的文案在开标签**之外**，所以要连着后面一小段一起取，不能只匹配标签本身。
    m = re.search(r'data-tab="pending"[^>]*>(.{0,40})', html, re.S)
    assert m and "待确认产物" in m.group(1), "页签上没有写清这是待确认产物"


def test_pending_rows_show_path_and_size_and_offer_both_verdicts():
    """每条待确认产物必须显示路径与大小，并给出「确认归属 / 排除」两个动作。

    **路径不能截断**：它是判断归属的全部依据（同一时刻可能有几个候选目录），
    截掉正好把区分它们的那部分截掉。
    """
    body = _fn("pendRow")
    assert "row.path" in body, "待确认产物没有显示路径"
    assert "fmtSize" in body, "待确认产物没有显示大小"
    # 断言的是**按钮本身**：只查函数体里有没有「确认归属」这几个字会被
    # 「已确认归属，会打进结果包」那条提示骗过去——真正缺了按钮也照样绿。
    assert re.search(r'el\("button",\s*"[^"]*",\s*"确认归属"\)', body), (
        "没有「确认归属」按钮"
    )
    assert re.search(r'el\("button",\s*"[^"]*",\s*"排除"\)', body), "没有「排除」按钮"
    assert "timewindow" in body or "SOURCE_LABEL" in body, "没有标出来源路径"


def test_verify_posts_to_the_verify_endpoint_with_task_id():
    """确认动作打 artifacts/{id}/verify，且带上 task_id（后端按它定位产物）。

    **这里曾经断言错了方向。** 早先这条断言找的是 ``"/api/admin/artifacts/"``，
    也就是把**有 bug 的写法**当成了标准：管理员面板的 ``apiBase()`` 已经自带
    ``/api/admin``，调用处再写一遍会拼成 ``/api/admin/api/admin/artifacts/…``，
    「确认归属」按钮在真机上是点不动的——而这条测试一直是绿的。

    所以现在钉的是"调用处写相对路径"这条契约。相对路径 + apiBase() 的前缀
    才是它真正会打的地址，那条地址真的存在与否由
    ``test_frontend_url_contract.py::test_every_admin_panel_url_exists_in_the_admin_app``
    拿服务端路由表核对。
    """
    body = _fn("doVerify")
    assert '"/artifacts/"' in body, "确认动作没打 artifacts/{id}/verify"
    assert "task_id:" in body, "确认动作没带 task_id"
    assert "/api/admin/artifacts/" not in body, (
        "调用处又写了全路径 /api/admin/artifacts/…，会与 apiBase() 的 /api/admin "
        "重复，拼成 /api/admin/api/admin/… —— 确认按钮在真机上点不动")


def test_verified_artifact_is_removed_from_the_pending_list():
    """确认之后这一项要从待确认区消失；本地结论不许被下一轮拉取冲掉。

    不消失的话，10 秒轮询会把它原样画回来——看着像"点了没反应"，
    而实际上产物已经转正了。
    """
    js = _read(JS)
    assert "state.pending.delete" in _fn("doVerify"), "确认后没有从待确认区移除"
    load_body = _fn("load")
    assert "__done" in load_body, "重新拉取时没有保住本地已确认结论"


def test_pending_rows_say_they_are_not_downloadable_yet():
    """必须明说「尚未确认 → 不进结果包、用户下载不到」。"""
    body = _fn("pendRow")
    assert "尚未确认" in body and "下载不到" in body, (
        "待确认产物没有说明确认前用户拿不到"
    )


# --- 3 · 审计日志 --------------------------------------------------------


def test_audit_table_has_command_exit_code_and_elapsed():
    """审计表要有命令原文、退出码、耗时——这是事后追责的唯一凭据。

    断言的是 `<th>` 里那几个词，不是全文包含：全文里这三个词还出现在搜索框的
    placeholder 与注释里，只查全文的话把表头删掉也照样绿。
    """
    html = _read(HTML)
    headers = re.findall(r"<th[^>]*>(.*?)</th>", html, re.S)
    joined = "".join(headers)
    for col in ("命令原文", "退出码", "耗时"):
        assert col in joined, f"审计表缺「{col}」列（表头里没有）"
    assert 'id="audit-rows"' in html, "审计表没有表体"


def test_audit_can_be_filtered_by_task():
    """可按任务过滤。"""
    html = _read(HTML)
    assert 'id="audit-task"' in html, "审计没有按任务过滤的入口"
    assert "state.auditFilter.task" in _fn("auditFiltered")


def test_audit_can_be_exported_and_export_is_unfiltered():
    """可导出，且导出的是**未筛选**的原始行与命令全文。

    导出过滤后的 20 条等于把证据裁掉——追责要的恰恰是"它到底跑了什么"。
    """
    body = _fn("exportAuditCsv")
    assert "state.auditAll" in body, "导出的不是原始审计行"
    assert "auditFiltered()" not in body, "导出用了过滤后的结果"
    assert "text/csv" in body, "没有生成 CSV"


def test_audit_command_is_not_wrapped():
    """命令原文**不折行**：折了就看不出有没有 rm、有没有拼错路径。"""
    css = _read(CSS)
    m = re.search(r"\.grid-audit \.c-cmd\s*\{(.*?)\}", css, re.S)
    assert m, "审计命令列没有样式"
    assert "white-space: pre" in m.group(1), "命令原文被折行了"


# --- 令牌与状态色 --------------------------------------------------------


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
    assert 'style.setProperty("--seg", "var(--st-' in _read(JS), "流转轨应当只引 CSS 变量"


# --- 流转轨 --------------------------------------------------------------


def test_rail_has_a_dot_per_station():
    """每一站都必须有圆点元素。

    缺了站就只剩一根线，读不出卡在第几站——那流转轨就没有存在意义。
    """
    css = _read(CSS)
    assert ".rail-dot" in css, "流转轨没有圆点元素"
    assert "rail-dot" in _fn("paintRail"), "JS 没有为每一站生成圆点"


def test_rail_has_exactly_four_stations_and_excludes_failed():
    """轨只画四站；failed 是终点，不占一站（否则轨会读成五段进度条）。"""
    js = _read(JS)
    m = re.search(r"const STATUS_ORDER\s*=\s*\[([^\]]*)\]", js)
    assert m, "找不到 STATUS_ORDER"
    stations = [x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()]
    assert stations == ["queued", "running", "packaging", "done"], stations


def test_failed_colors_only_the_current_station():
    """failed 时红只落在当前站，线不整条涂红。

    整线变红会丢掉"走到哪一步"，正是流转轨存在的意义。
    """
    body = _fn("paintRail")
    assert re.search(r"i\s*===\s*idx", body), "当前站没有单独判定"
    assert "is-failed" not in body.replace("card.classList", ""), "轨里出现了整线涂红的写法"


def test_running_card_shows_step():
    """running 的卡片要在轨下方显示 task.step（后端已按 80 码点截断）。"""
    assert "t.step" in _fn("liveCard"), "卡片没有渲染 step"
    assert "card-step" in _fn("liveCard"), "卡片没有当前步骤行"


# --- 2 · 干预 -------------------------------------------------------------


def test_intervention_offers_steer_and_follow_up_separately():
    """插话（steer）与追加要求（follow_up）必须是两个动作。

    发错了等于没发：插话晚了，agent 已经跑过那一段了。
    """
    body = _fn("buildIntervene")
    assert '"steer"' in body and '"follow_up"' in body, "干预没有同时提供两种发法"
    assert "插话" in body and "追加要求" in body, "两个发法没有分别说明"


def test_rpc_goes_through_the_panel_not_the_internal_port():
    """前端只打面板自己的 /rpc/{task_id}。

    直接打 8475 既跨不过 CSP（`default-src 'self'`），也绕开了面板的
    命令白名单——那是唯一挡住"往计算进程塞任意命令"的地方。
    """
    body = _fn("sendRpc")
    assert '"/rpc/"' in body, "干预没有走面板的 rpc 端点"
    assert "8475" not in body, "前端不该直接连内部端口"


def test_stream_is_read_with_fetch_not_eventsource():
    """Bearer 走请求头，EventSource 带不了 Authorization —— 必须用 fetch 读流。"""
    js = _read(JS)
    assert "new EventSource" not in js, "EventSource 发不了 Authorization 头"
    assert "getReader" in js
    assert "Last-Event-ID" in js


# --- 界面口径 -----------------------------------------------------------


def test_no_banned_wording_in_any_admin_ui_file():
    """三个文件（含注释）都不许出现 AI / 助手 / 大模型。"""
    for name, text in _files().items():
        for w in BANNED_WORDS:
            assert w not in text, f"{name} 里出现了禁用词「{w}」"


def test_platform_wording_present():
    """反过来也要钉住正面口径：统一称「自动化计算平台」「计算任务」「计算进程」。"""
    text = "\n".join(_files().values())
    assert "自动化计算平台" in text
    assert "计算任务" in text
    assert "计算进程" in text


# --- 鉴权与数据层 -------------------------------------------------------


def test_api_base_is_overridable_and_same_origin_by_default():
    """api() 的 base 走同源 /api/admin，开发时可用 ?base= 覆盖。"""
    js = _read(JS)
    assert "?base=" in js or "new URLSearchParams" in js
    assert '"/api/admin"' in js


def test_token_is_bearer_in_header_not_cookie():
    """鉴权走 Authorization: Bearer（不放 Cookie），localStorage 取 token。"""
    js = _read(JS)
    assert "Bearer" in js
    assert "localStorage" in js
    assert "document.cookie" not in js, "鉴权不许走 Cookie"


def test_every_dom_id_the_script_touches_exists_in_the_html():
    """app.js 引用的 id 必须在 HTML 里。

    少一个 id 的话，`document.getElementById("x").oninput = ...` 就是
    null.oninput —— 顶层直接抛异常，后面的脚本一行都不执行。而页面看着是活的，
    排查时最容易误判成"后端没返回数据"。
    """
    js, html = _read(JS), _read(HTML)
    used = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    have = set(re.findall(r'id="([^"]+)"', html))
    assert used, "app.js 一个 getElementById 都没有，八成是写坏了"
    assert not (used - have), f"app.js 引用了不存在的 id：{sorted(used - have)}"


def test_no_bare_reload_or_location_assignment():
    """不许用 location.reload() 整页重载。

    值班台开着实时流与 10 秒轮询，整页重载会把干预框里正在敲的字、
    当前页签、审计过滤条件全扔掉——而这些正是值班的人正在用的东西。
    """
    js = _read(JS)
    assert "location.reload" not in js, "值班台不该整页重载"
    assert "location.href =" not in js, "值班台不该跳转走"


# --- 后端怎么把三件套端出去 --------------------------------------------


def test_admin_app_exists_and_serves_the_three_files_with_csp():
    """面板必须真能把三件套端出来，且响应头带 CSP。

    这里只导入 `admin/app_admin.py` 并检查它的静态映射，**不起服务、不碰数据库**
    （conftest 的 autouse 夹具已经把库指到 tmp_path，`db.init_db()` 只建表）。
    测的是"路径映射对不对"，不是端到端发请求。

    映射曾经整页 404：FastAPI 的 `{path:path}` 取的是**斜杠之后**的部分，
    "/" 传进来是空串、"/app.js" 传进来是 "app.js"，而键写的是带斜杠的
    `"/app.js"`——永远命中不了。文件在磁盘上、路由也注册了，表现却是整页 404。
    """
    import sys

    admin_dir = Path(__file__).resolve().parent.parent / "admin"
    if str(admin_dir) not in sys.path:
        sys.path.insert(0, str(admin_dir))
    import app_admin as svc                     # noqa: PLC0415

    # 键不许带前导斜杠：`{path:path}` 不会把它带进来。
    for key in svc._STATIC_FILES:
        assert not key.startswith("/"), (
            f"静态映射的键 {key!r} 带了前导斜杠，{{path:path}} 永远匹配不到"
        )
    assert "" in svc._STATIC_FILES, "缺少根路径（index.html）的映射"

    for key, (name, _media) in svc._STATIC_FILES.items():
        real = Path(svc.UI_DIR) / name
        assert real.is_file(), f"映射 {key!r} 指向的文件不存在：{real}"

    # 令牌在 localStorage 里，一次 XSS 就把它拿走，所以头必须发。
    assert "default-src 'self'" in svc.CSP
    assert "nosniff" in "".join(
        Path(svc.__file__).read_text(encoding="utf-8").split("headers = {")[1].split("}")[0]
    ), "静态响应没有 nosniff"


def test_admin_app_binds_loopback_only():
    """面板只绑 127.0.0.1，且 host 不给开关。

    面板能中止任何人的任务、导出全部审计、建用户停人——把它绑到 0.0.0.0
    是安全事故，所以不能有"顺手改一下 host"的口子。
    """
    import sys

    admin_dir = Path(__file__).resolve().parent.parent / "admin"
    if str(admin_dir) not in sys.path:
        sys.path.insert(0, str(admin_dir))
    import app_admin as svc                     # noqa: PLC0415

    assert svc.ADMIN_HOST == "127.0.0.1", "管理员面板只能绑回环地址"
    assert svc.ADMIN_PORT == 8474, "管理员面板端口应是 8474"
    src = Path(svc.__file__).read_text(encoding="utf-8")
    assert 'uvicorn.run(create_admin_app(), host=ADMIN_HOST' in src, (
        "启动命令没有把 host 钉在 ADMIN_HOST 上"
    )
    # 剥掉注释再查：docstring 里正说着"绑到 0.0.0.0 是安全事故"，
    # 全文包含会把这段说明当成违规。
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    code = re.sub(r"#.*", "", code)
    assert "0.0.0.0" not in code, "面板的可执行代码里不许出现 0.0.0.0"
