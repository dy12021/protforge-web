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
# ① 运行中任务实时卡片 ② 干预输入框 ③ 审计日志表格 ④ 时间窗产物确认区
# ⑤ 用户管理 ⑥ 反思（任务收工后的复盘待办）
REQUIRED_PANES = ("live", "pending", "tasks", "audit", "reflections", "users")

# 危险动作 → 必须两步确认（与用户端的删除同理，SPEC 20.5 / 20.9）
# landBtn / applyBtn 也在内：写进全局经验文件 = 之后**每一个**任务开工都读它，
# applyBtn = 直接改技能文件并提交。两者都比「停错一个账号」更难回退，
# 而 applyBtn 那一步连按错了都看不出来（模型原话没进过你的眼）。
DANGEROUS_ACTIONS = ("doAbort", "doStatus", "doRetry", "doDisable", "doVerify",
                     "landBtn", "applyBtn")


def _read(p: Path) -> str:
    assert p.exists(), f"缺文件：{p}"
    return p.read_text(encoding="utf-8")


def _files() -> dict[str, str]:
    return {"index.html": _read(HTML), "style.css": _read(CSS), "app.js": _read(JS)}


# --- JS 注释剥离 -------------------------------------------------------------
#
# 静态围栏是拿正则去源码里找形状的。**注释里写着一模一样的形状时，正则照样
# 匹配上**——于是「确认整个去掉了，只在别处留一句提到 confirmBody 的注释」
# 这种缺陷照样绿。这不是假设：有一条变异就是这么写的（注释里留着
# `confirmBody(body, "重跑", ok => {`），围栏没咬住，测了两轮才发现。
#
# 抹掉的是**注释**，不是字符串：字符串里的 `//`（URL、正则）不能当注释。
# 所以下面是个小词法器，认三样东西：行注释、块注释、字符串（' " `）。
# 还有一样容易漏的：**正则字面量**——`/[",\n]/` 里就有个引号，按字符串处理
# 会把后面一大段当成字符串，注释就抹不掉了。正则靠「前一个有效字符」区分：
# 前面是 `=(,:[!&|?{};` 这类，或者 return/typeof 这类关键字，就是正则；
# 否则是除号。

_REGEX_KEYWORDS = ("return", "typeof", "case", "in", "of", "new", "delete",
                   "void", "do", "else", "await", "yield")
#: 这些字符之后出现的 `/` 是**正则**的开始，不是除号。
_REGEX_AFTER = set("=(,:[!&|?{};")


def _regex_can_start_here(prev: str, prev_word: str) -> bool:
    if prev == "":
        return True
    if prev in _REGEX_AFTER:
        return True
    return prev_word in _REGEX_KEYWORDS


def _strip_js_comments(src: str) -> str:
    """把 JS 源码里的注释抹掉，只留可执行代码。行数与缩进保持不变。

    抹掉的部分用空格替掉（换行保留），于是按缩进找函数收尾的那套逻辑
    （`_fn` / `_branch`）照旧成立。
    """
    out: list[str] = []
    word: list[str] = []
    prev = ""            # 上一个有效字符（跳过空白）
    prev_word = ""       # 上一个有效标识符
    i, n = 0, len(src)

    def flush_word() -> None:
        nonlocal prev_word
        if word:
            prev_word = "".join(word)
            word.clear()

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if c == "/" and nxt == "/":                       # 行注释
            j = src.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue

        if c == "/" and nxt == "*":                       # 块注释
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue

        if c == "/" and _regex_can_start_here(prev, prev_word):   # 正则字面量
            j = i + 1
            in_class = False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":                            # 没闭合就别当正则
                    break
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    j += 1
                    while j < n and src[j].isalpha():     # 标志位（g / i / m）
                        j += 1
                    break
                j += 1
            out.append(src[i:j])
            flush_word()
            prev = "/"
            i = j
            continue

        if c in "\"'`":                                    # 字符串
            j = i + 1
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == c:
                    j += 1
                    break
                if ch == "\n" and c != "`":               # 没闭合就别一直吞
                    break
                j += 1
            out.append(src[i:j])
            flush_word()
            prev = c
            i = j
            continue

        out.append(c)
        if c.isalnum() or c in "_$":
            word.append(c)
        else:
            flush_word()
            if not c.isspace():
                prev = c
        i += 1

    flush_word()
    return "".join(out)


