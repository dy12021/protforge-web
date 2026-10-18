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
      <div class="card-meta"><span class="card-time"></span></div>`;
    el.querySelector(".card-title").textContent = wf.name;
    el.querySelector(".card-id").textContent =
      `${WF_STATUS_LABEL[wf.status] || wf.status} · ${done} / ${total} 行`;
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
  $("#wf-name").textContent = wf.name;
  $("#wf-sheet").textContent = wfSheet ? "工作表 " + wfSheet : "";
  const done = Number(wf.cursor) || 0;
  const total = Number(wf.total_rows) || 0;
  $("#wf-progress-text").textContent =
    `${done} / ${total} 行` + (wf.abort_requested ? " · 已请求中止" : "");
  $("#wf-template").value = wf.prompt_template || "";

  // 下载链接按 **status === 'finished'** 判，不按 writeback 判：
  // _wf_view() 把 writeback 明确 pop 掉了（v1 不暴露这个开关），
  // 所以 `!wf.writeback` 恒为真 —— 那个链接**永远不会出现**。
  // 而且服务端在没配结果列时直接 404，不该让用户点一个必然失败的链接。
  $("#wf-download").hidden = wf.status !== "finished";
  $("#wf-download").href = `/api/workflows/${id}/result.xlsx`;

  // 动作按状态开关：跑完了再点开始没有意义，422/静默空转都不好看。
  $("#wf-start").disabled = wf.status === "running" || wf.status === "finished";
  $("#wf-pause").disabled = wf.status !== "running";
  $("#wf-abort").disabled = wf.status === "draft";

  renderCols(d.columns);
  renderRows(d);
  await loadPreview(0);
}

/* 列角色做在表头上：心智是「看着表选列」，操作就该发生在表上（规格 8.2）。
   变量名与结果字段跟角色一起改——服务端只认整体替换的列组，半套配置会被
   validate_binding 当成「变量没绑定」或「字段名不认识」驳回来。 */
function renderCols(cols) {
  const box = $("#wf-cols");
  box.innerHTML = "";
  for (const col of cols) {
    const row = document.createElement("div");
    row.className = "toolbar";
    const name = document.createElement("span");
    name.className = "who";
    name.textContent = `第 ${col.col_index + 1} 列：${col.header || "（无表头）"}`;
    row.appendChild(name);

    const sel = document.createElement("select");
    sel.dataset.role = "1";
    for (const r of ROLES) {
      const o = document.createElement("option");
      o.value = r;
      o.textContent = ROLE_LABEL[r];
      if (col.role === r) o.selected = true;
      sel.appendChild(o);
    }
    row.appendChild(sel);

    const slot = document.createElement("input");
    slot.type = "text";
    slot.dataset.slot = "1";
    slot.value = col.slot || "";
    slot.placeholder = "变量名";
    row.appendChild(slot);

    const field = document.createElement("select");
    field.dataset.field = "1";
    for (const f of OUTPUT_FIELDS) {
      const o = document.createElement("option");
      o.value = f;
      o.textContent = OUTPUT_LABEL[f];
      if (col.field === f) o.selected = true;
      field.appendChild(o);
    }
    row.appendChild(field);

    // 只显示当前角色用得上的那一格。角色一变就换，不让它留着占位。
    const sync = () => {
      const role = sel.value;
      slot.hidden = !(role === "input" || role === "context");
      field.hidden = role !== "output";
    };
    sel.addEventListener("change", sync);
    sync();
    box.appendChild(row);
  }
}

/** 从界面上的控件读回列组——控件就是唯一事实，不另存一份状态。 */
function readCols() {
  const out = [];
  for (const row of $("#wf-cols").children) {
    const name = row.querySelector(".who").textContent;
    const role = row.querySelector('[data-role]').value;
    const slot = row.querySelector('[data-slot]').value.trim();
    const field = row.querySelector('[data-field]').value;
    const m = name.match(/^第 (\d+) 列/);
    out.push({
      col_index: Number(m[1]) - 1,
      header: name.slice(name.indexOf("：") + 1),
      role,
      // 不相关的两格一律清空：留着旧值会让服务端把一列当成既绑了变量
      // 又要写回，驳回的理由还落在另一列上。
      slot: role === "input" || role === "context" ? slot : "",
      field: role === "output" ? field : "",
    });
  }
  return out;
}

function renderRows(d) {
  const body = $("#wf-body");
  body.innerHTML = "";
  // 表头是「行号列 + 每一列角色」共 1+N 格，所以数据行也必须 1+N 格。
  // 少写一个 td，浏览器会把后面的行整体左移，读起来像是列错位了。
  const cols = d.columns;
  const head = $("#wf-head");
  head.innerHTML = "";
  head.appendChild(document.createElement("th"));
  for (const col of cols) {
    const th = document.createElement("th");
    th.textContent = `${col.header || "列" + (col.col_index + 1)}（${ROLE_LABEL[col.role] || col.role}）`;
    head.appendChild(th);
  }
  for (const row of d.rows) {
    const tr = document.createElement("tr");
    tr.dataset.row = String(row.workflow_row);
    tr.addEventListener("click", () => guard("预览这一行", () => loadPreview(row.workflow_row)));
    const th = document.createElement("th");
    th.textContent = String(row.workflow_row + 1);
    th.title = row.title || "";
    tr.appendChild(th);
    for (const col of cols) {
      const td = document.createElement("td");
      td.dataset.col = String(col.col_index);
      if (col.role === "output") {
        // 结果列放行任务的状态：格子的值在表格里，归档回 Excel 才有；
        // 这里显示状态，不是假装已经读到结果。
        td.textContent = ROW_STATUS_LABEL[row.status] || row.status || "";
      }
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
}

/** 预览第 rowIndex 个数据行：既取渲染后的提示词，也把那行的单元格填回表格。 */
async function loadPreview(rowIndex) {
  const d = await api("GET", `/api/workflows/${wfId}/preview?row=${rowIndex}`);
  $("#wf-preview-row").textContent = rowIndex + 1;
  $("#wf-preview").textContent = d.rendered || "";
  const values = (d.values || {})[wfSheet] || {};
  for (const tr of $("#wf-body").children) {
    const on = tr.dataset.row === String(rowIndex);
    tr.className = on ? "is-current" : "";
    if (!on) continue;
    for (const td of tr.children) {
      if (!td.dataset) continue;
      const i = td.dataset.col;
      if (i === undefined) continue;
      const v = values[i] === undefined ? "" : String(values[i]);
      // 结果列那一格已经被状态占了，单元格值写在 title 上，不顶掉状态。
      if (td.title === "" || td.title === undefined) td.title = v;
    }
  }
}

/* ---------------------------------------------------------------- 动作 */

async function saveMapping() {
  await api("POST", "/api/workflows", {
    workflow_id: wfId,
    prompt_template: $("#wf-template").value,
    columns: readCols(),
  });
  await openDetail(wfId);
}

async function control(action) {
  await api("POST", `/api/workflows/${wfId}/${action}`);
  await openDetail(wfId);
}

$("#wf-back").addEventListener("click", () => guard("返回列表", loadList));
$("#wf-save").addEventListener("click", () => guard("保存映射", saveMapping));
$("#wf-start").addEventListener("click", () => guard("开始", () => control("start")));
$("#wf-pause").addEventListener("click", () => guard("暂停", () => control("pause")));

/* 中止是危险动作：点一下就停掉正在跑的那行。所以要两步——
   第一下只把按钮改成「再点一下就中止」，第二下才真发请求。
   不用原生 confirm：样式不受控，一关输入就没了。 */
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
