/* 自动化计算平台 · 工作流页
 *
 * 三条硬口径，与 app.js 同：
 *   1. 界面口径统一「自动化计算平台」「计算任务」，测试 test_web_static.py
 *      会把违规字眼连注释一起扫出来。
 *   2. 零外链零构建：只用同源 style.css / workflow.js。
 *   3. 鉴权走 Authorization: Bearer（不放 Cookie），令牌键与 app.js 相同
 *      （localStorage 里的 protforge.token）。**漏了这个头，页面上每一个
 *      接口都是 401，而骨架是静态 HTML、渲染得好好的，看着像「能打开」。**
 *
 * 生命周期是串行的：表格每行一个计算任务，一行跑完归档回表格才放下一行。
 * 所以这里显示的是「第 N / M 行」这种批量的心智，不是几百张任务卡片。
 */
const $ = (s) => document.querySelector(s);
const TOKEN_KEY = "protforge.token";

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }

/* base 是**源前缀**，不含 /api。与 app.js 同一份实现：?base= 是开发时
 * 后端不在同一个源的覆盖口，两页必须一致，否则带 ?base= 打开工作流页
 * 会打到一个不存在的地方。 */
function apiBase() {
  const q = new URLSearchParams(location.search).get("base");
  const b = q || window.PROTFORGE_WEB_BASE || "";
  return b ? b.replace(/\/+$/, "") : "";
}

function authHeaders() {
  const h = {};
  const t = token();
  if (t) h["Authorization"] = "Bearer " + t;
  return h;
}

/* detail 恒为中文字符串（含 pydantic 框架层错误）。数组透出去前端只会拿到
 * "[object Object]"，所以只认字符串。 */
async function errFrom(r) {
  const d = await r.json().catch(() => null);
  const msg = d && typeof d.detail === "string" ? d.detail : null;
  return new Error(msg || ("请求失败（HTTP " + r.status + "）"));
}

async function api(method, path, body) {
  const opt = { method, headers: authHeaders() };
  if (body !== undefined && body !== null) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(apiBase() + path, opt);
  if (!r.ok) throw await errFrom(r);
  if (r.status === 204) return null;
  return r.json();
}

/* ---------------------------------------------------------------- 提示条 */

function say(text) {
  const box = $("#wf-msg");
  box.textContent = text || "";
  box.hidden = !text;
}

/** 任何一步出错都写进提示条。静默失败是这个项目最难查的一类：
 *  点一下没反应、日志里也没有，用户只会当成「后端没返回数据」。 */
async function guard(label, fn) {
  try {
    const v = await fn();
    say("");
    return v;
  } catch (e) {
    say(label + "：" + (e && e.message ? e.message : String(e)));
    return null;
  }
}

/* ---------------------------------------------------------------- 词表 */

const ROLES = ["ignore", "key", "input", "context", "output"];
const ROLE_LABEL = {
  ignore: "忽略", key: "标识列", input: "输入列",
  context: "上下文", output: "结果列",
};
/* 结果列的字段名是服务端白名单（server/workflow.py 的 OUTPUT_FIELDS），
   写一个不在表里的名字，服务端整条保存会 422。 */
const OUTPUT_FIELDS = ["status", "summary", "error", "artifacts"];
const OUTPUT_LABEL = {
  status: "状态", summary: "结果说明", error: "失败原因", artifacts: "产物清单",
};
/* 工作流的 status 词与行任务**不是**一套：
   workflows 表的 CHECK 是 draft/running/paused/finished，终态是 finished；
   行任务才是 done/failed（db.TERMINAL_STATUSES）。混用会写出永远不触发的分支。 */
const WF_STATUS_LABEL = {
  draft: "未开始", running: "运行中", paused: "已暂停", finished: "已跑完",
};
const ROW_STATUS_LABEL = {
  queued: "已排队", running: "计算中", packaging: "打包中",
  done: "已完成", failed: "失败",
};

let wfId = null;
let wfSheet = "";
/* 详情页最近一次拿到的整份数据。「只看失败行」切的是**重绘**，所以得留一份，
   否则点一下筛选就得重新打一次接口——而筛选本身不该产生任何副作用。 */
let lastDetail = null;
/* 列模型是列配置的**唯一事实**：表头、角色弹层、保存、实时预览全从它读。
   界面上不再另有一份「映射表单」可以与之不一致——旧版那两份各说各话，
   改完表头和改完表单不是同一件事。 */