_JS_CODE: str | None = None


def _js_code() -> str:
    """整份 app.js 去掉注释后的代码。缓存一份：`_fn` 一轮下来要调几十次。"""
    global _JS_CODE
    if _JS_CODE is None:
        _JS_CODE = _strip_js_comments(_read(JS))
    return _JS_CODE


def _fn(name: str) -> str:
    """取 JS 里某个函数的源码（到**同缩进的**收尾 `}` 为止）。

    不能用 `\\n\\}`：函数体里常有嵌套的箭头函数与回调（`forEach(x => …)`），
    它们的 `}` 也是行首，朴素匹配会在函数**中间**截断——于是断言"函数体里有
    某个按钮"时，恰恰在按钮之前就返回了后半段看不到的假阴性。
    按缩进配对才是对的：取该函数声明所在行的缩进，收尾是同缩进的 `}`。

    拿的是**去掉注释之后**的代码：注释里留着一样的形状时，正则会照样匹配上，
    于是「代码删了、注释里还留着」的缺陷照样绿。
    """
    js = _js_code()
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
        ".refl-panel",    # 反思正文（默认收起）
        ".refl-raw",      # 模型原话（默认收起，解析失败时的兜底）
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


# --- 窄屏适配（手机 / 平板）----------------------------------------------
#
# 适配做完之前，viewport 与响应式规则**零覆盖**——把 width=1440 改回去、
# 把断点整段删掉，全量测试照样绿。下面每条都取到具体那一个 @media 块里再断。

ADMIN_PHONE = "(max-width: 759px)"
ADMIN_COARSE = "(pointer: coarse)"


def _media_block(css: str, query: str) -> str:
    """取出 `@media <query> { … }` 的块体（按花括号配对）。

    正则截到第一个 `}` 的话，块里第一条规则之后的几十行全落在断言范围外，
    「这条规则被改错了」照样绿。
    """
    parts = []
    for m in re.finditer(r"@media\s+" + re.escape(query) + r"\s*\{", css):
        depth, i = 0, m.end() - 1
        while i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    parts.append(css[m.end():i])
                    break
            i += 1
        else:
            raise AssertionError(f"@media {query} 的块没有闭合")
    assert parts, f"style.css 里没有 @media {query} 这一档"
    return "\n".join(parts)


def test_viewport_follows_the_device_width():
    """值班台的 viewport 也跟设备宽度走（原先锁的是 width=1440）。

    锁像素宽度的后果不是「窄了点」，而是整页按 1440 CSS px 渲染再整体缩小，
    表格里的命令原文彻底读不出来——而那正是审计日志存在的理由。
    """
    html = _read(HTML)
    m = re.search(r'<meta[^>]*name="viewport"[^>]*>', html)
    assert m, "没有 viewport meta"
    tag = m.group(0)
    assert re.search(r"width\s*=\s*device-width", tag), \
        f"viewport 没有跟设备宽度走（现在写的是 {tag}）"
    assert re.search(r"initial-scale\s*=\s*1", tag), "没有 initial-scale=1"
    assert "user-scalable=no" not in tag, "禁掉了用户缩放"
    assert not re.search(r"width\s*=\s*(?!device-width)\d+", tag), \
        f"viewport 仍然锁死了像素宽度：{tag}"


def test_every_table_gets_a_horizontal_scroll_wrapper():
    """每张 .grid 外面都套 .grid-wrap，且只在窄屏那一档开滚动。

    双向断：表套了而 CSS 没给 overflow（滚动没地方发生），或者 CSS 给了而
    表没套（页面整条横着滚，顶栏和页签会跟着一起横移）。

    桌面上**不许**给 .grid-wrap 设 overflow：`overflow-x: auto` 会把
    overflow-y 从 visible 变成 auto，于是这个 div 成了滚动容器，
    `.grid thead th` 的 sticky 就以它为基准——而它的高度等于内容高度，
    根本没有纵向可滚空间，表头从此再也不粘。这是个静默降级。
    """
    html = _read(HTML)
    tables = html.count('<table class="grid')
    wraps = html.count('class="grid-wrap"')
    assert tables, "找不到任何 .grid 表格"
    assert wraps == tables, f"表格 {tables} 张而 .grid-wrap 只有 {wraps} 个"

    css = _read(CSS)
    phone = _media_block(css, ADMIN_PHONE)
    wrap = re.search(r"\.grid-wrap\s*\{([^}]*)\}", phone)
    assert wrap, "窄屏档没有动 .grid-wrap"
    assert re.search(r"overflow-x:\s*auto", wrap.group(1)), \
        "窄屏档没有给 .grid-wrap 开横向滚动——整页会横着滚"

    before = css.split("@media", 1)[0]
    base = re.search(r"\.grid-wrap\s*\{([^}]*)\}", before)
    assert base, "文件里没有 .grid-wrap 的基线规则"
    assert "overflow" not in base.group(1), \
        "桌面上就给 .grid-wrap 设了 overflow：会把 sticky 表头的基准换成它"