let colsModel = [];
/* 正在配列的那一列在 colsModel 里的下标；-1 表示弹层没开。 */
let picked = -1;
/* 当前预览的是第几行，以及那一行的单元格值（最近一次 /preview 带回来的）。
   实时预览在**本地**把它们渲染成提示词，见 renderHere。 */
let curRow = 0;
let previewValues = {};
/* 三步向导当前站在第几步（0 基）。默认站由真实状态算，用户点步骤条可以回去。 */
let stepNow = 1;

/* 三步向导的步骤名（规格 8.2「新建是 3 步向导」）。
   做成真的分步：三块面板各有进入条件与返回，不是画上去的一串假按钮。 */
const WF_STEPS = ["上传表格", "配列与模板", "确认开跑"];
/* 上传时服务端先落一句占位模板（server/app.py 的 PROMPT_PLACEHOLDER），
   它不是用户写的提示词，不能算「第 2 步已配好」。 */
const PROMPT_PLACEHOLDER = "（还没配置提示词模板）";
/* 槽位语法，与 server/workflow.py 的 _SLOT_RE 同一套写法：{{名称}}。
   本地渲染（renderHere）照它写，改一处必须同时改另一处。 */
const SLOT_RE = /\{\{\s*([^{}\s]+)\s*\}\}/g;

/** 模板里用到的变量名，按出现顺序去重（与服务端 extract_slots 同义）。 */
function templateSlots(tpl) {
  const out = [];
  const re = new RegExp(SLOT_RE.source, "g");
  let m;
  while ((m = re.exec(tpl || "")) !== null) {
    if (out.indexOf(m[1]) === -1) out.push(m[1]);
  }
  return out;
}

/** 第 2 步还缺什么：返回空串就是配好了，可以进第 3 步。
 *
 *  判据与服务端 validate_binding 同一套——模板不是那句占位文本、每条输入/
 *  上下文列都绑了变量名、模板里的变量都有列、绑了的变量模板里都用上了。
 *  **判据读界面上的控件**（模板框 + 列模型），不是服务端存的那一份：
 *  用户刚敲完模板而「下一步」还是灰的，看着就像坏了。
 */
function missingConfig() {
  const t = ($("#wf-template").value || "").trim();
  if (!t || t === PROMPT_PLACEHOLDER) return "先在下面写提示词模板";
  const bound = colsModel.filter(
    (c) => c.role === "input" || c.role === "context");
  if (!bound.length) return "至少把一列设成输入列或上下文";
  if (bound.some((c) => !(c.slot || "").trim())) {
    return "有输入列或上下文列还没绑变量名";
  }
  const slots = bound.map((c) => (c.slot || "").trim());
  const used = templateSlots(t);
  const unbound = used.filter((n) => slots.indexOf(n) === -1);
  if (unbound.length) {
    return "提示词里的这些变量没有列绑定：" + unbound.join("、");
  }
  const unused = slots.filter((n) => used.indexOf(n) === -1);
  if (unused.length) return "这些列绑了变量但模板里没用到：" + unused.join("、");
  return "";
}

/** 按真实状态，该站在第几步：没配好停在「配列与模板」，配好了就在「确认开跑」。 */
function stepByState() {
  return missingConfig() ? 1 : 2;
}

/** 步骤条。每一步能不能点，就看它是不是状态允许到达的那一步：
 *  没配好时第 3 步点不动（进了也开不了跑），其余都能点回去。 */
function renderSteps() {
  const box = $("#wf-steps");
  box.innerHTML = "";
  const reach = stepByState();
  WF_STEPS.forEach((label, i) => {
    const li = document.createElement("li");
    const jump = i !== stepNow && i <= reach;
    li.className = "wf-step"
      + (i < stepNow && i <= reach ? " is-done" : "")
      + (i === stepNow ? " is-now" : "")
      + (i > reach ? " is-locked" : "")
      + (jump ? " is-jump" : "");
    li.textContent = `${i + 1} ${label}`;
    if (jump) li.addEventListener("click", () => gotoStep(i));
    box.appendChild(li);
  });
}

/** 切到第 i 步。两条回去的路都留着：点步骤条、点「上一步」。 */
function gotoStep(i) {
  stepNow = Math.max(0, Math.min(WF_STEPS.length - 1, i));
  showStep();
}