def test_phone_fixes_the_two_grids_that_would_overflow():
    """两处 `minmax(Nrem, 1fr)` 在容器比 Nrem 窄时**照样排一列 Nrem**。

      · .live-list 是 minmax(24rem, 1fr) → 整块横向溢出，页面被撑出横滚条；
      · .grid 的列被压到读不出内容 → 要 min-width 保住列宽、自己横着滚。

    形状与首页 .board 一样（都是固定下限的轨道），失败方式不同，分开断。
    """
    phone = _media_block(_read(CSS), ADMIN_PHONE)
    live = re.search(r"\.live-list\s*\{([^}]*)\}", phone)
    assert live, "窄屏档没有动 .live-list"
    assert re.search(r"grid-template-columns:\s*1fr\b", live.group(1)), (
        "窄屏的实时卡片列表还是 minmax(24rem, 1fr)：容器比 24rem 窄时它照样排一列 "
        "24rem，整块横向溢出")

    grid = re.search(r"\.grid\s*\{([^}]*)\}", phone)
    assert grid and re.search(r"min-width:\s*\d+rem", grid.group(1)), (
        "窄屏的表格没有 min-width：列会被压到读不出内容")


def test_phone_drops_the_measured_sticky_offset():
    """窄屏放弃两个 sticky：它们的偏移是按桌面量出来的。

    页签的 `top: 2.55rem` 是桌面工具条的高度。工具条一旦换行，这个偏移
    全错——不是偏一点，是页签会插到工具条**中间**去，或者干脆盖住它。
    与其算不准的魔数，不如让整页正常滚：值班台上滚回去点「刷新」是常规动作。
    """
    phone = _media_block(_read(CSS), ADMIN_PHONE)
    tb = re.search(r"\.toolbar\s*\{([^}]*)\}", phone)
    assert tb and re.search(r"position:\s*static", tb.group(1)), \
        "窄屏的工具条没有放弃 sticky"
    tabs = re.search(r"\.tabs\s*\{([^}]*)\}", phone)
    assert tabs and re.search(r"position:\s*static", tabs.group(1)), \
        "窄屏的页签没有放弃 sticky：它的 top 偏移是按桌面工具条高度量出来的"
    assert re.search(r"overflow-x:\s*auto", tabs.group(1)), \
        "窄屏的五个页签没有横着滑"


def test_touch_pointer_gets_44px_targets():
    """pointer: coarse 下按钮与页签给到 44px。

    表格行**故意不给** 44px：值班台的行高就是密度，撑到 44px 会让一屏放不下
    一屏的量。真正要点的那些按钮（中止、改状态、重跑、停用、确认归属）
    走 .btn / .secondary，在下面拿满 44px——所以这里断的是按钮。
    """
    coarse = _media_block(_read(CSS), ADMIN_COARSE)
    assert re.search(r"min-height:\s*44px", coarse), "触控档没有 44px 最小可点区域"
    for sel in (".btn", ".tab", ".primary", ".secondary"):
        assert sel in coarse, f"{sel} 在触控设备上没有放大——手指点不准"


# --- 6 · 反思（任务收工后的复盘待办）---------------------------------------
#
# 这一块的核心约束只有一条：**模型的分类只是建议，落地一定经过人的手。**
# 下面的断言几乎全在钉"哪一步必须有人点一下"——一旦某一步省掉，
# 症状不是报错，而是**错的经验进了全局经验文件，之后每个任务都读它**。


def _branch(fn_src: str, marker: str) -> str:
    """取函数体内某个 if 分支的源码（按**同缩进**收尾，与 `_fn` 同一套做法）。

    不能用 `count("{")` 配平：JS 的字符串与模板字面量里都可能出现大括号，
    数出来的配平是巧合而不是结构。按缩进找收尾才是结构。
    """
    assert marker in fn_src, f"这段代码里没有分支 {marker}"
    i = fn_src.index(marker)
    line_start = fn_src.rfind("\n", 0, i) + 1
    indent = re.match(r"[ \t]*", fn_src[line_start:]).group(0)
    tail = fn_src[line_start:]
    end = re.search(r"\n" + re.escape(indent) + r"\}", tail)
    assert end, f"分支 {marker} 没找到收尾大括号（分支可能没闭合）"
    return tail[:end.end()]


def test_reflections_pane_has_a_tab_a_badge_and_its_own_list():
    """反思要是一块独立区域：有页签进得去、有角标、有一个自己的列表。"""
    html, js = _read(HTML), _read(JS)
    assert 'data-tab="reflections"' in html, "反思没有页签入口"
    m = re.search(r'data-tab="reflections"[^>]*>(.{0,40})', html, re.S)
    assert m and "反思" in m.group(1), "页签上没有写清这是反思"
    assert 'id="badge-reflections"' in html, "反思页签没有角标（待办数量看不见）"
    assert 'id="pane-reflections"' in html and 'data-pane="reflections"' in html, \
        "反思没有独立面板"
    # 角标挂在**页签按钮**里面，不在面板里：面板没打开的时候它也得能报数。
    assert html.index('id="badge-reflections"') < html.index('data-pane="reflections"'), \
        "角标长在面板内部，面板没打开时就看不到待办有多少条"
    assert 'id="refl-list"' in html and 'id="refl-empty"' in html, \
        "反思没有列表容器或空态"
    assert 'getElementById("refl-list")' in js and 'getElementById("badge-reflections")' in js


def test_reflections_load_even_while_you_are_on_another_tab():
    """角标必须**不看当前页签**地刷新。

    只在切进反思页签时才拉，那个角标会一直停在"你上次打开它那一刻"的数字上——
    等于没有角标：管理员不会为了看数字专门切过去，等切过去时早就该判的都堆在那儿了。
    """
    js = _read(JS)
    m = re.search(r"setInterval\(\(\)\s*=>\s*\{(.*?)\n\},\s*10000\);", js, re.S)
    assert m, "找不到 10 秒兜底轮询的回调体"
    body = m.group(1)
    assert "loadReflections()" in body, \
        "10 秒兜底轮询没有拉反思——不在反思页签时角标不会更新"
    # 钉的是"没有页签闸门"这件事本身：把这一行改成
    # `if (state.tab === "reflections") loadReflections()...` 也能拉到数据，
    # 但角标在别的页签上从此不更新——所以这里直接断言闸门不存在。
    assert 'state.tab === "reflections"' not in body, \
        "反思的兜底拉取被页签闸住了：不在该页签时角标永远停在旧数"
    # 切进页签时也要拉一次（而不是只在启动时拉）。
    assert "loadReflections()" in _fn("showTab"), \
        "切进反思页签没有触发拉取——刷新之后第一次打开是空的"
    # 开页时也要拉：只靠 10 秒轮询的话，刷新后那十秒里角标是空的，
    # 值班的人会以为没有待办。
    assert "loadReflections()" in _fn("boot"), \
        "启动时没有拉反思——刷新后角标要空等一个轮询周期才出现"


def test_reflections_badge_counts_the_unfiltered_backlog_not_the_current_filter():
    """角标数的是**全部待办**，不是当前筛选后列表的长度。

    管理员经常把筛选停在「待分类」上；按列表长度算，判完一条之后切到别的
    筛选，角标就变成 0——而实际上还有一堆待分类的。那等于角标在说谎。
    """
    body = _fn("loadReflections")
    assert "state.reflCounts" in body, "角标没有用服务端给的各状态计数"
    m = re.search(r"const todo\s*=\s*(.*?);", body, re.S)
    assert m, "找不到待办条数的计算"
    assert "reflCounts" in m.group(1), \
        "待办条数是按当前列表长度算的：筛选一换角标就说谎了"
    assert "reflections.length" not in m.group(1) and "rows.length" not in m.group(1), \
        "待办条数用了当前列表的长度——筛选一换角标就说谎了"
    assert re.search(r"badge\.hidden\s*=\s*todo\s*===\s*0", body), \
        "待办清零之后角标没有藏起来——会一直挂着一个 0"