/* 三块面板各对应一步。**逐个列出来**而不是 `#wf-pane-${i}` 拼出来：
   test_web_static.py 的「脚本引用的 id 必须在 HTML 里」是按字面量扫的，
   拼出来的 id 它扫不到，于是拼错一个字母就是一条没人守着的空面板。 */
const STEP_PANES = [$("#wf-pane-1"), $("#wf-pane-2"), $("#wf-pane-3")];

/** 按当前步显示面板，并重画那道「进下一步」的门。
 *  **纯重画**：不打接口、不改任何一行任务的状态。 */
function showStep() {
  STEP_PANES.forEach((pane, i) => { pane.hidden = i !== stepNow; });
  const gap = missingConfig();
  const next = $("#wf-next");
  next.disabled = stepNow >= WF_STEPS.length - 1 || !!gap;
  next.title = gap || "去看一眼要跑什么";
  $("#wf-prev").disabled = stepNow === 0;
  $("#wf-next-hint").textContent = gap;
  renderSteps();
  if (stepNow === 2) renderSummary();
}

/** 第 3 步的「确认开跑」：把要跑的东西摊开写出来，再决定点不点开始。 */
function renderSummary() {
  const lines = [];
  const total = Number(lastDetail && lastDetail.workflow
    ? lastDetail.workflow.total_rows : 0) || 0;
  const rows = (lastDetail && lastDetail.rows) || [];
  const failed = rows.filter((r) => r.status === "failed").length;
  lines.push(`表格：${wfSheet ? "工作表 " + wfSheet : "—"}，共 ${total} 行，逐行串行跑。`);
  lines.push("列：" + (colsModel.map(
    (c) => `${c.header || "列" + (c.col_index + 1)}→${ROLE_LABEL[c.role] || c.role}`
      + (c.slot ? " " + c.slot : "") + (c.field ? " " + OUTPUT_LABEL[c.field] : "")
  ).join("，") || "（还没有列）"));
  lines.push("回写：" + ($("#wf-writeback").checked
    ? "跑完生成结果表，可下载" : "结果只存在平台里，不生成结果表"));
  lines.push(`已跑 ${Number(lastDetail.workflow.cursor) || 0} / ${total} 行`
    + (failed ? `，失败 ${failed} 行` : ""));
  const tpl = ($("#wf-template").value || "").trim();
  lines.push("提示词：" + (tpl || PROMPT_PLACEHOLDER));
  $("#wf-summary").textContent = lines.join("\n");
}

/* ---------------------------------------------------------------- 列表 */

async function boot() {
  await guard("读取工作流列表", loadList);
}

async function loadList() {
  const d = await api("GET", "/api/workflows");
  $("#wf-list").hidden = false;
  $("#wf-detail").hidden = true;
  const box = $("#wf-cards");
  box.innerHTML = "";
  for (const wf of d.workflows) {
    const done = Number(wf.cursor) || 0;
    const total = Number(wf.total_rows) || 0;
    const el = document.createElement("article");
    el.className = "card";
    el.innerHTML = `<h3 class="card-title"></h3>
      <div class="card-meta"><span class="card-id"></span></div>
      <div class="wf-card-line">
        <span class="wf-card-failed"></span>
        <span class="wf-card-now"></span>
      </div>
      <div class="wf-bar"><div class="wf-bar-fill"></div></div>
      <div class="card-meta"><span class="card-time"></span></div>`;
    el.querySelector(".card-title").textContent = wf.name;
    el.querySelector(".card-id").textContent =
      `${WF_STATUS_LABEL[wf.status] || wf.status} · ${done} / ${total} 行`;
    // 「当前在跑第几行」与「失败多少行」是服务端按行任务算出来的两个数，
    // 列表端点一次 GROUP BY 给了（server/app.py 的 _row_stats）。
    // **两个键缺一都要照实说**：缺 failed_rows 时写「没算出来」而不是 0，
    // 把「没这个数」显示成「没有失败行」等于对着一个没发生过的故障报平安。
    const nowBox = el.querySelector(".wf-card-now");
    nowBox.textContent = Number.isInteger(wf.current_row)
      ? `第 ${wf.current_row + 1} 行 · ${ROW_STATUS_LABEL[wf.current_status] || "在跑"}`
      : (wf.status === "finished" ? "已全部跑完" : "");
    const failedBox = el.querySelector(".wf-card-failed");
    const hasFailed = wf.failed_rows !== undefined && wf.failed_rows !== null;
    const failed = Number(wf.failed_rows) || 0;
    failedBox.className = "wf-card-failed" + (failed > 0 ? " is-bad" : "");
    failedBox.textContent = !hasFailed
      ? "失败行数没算出来"
      : failed > 0 ? `失败 ${failed} 行` : "没有失败行";
    failedBox.title = hasFailed
      ? `已失败的行数（共 ${total} 行）`
      : "服务端没有返回这个数，别当成 0";
    if (failed > 0) el.classList.add("is-failed");
    // 进度条不是装饰：宽度真按 cursor / total 算，空表也不会除出 NaN。
    // 在跑的用暖色、停了的用绿色——一眼分得出「还在干」和「停了但已收工」。
    const bar = el.querySelector(".wf-bar");
    bar.className = "wf-bar" + (wf.status === "running" ? " is-running" : "");
    bar.title = `已跑 ${done} / ${total} 行`;
    el.querySelector(".wf-bar-fill").style.width =
      (total > 0 ? Math.round(done * 100 / total) : 0) + "%";
    el.querySelector(".card-time").textContent =
      (wf.abort_requested ? "已请求中止" : "")
      + (wf.writeback_pending ? " · 结果表待补写" : "");
    el.addEventListener("click", () => guard("打开工作流", () => openDetail(wf.id)));
    box.appendChild(el);
  }
}