def test_reflections_retry_timer_is_cancelled_before_it_is_armed_again():
    """重新拉取前必须先 clearTimeout。

    方案生成期间一次调用挂一条 4 秒定时器，10 秒兜底轮询又调一次 —— 两条路都会挂。
    不撤销就是定时器越挂越多、越刷越快，而且页面早就关了还在跑。
    """
    body = _fn("loadReflections")
    arm = body.find("state.reflTimer = setTimeout")
    assert arm != -1, "方案生成中的那条没有自轮询：管理员盯着等，界面却一直不动"
    clr = body.find("clearTimeout(state.reflTimer)")
    assert clr != -1, "拉取前没有撤销上一次挂的定时器"
    assert clr < arm, "撤销写在了挂定时器之后——顺序反了等于没撤"


def test_reflections_body_starts_collapsed():
    """正文默认收起：管理员进来第一眼要的是「有几条等我判」，不是一堵墙。

    开合状态存在 `state.reflOpenId` 里，所以「默认收起」钉的是**它的初值是空**，
    而不是钉某一行 `panel.hidden = true` ——后者会在为了让它扛住轮询重画
    （见下一条）而改成按 state 恢复时被误判成缺陷。
    """
    js = _read(JS)
    m = re.search(r"const state = \{(.*?)\n\};", js, re.S)
    assert m, "找不到 state 字面量"
    assert re.search(r"reflOpenId:\s*null", m.group(1)), \
        "正文开合状态的初值不是空：新打开页面时所有反思都摊开了"


def test_reflections_open_body_survives_the_poll_rebuild():
    """展开的正文不许被 10 秒轮询收回去。

    **这条是被真机抓出来的缺陷。** 角标要不走样，就得无条件轮询；而轮询走的是
    `host.textContent = ""` 整块重画，于是卡片元素每次都是新的。展开状态要是
    只存在卡片元素上（或者干脆 `panel.hidden = true` 写死），管理员刚点开的
    正文十秒后自己收起来——读到一半的内容没了，界面上没有任何东西提示发生过。

    所以开合状态必须在 `state` 里，重画时按 id 恢复。
    """
    card = _fn("reflCard")
    assert "state.reflOpenId" in card, \
        "正文开合状态没有存进 state：轮询一重画，正在读的反思就自己收起来了"
    # 开合按钮必须**写**这个状态，否则重画后无从恢复。
    assert re.search(r"state\.reflOpenId\s*=\s*open\s*\?\s*r\.id\s*:\s*null", card), \
        "点开/收起没有更新 state.reflOpenId"
    # 卡片建出来时按它决定开合，而不是写死收起。
    assert re.search(r"panel\.hidden\s*=\s*state\.reflOpenId\s*!==\s*r\.id", card), \
        "卡片重建时把正文写死收起了：轮询一轮就丢一次展开状态"
    assert re.search(r'toggle\.textContent\s*=\s*panel\.hidden', card), \
        "按钮文案没有跟着开合状态走：重画后写着「收起正文」却是收起的"
    # **只恢复 hidden 是不够的**：新卡片的 panel 是空 div，恢复成不隐藏就得到
    # 一个"开着、但什么都没有"的白框——那比收起来更像坏了。所以建卡片时就要
    # 在「应当是展开的」这一支上把正文内容重画回来。
    assert re.search(
        r"if\s*\(\s*!\s*panel\.hidden\s*&&\s*state\.reflDetail\s*&&\s*"
        r"state\.reflDetail\.id\s*===\s*r\.id\s*\)\s*\{\s*renderReflPanel\(", card), \
        "重画后没有把正文内容补回来：面板开着但是空的"