/* ---------------------------------------------------------------- 详情 */

async function openDetail(id) {
  wfId = id;
  const d = await api("GET", `/api/workflows/${id}`);
  $("#wf-list").hidden = true;
  $("#wf-detail").hidden = false;
  const wf = d.workflow;
  wfSheet = wf.sheet || "";
  lastDetail = d;
  picked = -1;
  $("#wf-name").textContent = wf.name;
  $("#wf-sheet").textContent = wfSheet ? "工作表 " + wfSheet : "";
  $("#wf-upload-receipt").textContent =
    `${wf.name} · ${wfSheet ? "工作表 " + wfSheet : "第一个工作表"} · `
    + `${Number(wf.total_rows) || 0} 行数据（表头不算）。`
    + "要换表就回全部工作流那一页重新上传，那会另建一个工作流。";
  const done = Number(wf.cursor) || 0;
  const total = Number(wf.total_rows) || 0;
  $("#wf-progress-text").textContent =
    `${done} / ${total} 行` + (wf.abort_requested ? " · 已请求中止" : "");

  // 详情页的失败数从**这份行数据**里数出来（列表端的聚合值在这里不够细：
  // 筛选、逐行排查都要对着真实的行）。
  const rows = d.rows || [];
  const failed = rows.filter((r) => r.status === "failed").length;
  const failedBox = $("#wf-failed");
  failedBox.textContent = failed > 0 ? `失败 ${failed} 行` : "没有失败行";
  failedBox.className = failed > 0 ? "who wf-failed is-bad" : "who";

  $("#wf-template").value = wf.prompt_template || "";
  $("#wf-writeback").checked = !!Number(wf.writeback);

  // 下载入口的判据是**服务端算的 result_ready**（有没有一张已经生成的结果表），
  // 不是 status === "finished"。后者是 M-1 死路的后半段：中止之后工作流停在
  // paused，而 paused 既不满足 finished、又已经把已算出的部分结果锁在磁盘上，
  // 用户点完中止既看不到也拿不到，只能再点一次「继续」把批次接着跑下去。
  // 规格 7.1 要的是「随时能下载**当前进度**」——有一份就够下。
  $("#wf-download").hidden = !wf.result_ready;
  $("#wf-download").href = `/api/workflows/${id}/result.xlsx`;

  // 动作按状态开关：跑完了再点开始没有意义，422/静默空转都不好看。
  // 「继续」只在 paused 可点——它在别的状态下要么 422 要么是空转，
  // 而缺了这颗按钮的话，用户点了暂停就在界面上再也回不去了。
  $("#wf-start").disabled = wf.status === "running" || wf.status === "finished";
  $("#wf-pause").disabled = wf.status !== "running";
  $("#wf-resume").disabled = wf.status !== "paused";
  $("#wf-abort").disabled = wf.status === "draft";

  colsModel = (d.columns || []).map((c) => ({
    col_index: Number(c.col_index),
    header: c.header || "",
    role: c.role,
    slot: c.slot || "",
    field: c.field || "",
  }));
  renderHead();
  renderRows(d);
  stepNow = stepByState();
  showStep();
  closeColPick();
  await loadPreview(0);
}