def test_classify_and_land_are_two_separate_steps():
    """分类与落地必须分成两步，中间留一次人的停顿。

    一步到位（draft 就直接能「写进经验」）意味着管理员在**还没读过正文**的时候
    就能把模型的话写进全局经验文件——而那之后每个任务开工都会读到它，
    没人说得清那条奇怪的要求是从哪来的。
    """
    card = _fn("reflCard")
    draft = _branch(card, 'if (r.status === "draft")')
    assert "classifyBtn" in draft, "待分类的那条没有分类按钮"
    assert "rejectBtn" in draft, "待分类的那条不能否决"
    assert "landBtn" not in draft and "/land" not in draft, \
        "待分类就能直接写进经验——分类与落地被并成一步了"
    reviewed = _branch(card, 'if (r.status === "reviewed" && r.kind === "lesson")')
    assert "landBtn" in reviewed, "判为经验之后没有落地入口"


def test_tool_gap_cannot_be_landed_as_a_lesson():
    """「工具不够用」不能一步写成经验。

    这两件事的做法完全相反：经验是写进记忆文件、工具意见是要改技能文件。
    混了以后管理员以为在改技能，实际把一条「这个工具太难用」写成了
    「以后凡是这样就绕开它」——把该修的东西写成了绕过的东西。
    """
    card = _fn("reflCard")
    gap = _branch(card, 'if (r.status === "reviewed" && r.kind === "tool_gap")')
    assert "landBtn" not in gap and "/land" not in gap, \
        "工具意见能被直接写成经验——该修的东西被写成了绕过的东西"
    assert "proposeRow" in gap, "工具意见没有走「生成修改方案」这条路"


def test_apply_proposal_shows_the_real_diff_before_touching_anything():
    """应用方案必须**先把 diff 取回来给人看**，看不全就不许点。

    断言的是顺序与守卫两件事：`GET /reflections/{id}` 要在 confirmBody 之前，
    读不到 diff 要当场退出而不是"照样确认"，而 diff 本身要进确认表正文。
    只查"函数体里出现过 detail.diff"的话，把渲染挪到确认之后也一样绿。
    """
    body = _fn("applyBtn")
    get_at = body.find('api("GET", "/reflections/"')
    confirm_at = body.find("confirmBody(")
    assert get_at != -1, "应用前没有把方案取回来"
    assert confirm_at != -1, "应用方案没有走两步确认"
    assert get_at < confirm_at, "先把确认表弹出来再取 diff：弹窗里是空的"
    guard = re.search(r"if\s*\(\s*!\s*detail\.diff\s*\)\s*\{(.*?)\n\s*\}", body, re.S)
    assert guard, "读不到 diff 却没有当场退出——会拿着空方案确认"
    assert "return" in guard.group(1), "读不到 diff 的分支没有 return，后面照样往下走"
    assert guard.start() < confirm_at, "守卫写在确认之后，等于没有守卫"
    assert re.search(r'el\("pre",\s*"refl-diff",\s*detail\.diff\)', body), \
        "diff 没有进确认表正文——确认的是一句说明，不是改动本身"
    assert "/apply" in body, "确认之后没有真的调应用端点"


def test_a_failed_reflection_offers_a_way_to_run_it_again():
    """失败的那条必须给一个「重跑」的出口。

    这条是被真机抓出来的：值班台上「生成失败」的反思卡片上**一个按钮都没有**。
    而 `generate` 见到记录就直接返回，于是模型调不通一次、正文写坏一次，
    这条反思就**永远**停在失败态——管理员既不能重试也不能否决。

    少这一条按钮的代价不是"少个功能"，是这条反思彻底没人管了：它既不在
    待办里（不算 draft），又永远回不来。
    """
    card = _fn("reflCard")
    failed = _branch(card, 'else if (r.status === "failed")')
    assert "retryBtn" in failed, \
        "失败的那条反思没有重跑入口：它永远停在失败态，管理员干看着"
    # 展开正文会重画动作条，那里也得有——不然点一下「看正文」按钮就没了。
    panel = _fn("renderReflPanel")
    failed2 = _branch(panel, 'else if (fresh.status === "failed")')
    assert "retryBtn" in failed2, \
        "展开正文后重画的动作条里没有重跑入口：点一下「看正文」按钮就消失了"