/** 表头 = 行号列 + 每一列。**点表头选角色**（规格 8.2）：心智是「看着表选列」，
    操作就该发生在表上——所以角色选择器就长在表头里，不再另开一个映射表单。 */
function renderHead() {
  const head = $("#wf-head");
  head.innerHTML = "";
  const headNum = document.createElement("th");
  headNum.className = "wf-rownum";
  headNum.textContent = "行";
  head.appendChild(headNum);
  colsModel.forEach((col, i) => {
    const th = document.createElement("th");
    th.className = "wf-th";
    th.dataset.col = String(col.col_index);
    th.title = "点这格改这一列的角色";
    th.textContent = col.header || "列" + (col.col_index + 1);
    const tag = document.createElement("span");
    tag.className = "wf-role-tag";
    tag.dataset.role = col.role;
    tag.textContent = ROLE_LABEL[col.role] || col.role;
    th.appendChild(tag);
    th.addEventListener("click", () => openColPick(i));
    head.appendChild(th);
  });
}

/** 打开某一列的角色弹层。**误触不改任何东西**：点表头只是把弹层打开，
    只有在弹层里点了一个角色才会写进列模型；「取消」直接关掉，什么都不变。 */
function openColPick(i) {
  const col = colsModel[i];
  if (!col) return;
  picked = i;
  $("#wf-colpick").hidden = false;
  $("#wf-colpick-title").textContent =
    `第 ${col.col_index + 1} 列「${col.header || "无表头"}」用作什么`;
  for (const b of $("#wf-roles").children) {
    b.className = "wf-role" + (b.dataset.role === col.role ? " is-on" : "");
  }
  const slot = $("#wf-slot");
  slot.hidden = !(col.role === "input" || col.role === "context");
  slot.value = col.slot || "";
  const field = $("#wf-field");
  field.hidden = col.role !== "output";
  field.value = col.field || OUTPUT_FIELDS[1];
}

/** 关弹层。改了角色的话把焦点还给刚才那一列表头。 */
function closeColPick() {
  picked = -1;
  $("#wf-colpick").hidden = true;
}

/** 换角色。变量名与结果字段跟着一起换——服务端只认整体替换的列组，
    半套配置会被 validate_binding 当成「变量没绑定」或「字段名不认识」驳回来。 */
function setRole(role) {
  const col = colsModel[picked];
  if (!col) return;
  col.role = role;
  if (role !== "input" && role !== "context") col.slot = "";
  if (role !== "output") col.field = "";
  if (role === "output" && !col.field) col.field = OUTPUT_FIELDS[1];
  renderHead();
  openColPick(picked);
  showStep();
  renderHere();
}

/** 从列模型读回列组——模型就是唯一事实，不另存一份状态。 */
function readCols() {
  return colsModel.map((c) => ({
    col_index: c.col_index,
    header: c.header,
    role: c.role,
    slot: (c.role === "input" || c.role === "context") ? (c.slot || "").trim() : "",
    field: c.role === "output" ? c.field : "",
  }));
}

function renderRows(d) {
  const body = $("#wf-body");
  body.innerHTML = "";
  // 表头是「行号列 + 每一列角色」共 1+N 格，所以数据行也必须 1+N 格。
  // 少写一个 td，浏览器会把后面的行整体左移，读起来像是列错位了。
  const cols = colsModel;
  // 「只看失败行」是**真筛**：非失败的行根本不进 DOM，不是给它们调透明度
  // ——筛完还让人以为「整批只有这几行跑过」比筛不出来更坏。
  const onlyFailed = !!$("#wf-only-failed").checked;
  let shown = 0;
  for (const row of d.rows) {
    if (onlyFailed && row.status !== "failed") continue;
    shown++;
    const tr = document.createElement("tr");
    tr.dataset.row = String(row.workflow_row);
    tr.addEventListener("click", () => guard("预览这一行", () => loadPreview(row.workflow_row)));
    const num = document.createElement("th");
    num.className = "wf-rownum";
    num.textContent = String(row.workflow_row + 1);
    num.title = row.title || "";
    tr.appendChild(num);
    for (const col of cols) {
      const td = document.createElement("td");
      td.dataset.col = String(col.col_index);
      if (col.role === "output") {
        // 结果列放行任务的状态，前面缀一颗状态色点：格子里的值在表格里，
        // 归档回 Excel 才有；这里显示状态，不是假装已经读到结果。
        const dot = document.createElement("span");
        dot.className = "wf-dot";
        dot.dataset.status = row.status || "queued";
        td.appendChild(dot);
        td.appendChild(document.createTextNode(
          ROW_STATUS_LABEL[row.status] || row.status || ""));
      }
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  // 筛掉了多少行要写出来：筛完不给对账，用户会以为表坏了。
  $("#wf-filter-note").textContent = onlyFailed
    ? `只看失败行：显示 ${shown} / ${d.rows.length} 行`
    : "";
}

/** 取第 rowIndex 行的单元格值，并把那一行标成当前行。
 *
 *  **提示词不在这里渲染**：/preview 渲染的是服务端**存好的**那一份，
 *  而规格 8.2 要的是「改完立刻预览」——还没保存的改动它压根看不见。
 *  所以这里只取值，渲染交给本地那条 renderHere。 */
async function loadPreview(rowIndex) {
  const d = await api("GET", `/api/workflows/${wfId}/preview?row=${rowIndex}`);
  curRow = rowIndex;
  $("#wf-preview-row").textContent = rowIndex + 1;
  previewValues = (d.values || {})[wfSheet] || {};
  for (const tr of $("#wf-body").children) {
    const on = tr.dataset.row === String(rowIndex);
    tr.className = on ? "is-current" : "";
    if (!on) continue;
    for (const td of tr.children) {
      if (!td.dataset) continue;
      const i = td.dataset.col;
      if (i === undefined) continue;
      const v = previewValues[i] === undefined ? "" : String(previewValues[i]);
      // 结果列那一格已经被状态占了，单元格值写在 title 上，不顶掉状态。
      if (td.title === "" || td.title === undefined) td.title = v;
    }
  }
  renderHere();
}

/** 就地渲染当前行的提示词。**不发请求**。
 *
 *  「立刻」的判据：改了列角色、变量名或模板之后，不点任何按钮，
 *  下面这一行的提示词就变了。
 *
 *  为什么本地算而不是拉接口：① 每敲一个字打一次预览端点会把接口打爆，
 *  节流也仍然是「打完还在等」；② 那个端点渲染的是**已保存**的配置，
 *  保存前它给的一直是旧句子——节流多少毫秒都做不到「立刻」。
 *  代价是这份槽位替换与服务端 workflow.render() 是两份实现，
 *  所以语法（SLOT_RE）与「没绑定的槽位渲染成空串」都写死成同一套，
 *  保存之后服务端那条路会再渲染一次盖回来，对不上时以服务端为准。
 */
function renderHere() {
  const slots = {};
  for (const c of colsModel) {
    if ((c.role === "input" || c.role === "context") && (c.slot || "").trim()) {
      slots[(c.slot || "").trim()] = previewValues[c.col_index];
    }
  }
  const tpl = $("#wf-template").value || "";
  const re = new RegExp(SLOT_RE.source, "g");
  $("#wf-preview").textContent = tpl.replace(re, (_m, name) => {
    const v = slots[name];
    return v === undefined || v === null ? "" : String(v);
  });
}

/* ---------------------------------------------------------------- 动作 */

async function saveMapping() {
  await api("POST", "/api/workflows", {
    workflow_id: wfId,
    prompt_template: $("#wf-template").value,
    columns: readCols(),
    // 回写开关跟着一起存（规格 7.1）。服务端真的按它决定要不要写
    // result.xlsx——一个后端不读的勾选框比没有这个框更危险。
    writeback: $("#wf-writeback").checked,
  });
  await openDetail(wfId);
}

async function control(action) {
  await api("POST", `/api/workflows/${wfId}/${action}`);
  await openDetail(wfId);
}

/* 下载结果表走 fetch + blob，**不是**让 <a> 直接导航过去。
 *
 * 原因：令牌在 localStorage 里，不在 Cookie 里（app.js 同一条口径），而
 * <a href> 的导航请求不会带 Authorization 头 —— 实测点了只会拿到一个
 * 401 的 JSON，页面不会报任何错，用户看到的是「点了没反应」。
 * 同一个地址带上 Bearer 头就是 200，所以这条路是死的，不是服务端的问题。 */
async function downloadResult() {
  const r = await fetch(apiBase() + `/api/workflows/${wfId}/result.xlsx`,
                        { headers: authHeaders() });
  if (!r.ok) throw await errFrom(r);
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "结果表.xlsx";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

$("#wf-download").addEventListener("click", (e) => {
  e.preventDefault();
  guard("下载结果表", downloadResult);
});

/* 角色选择器里的五颗按钮在页面加载时就建好：它们不依赖任何一份行数据，
   建一次就一直能用，改角色只切 class，不重建。 */
for (const r of ROLES) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "wf-role";
  b.dataset.role = r;
  b.textContent = ROLE_LABEL[r];
  b.addEventListener("click", () => setRole(r));
  $("#wf-roles").appendChild(b);
}

for (const f of OUTPUT_FIELDS) {
  const o = document.createElement("option");
  o.value = f;
  o.textContent = OUTPUT_LABEL[f];
  $("#wf-field").appendChild(o);
}

$("#wf-back").addEventListener("click", () => guard("返回列表", loadList));
$("#wf-prev").addEventListener("click", () => gotoStep(stepNow - 1));
$("#wf-next").addEventListener("click", () => gotoStep(stepNow + 1));
$("#wf-save").addEventListener("click", () => guard("保存映射", saveMapping));
$("#wf-start").addEventListener("click", () => guard("开始", () => control("start")));
$("#wf-pause").addEventListener("click", () => guard("暂停", () => control("pause")));
$("#wf-resume").addEventListener("click", () => guard("继续", () => control("resume")));
/* 切筛选只是重绘：不该打接口，更不该顺手改任何一行任务的状态。 */
$("#wf-only-failed").addEventListener("change", () => {
  if (lastDetail) renderRows(lastDetail);
});

/* 改模板与改变量名都要**立刻**出预览：这两个是「一个字一个字敲」的输入框，
   绑 change 就等于要用户先失焦才看得到变化。 */
$("#wf-template").addEventListener("input", () => {
  showStep();
  renderHere();
});
$("#wf-slot").addEventListener("input", () => {
  const col = colsModel[picked];
  if (!col) return;
  col.slot = $("#wf-slot").value;
  showStep();
  renderHere();
});
$("#wf-field").addEventListener("change", () => {
  const col = colsModel[picked];
  if (!col) return;
  col.field = $("#wf-field").value;
  renderHead();
});
$("#wf-writeback").addEventListener("change", () => {
  if (stepNow === 2) renderSummary();
});
$("#wf-colpick-close").addEventListener("click", () => {
  /* 取消只是关掉弹层。**不撤销**：点开弹层那一刻还没改过任何东西，
     所以「取消」等于什么都没发生——真要撤销上一次的角色变更，
     用户点回原来的角色即可，不必在这里维护一份撤销栈。 */
  closeColPick();
});

/* 中止是危险动作：点一下就停掉正在跑的那行。所以要两步——
   第一下只把按钮改成「再点一下就中止」，第二下才真发请求。
   不用原生确认框：样式不受控，一关输入就没了。 */
let abortArmed = false;
$("#wf-abort").addEventListener("click", () => {
  const btn = $("#wf-abort");
  if (!abortArmed) {
    abortArmed = true;
    btn.textContent = "再点一下就中止整批";
    btn.className = "danger";
    setTimeout(() => {
      abortArmed = false;
      btn.textContent = "中止整批（立刻停掉当前行）";
      btn.className = "secondary";
    }, 5000);
    return;
  }
  abortArmed = false;
  guard("中止整批", () => control("abort"));
});

$("#wf-upload").addEventListener("click", async () => {
  const f = $("#wf-file").files[0];
  if (!f) { say("先选一个 xlsx 表格。"); return; }
  $("#wf-upload-state").textContent = "上传中…";
  // FormData 自己带 boundary，**不能**手写 Content-Type，
  // 否则浏览器不会补 boundary，服务端那边 multipart 解析直接失败。
  const fd = new FormData();
  fd.append("file", f);
  await guard("上传表格", async () => {
    const r = await fetch(apiBase() + "/api/workflows/upload",
                           { method: "POST", headers: authHeaders(), body: fd });
    if (!r.ok) throw await errFrom(r);
    const d = await r.json();
    $("#wf-upload-state").textContent =
      `${f.name} 共 ${d.row_count} 行，去配列映射和模板再开始`;
    await loadList();
  });
});

boot();