def test_retry_goes_through_a_confirmation_and_calls_the_retry_endpoint():
    """重跑要两步确认，且**接口调用必须在确认回调里面**。

    只查「函数体里出现过 confirmBody」是不够的：把确认整个去掉、别处留一句
    提到 confirmBody 的注释，照样能过——而那正是"管理员没确认，记录就被删了"。
    这条围栏第一次就是这么写的，被一条变异当场揭穿；第二次仍然没咬住，
    因为那句注释被正则当成了真的调用（`_fn` 现在只给**去注释后**的代码）。

    所以这里查的是**确认回调的函数体里有没有那个接口调用**：
    confirmBody 的第一个参数是表体，第二个是按钮文案，第三个才是回调。
    """
    body = _fn("retryBtn")
    m = re.search(
        r'confirmBody\(\s*body\s*,\s*"重跑"\s*,\s*\(\s*\)\s*=>\s*\{(.*?)\n\s*\}\);',
        body, re.S)
    assert m, "重跑没有走两步确认（找不到「确认表 → 回调」这个形状）"
    inner = m.group(1)
    assert "/retry" in inner, \
        "重跑的接口调用不在确认回调里：管理员还没确认，记录就被删了"
    # 只出现一次：回调里一次、回调外一次，说明有另一条路绕过了确认。
    assert body.count("/retry") == 1, \
        "重跑按钮里有多于一处接口调用：有一条绕过了确认"


def test_reflection_actions_use_the_read_contract_not_a_callback():
    """反思那四个按钮的确认回调必须**不接参数**。

    `confirmBody` 的第三个参数是「读表单值」的回调——onOk 里是 `read()`，
    **不传参**，它的返回值就是那张表 resolve 出去的东西。于是只有两种用法：
    要读表单值就返回那个值；只是「确认完就跑动作」就把动作做了、返回 true。

    写成「拿到一个回调再调它」（也就是当它是个 `ok`）是错的：那个参数是
    undefined，一调就抛。抛在接口调用之前，重跑按钮完全空转；抛在之后，
    确认表永远不关、Promise 悬着。

    **这条是被真机抓出来的**：那四个按钮照着**用户面板** `confirmAction` 的
    写法写进了**值班台**，两套约定串了。静态形状完全正确，58 条静态围栏全绿
    ——只有真把确认点一遍才看得见（行为围栏在 tests/frontend_smoke.mjs）。
    """
    for fn, label in (("retryBtn", "重跑"), ("rejectBtn", "否决"),
                      ("landBtn", "写进去"), ("applyBtn", "应用")):
        body = _fn(fn)
        m = re.search(r'confirmBody\(\s*body\s*,\s*"' + label + r'"\s*,(.*)', body, re.S)
        assert m, f"{fn}() 的确认表不是 confirmBody(body, \"{label}\", ...)"
        arg = m.group(1).lstrip()
        assert arg.startswith("() =>"), (
            f"{fn}() 的确认回调声明了参数「{arg.splitlines()[0]}」："
            "confirmBody 的第三参是读值回调，不传参——那个参数是 undefined，"
            "一调就抛，抛在接口调用之前按钮就完全空转"
        )


def test_landing_lets_the_admin_pick_the_skill_and_says_what_it_wrote():
    """落地那一步要能指定技能，而且**落地报告要显示出来**。

    两条都是真机抓出来的：模型标的技能是「通用」（它不认识真实的技能名），
    记录里那个技能落地时没人读；落地报告被端点丢掉之后，「只落了一半」这件
    事界面上一个字都不说——管理员以为技能目录里也已经有了那一条。
    """
    body = _fn("landBtn")
    assert "refl-skill-list" in body, \
        "落地的确认表里没有技能输入：模型标的技能对不上真实技能时，经验落不进技能目录"
    assert re.search(
        r'api\("POST",\s*"/reflections/"\s*\+\s*r\.id\s*\+\s*"/land",\s*\{\s*skill:',
        body), "落地时没有把管理员填的技能发给服务端"
    assert re.search(r"got\s*&&\s*got\.note", body), \
        "落地报告没被显示：只落了一半也不会有人知道"
    # **不许预填。** 预填把「模型的猜测」变成「管理员明确指定」，而归属语义
    # 正是靠这个区别：模型标「通用」= 跨技能的通用做法，只留全局文件。
    # 预填过的话，「通用」那条会被自动塞进某个技能目录。模型的建议只能进
    # placeholder（看得见、抄得动，但没填就不算指定）。
    assert not re.search(r"skill\.value\s*=\s*[^;]*r\.skill", body), \
        "落地确认表把技能预填成了记录里那个：那是模型猜的，不是管理员指定的"
    assert not re.search(r"skill\.value\s*=\s*[^;]*suggested_skill", body), \
        "落地确认表把技能预填成了模型建议：没填就不算管理员指定"
    assert re.search(r"skill\.placeholder\s*=", body), \
        "模型的建议连提示都没给：管理员不知道该填什么"


def test_the_comment_stripper_keeps_code_and_drops_comments():
    """剥注释的词法器本身要有围栏：它错了，全盘的静态围栏就都瞎了。

    三个容易错的地方：字符串里的 `//` 不是注释；正则字面量里的引号不是
    字符串的开始；行数不能变（按缩进找函数收尾的那套逻辑靠它）。
    """
    src = (
        'const a = 1; // 注释里也有 confirmBody(\n'
        '/* 块注释\n'
        '第二行 */ const b = "http://x//y";\n'
        'const re = /[",\\n]/;\n'
    )
    code = _strip_js_comments(src)
    assert "注释里也有" not in code, "行注释没被抹掉"
    assert "块注释" not in code, "块注释没被抹掉"
    assert "http://x//y" in code, "字符串里的 // 被当成注释抹掉了"
    assert '/[",\\n]/' in code, "正则字面量里的引号被当成了字符串的开始"
    assert code.count("\n") == src.count("\n"), "抹注释时行数变了"


def test_reflections_render_the_models_guess_as_a_guess():
    """界面上必须把「模型建议」和「谁定成了什么」分开说。

    建议与结论长得一样（同一个词、同一个位置、同一种颜色）的时候，
    人就会顺手点"同意"——这正是这一块要防的失败模式。
    """
    card = _fn("reflCard")
    assert "模型建议：" in card, "建议行没有写明这是模型给的"
    assert "REFL_KIND_LABEL[r.suggested]" in card, "建议没有走自己的标签表"
    assert "decided_by" in card and "refl-by" in card, \
        "谁定成了什么没有单独说：建议与结论读起来是同一件事"
    assert "REFL_KIND_LABEL[r.kind]" in card


def test_reflections_card_publishes_its_status_for_the_css():
    """卡片必须把状态写进 data-status，竖条的颜色靠它。

    JS 不写、CSS 只按 data-* 上色的话，这个功能表现为"所有卡片一个颜色"，
    而且没有任何报错——待办与已办在页面上长得一模一样。
    """
    assert re.search(r"box\.dataset\.status\s*=\s*r\.status", _fn("reflCard")), \
        "反思卡片没有把状态写进 data-status，左侧竖条会永远是同一个颜色"
    css = _read(CSS)
    for st in ("reviewed", "applied", "rejected", "failed"):
        assert re.search(r"\.refl\[data-status=" + st + r"\]", css), \
            f"data-status={st} 没有对应的样式，状态看不出来"


def test_reflections_panel_keeps_the_skill_path_verbatim():
    """要改的技能路径照原样显示、不截断。

    路径写错一位，改到的是另一个文件——而这类"改错了地方"的错误不会报错，
    技能照常加载，只是你修的那条问题还在。
    """
    assert re.search(r"\.refl-target\s*\{[^}]*\}", _read(CSS)), \
        "技能路径输入框没有样式"
    rule = re.search(r"\.refl-target\s*\{([^}]*)\}", _read(CSS)).group(1)
    assert "text-overflow" not in rule and "ellipsis" not in rule, \
        "技能路径被截断了：路径写错一位，改到的是另一个文件"


def test_reflections_raw_text_is_kept_for_unparsable_output():
    """模型原话要留着：解析不出结构时，人至少能看到它到底写了什么。"""
    body = _fn("renderReflPanel")
    assert re.search(r'el\("pre",\s*"refl-raw",\s*detail\.body', body), \
        "没有留模型原话：解析失败就只剩一句「解析不出结构」"
    assert re.search(r"raw\.hidden\s*=\s*true", body), "模型原话默认展开"
    assert "parsed" in body and "refl-bad" in body, "解析失败没有明说"
