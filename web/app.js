/* 自动化计算平台 · 用户面板前端
 *
 * 边界：这里只读写 REST API。计算怎么跑、编排哪条技能链，全在后台进程里，
 * 前端不参与——它只负责让人看见"现在在干什么"。
 *
 * 三条硬口径：
 *   1. 界面口径统一「自动化计算平台」「计算任务」，测试 test_web_static.py
 *      会把违规字眼连注释一起扫出来。
 *   2. 零外链零构建：只用同源 style.css / app.js，字体全是系统自带。
 *   3. 鉴权走 Authorization: Bearer（不放 Cookie），所以实时流不能用
 *      EventSource（它发不了自定义头），必须 fetch + ReadableStream 自己读。
 *
 * 这一版把**工作流并进了看板**：以前工作流是另一个页面（workflow.html），
 * 现在它是看板上的主卡片，展开就是行列表；新建与修改工作流也并进了
 * 「提交计算任务」这个对话框的两种模式。
 */

/* ---------------------------------------------------------------- 状态机
 *
 * 流转轨**只画这四站**（主链）：灰 → 暖 → 紫 → 绿。failed 是终点不在主链上，
 * 占了轨就变成"五段进度条"，反而读不出走到哪一步。
 * failed 时只把当前站染红 + 卡片加红边，**绝不把整条线涂红**。 */
const STATUS_ORDER = ["queued", "running", "packaging", "done"];
const STATUS_LABEL = {
  queued: "已排队", running: "计算中", packaging: "打包中",
  done: "已完成", failed: "失败",
};
/* 看板五列：四站主链 + 失败。失败任务也得有地方待着，不能凭空消失。 */
const COLUMNS = [
  { key: "queued", label: "已排队" },
  { key: "running", label: "计算中" },
  { key: "packaging", label: "打包中" },
  { key: "done", label: "已完成" },
  { key: "failed", label: "失败" },
];
const ACTIVE = new Set(STATUS_ORDER.slice(0, 3));   // 已排队/计算中/打包中 = 还在跑

/* 显示层翻译表。服务端 event.message 是 API 契约（中文）不能改，
 * 所以裸字段名的翻译放在这里——满屏 body/brief 正是"像调试模式"的另一半。 */
const KIND_LABEL = {
  created: "创建", status: "状态变更", log: "输出", note: "说明",
  upload: "上传", package: "打包", error: "报错", edited: "修改",
  step: "步骤", finished: "收口",
};
const ACTOR_LABEL = {
  human: "提交人", agent: "计算进程", admin: "管理员", system: "系统",
};
const FIELD_CN = {
  title: "标题", brief: "需求说明", step: "当前步骤", status: "状态",
  summary: "结果说明", error: "失败原因", review_mode: "验收方式",
};

/* 验收方式。三级：任务 > 工作流 > 全局默认；**空串 = 继承上层**。
 *
 * 词面上与这个界面其余部分保持一致：跑计算的是「计算进程」，
 * 审计算结果的那个也是平台的一部分——它是「审核进程」。 */
const REVIEW_MODE_LABEL = {
  human: "人工 + 条件判定",
  llm: "机器审核",
};
const REVIEW_MODE_SOURCE = {
  task: "这一轮单独设的",
  workflow: "跟着这一批",
  global: "跟着平台默认",
};

/* ---------------------------------------------- 工作流的词表
 *
 * 工作流的 status 词与行任务**不是**一套：workflows 表的 CHECK 是
 * draft/running/paused/finished，终态是 finished；行任务才是 done/failed。
 * 混用会写出永远不触发的分支。 */
const WF_STATUS_LABEL = {
  draft: "未开始", running: "运行中", paused: "已暂停", finished: "已跑完",
};
/* 工作流状态 → 芯片类。颜色只借任务那五个（灰=排队、暖=在算、紫=打包、
 * 绿=完成、红=失败），不另起一套色：同一个界面里两种语义用两套颜色，
 * 读的人就要先学一遍。 */
const WF_CHIP = {
  draft: "chip-queued", running: "chip-running",
  paused: "chip-none", finished: "chip-done",
};
/* 行任务的第五种显示态：跳过。库里它是 failed（tasks.status 没有 skipped），
 * 界面按 workflow_skips 推出来——跳过的行「没跑过」，与「跑失败了」是两件事。 */
const ROW_STATUS_LABEL = {
  queued: "已排队", running: "计算中", packaging: "打包中",
  done: "已完成", failed: "失败", skipped: "已跳过",
};
/* ---------------------------------------------- 验收
 *
 * 「算完」与「交付完成」是**两根轴**：status 说算没算完，acceptance 说
 * 交没交付。模型自己调 finish 只说明它觉得算完了，平台原来照单全收——
 * 交上来什么就打包、标绿、归档。
 *
 * 界面上的「待验收」= status 是 done 且 acceptance 是 pending。
 * **别只看 status**：只看 status 的话，待验收的任务和验收通过的任务
 * 长得一模一样，「结果没人认」这件事就完全看不见了。 */
const ACCEPTANCE_LABEL = {
  none: "", pending: "待验收", accepted: "已验收", rejected: "验收没通过",
};

/* 这个任务是不是在等人验收。 */
function awaitingReview(t) {
  return !!t && t.status === "done" && t.acceptance === "pending";
}

/* 验收状态的芯片。**只有待验收与没通过才显示**——已验收是默认状态，
 * 到处挂一个「已验收」标签只会把真正要人看的那两个淹掉。 */
function reviewChip(t) {
  const a = (t && t.acceptance) || "none";
  if (a === "none") return null;
  if (a === "accepted") return null;
  const cls = a === "rejected" ? "chip chip-failed" : "chip chip-review";
  return el("span", cls, ACCEPTANCE_LABEL[a] || a);
}

const WF_ROLES = ["ignore", "key", "input", "context", "output"];
const WF_ROLE_LABEL = {
  ignore: "忽略", key: "标识列", input: "输入列",
  context: "上下文", output: "结果列",
};
/* 结果字段是服务端白名单，写一个不在表里的名字整条保存会 422。 */
const WF_OUTPUT_FIELDS = ["status", "summary", "error", "artifacts"];
const WF_OUTPUT_LABEL = {
  status: "状态", summary: "结果说明", error: "失败原因", artifacts: "产物清单",
};

/* ---------------------------------------------- 筛选条的统一状态词
 *
 * 任务与工作流各有一套状态词，但**筛选项只有一套**——否则「全部」视图下
 * 一个下拉框没法同时筛两种东西。每一条筛选项写清楚它盖住哪些状态：
 * 任务那边一套、工作流那边一套，缺一边的就是「这一类在这一档上没有对应」。 */
const FILTER_OPTIONS = [
  { key: "", label: "全部状态" },
  { key: "active", label: "进行中" },
  { key: "queued", label: "排队中" },
  { key: "running", label: "计算中" },
  { key: "packaging", label: "打包中" },
  { key: "paused", label: "已暂停" },
  { key: "done", label: "已完成" },
  { key: "pending", label: "待验收" },
  { key: "failed", label: "失败" },
];
const FILTER_TASKS = {
  active: ["queued", "running", "packaging"],
  queued: ["queued"], running: ["running"], packaging: ["packaging"],
  paused: [], done: ["done"], failed: ["failed"],
  /* 「待验收」**不是状态**，是另一根轴（acceptance）——不走这张表，
   * 在 filtered() 里单独判。留个空数组是为了让「这一档不按 status 过滤」
   * 这件事在表上看得见。 */
  pending: [],
};
const FILTER_WORKFLOWS = {
  active: ["draft", "running", "paused"],
  queued: ["draft"], running: ["running"], packaging: [],
  paused: ["paused"], done: ["finished"], failed: [],
  /* 工作流这边同理：「待验收」指的是**这一批里还有行没人认**，
   * 不是工作流这个对象的状态。 */
  pending: [],
};
/* 「只看在跑的」覆盖的那一档（与 active 同一口径）。 */
const ACTIVE_TASKS = ["queued", "running", "packaging"];
const ACTIVE_WORKFLOWS = ["draft", "running", "paused"];

/* ------------------------------------------------- 时间线的事件分档
 *
 * 关键事件（状态变更、报错、收口、打包起止）单独一路突出；
 * 过程输出（日志、说明、上传、修改）降级成一组，收起来。
 *
 * **step 事件不进时间线**：一次计算里它能被调用几百次，混进来时间线就被刷屏，
 * 而"当前在干什么"只需要卡片上那一条。 */
const KEY_KINDS = new Set(["created", "status", "error", "finished", "package"]);
const TIMELINE_FILTERS = [
  { key: "all", label: "全部" },
  { key: "key", label: "关键事件" },
  { key: "error", label: "只看报错" },
  { key: "status", label: "只看状态变化" },
];

const state = {
  items: [],            // GET /api/tasks 的快照（最新在前）
  byId: new Map(),      // id -> task
  cards: new Map(),     // id -> 卡片元素（复用，不要每次重建：重建就没有滑轨动画）
  events: new Map(),    // taskId -> Map(eventId -> event)
  reached: new Map(),   // taskId -> 走到的站序；failed 时用它把红点放对位置
  streams: new Map(),   // taskId -> { stop, ac, lastId }
  cols: new Map(),      // status -> 列的 .col-list
  filter: { q: "", status: "", activeOnly: false, view: "all" },
  workflows: [],        // GET /api/workflows 的快照
  wfById: new Map(),    // workflowId -> workflow
  wfCards: new Map(),   // workflowId -> 卡片元素（复用）
  wfOpen: new Set(),    // 展开了哪些工作流
  wfDetail: new Map(),  // workflowId -> 详情快照（行列表）
  wfStatus: new Map(),  // workflowId -> /api/status/workflow/{id} 的快照
  wfEdit: null,         // 对话框当前在配的工作流（null = 新建）
  wfDraft: null,        // 对话框里已上传的表（{workflow, headers, row_count}）
  wfCols: [],           // 对话框里的列模型（唯一事实）
  timelineFilter: "all",
  timelineOpen: false,  // 过程输出那一组是展开还是收起
  openId: null,         // 抽屉当前开着的任务
  drawerMsg: null,      // 抽屉里的提示条（存 state，重绘时才贴得住）
  taskArts: new Map(),  // taskId -> 产物清单（验收区要显示「这一轮交了什么」）
  taskReview: new Map(), // taskId -> {acceptance, failures}（验收没过的原因）
  editing: false,       // 抽屉里有字段正在编辑 —— 此时**不重绘抽屉**
  me: null,
  canEdit: true,        // 服务端没开放编辑时降级成纯只读（见 PATCH 分支）
  canDelete: true,
};

const TOKEN_KEY = "protforge.token";

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
function setToken(v) { if (v) localStorage.setItem(TOKEN_KEY, v); else localStorage.removeItem(TOKEN_KEY); }

/* base 是**源前缀**，不含 /api。所有调用处传的都是 "/api/xxx" 完整路径，
 * 这里再拼一次 "/api" 会变成 /api/api/xxx —— 整块面板的接口全部 404，
 * 而页面骨架是静态 HTML，照样渲染得很正常，看着像"能打开"。
 *
 * ?base= 覆盖口保留：开发时后端不在同一个源，填 http://127.0.0.1:8473 这样
 * 的**源**（不要带 /api），最终拼成 http://127.0.0.1:8473/api/xxx。 */
function apiBase() {
  const q = new URLSearchParams(location.search).get("base");
  const b = q || window.PROTFORGE_WEB_BASE || "";
  return b ? b.replace(/\/+$/, "") : "";
}

function authHeaders(lastEventId) {
  const h = {};
  const t = token();
  if (t) h["Authorization"] = "Bearer " + t;
  if (lastEventId != null) h["Last-Event-ID"] = String(lastEventId);
  return h;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// detail 恒为中文字符串（含 pydantic 框架层错误）。数组透出去前端只会拿到
// "[object Object]"，所以只认字符串。
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
  if (!r.ok) {
    const e = await errFrom(r);
    e.status = r.status;                 // 401 要走重新登录，404/405 要走能力降级
    throw e;
  }
  if (r.status === 204) return null;
  return r.json();
}

// ---------------------------------------------------------------- 小工具

/* 任务详情类接口（POST /api/tasks、PATCH /api/tasks/{id}、POST …/abort）
 * 返回的都是**信封**：{ task, events, artifacts }，任务本体在 got.task 里。
 *
 * 早先这几处把返回值直接当任务对象用，于是：
 *   - 提交后 openDrawer(got.id) 打开的是 undefined，请求 /api/tasks/undefined 得 422；
 *   - 改完标题或需求说明，存进 state.byId 的是整个信封，抽屉里字段全空。
 * 症状都是"点了没反应"——请求要么 422、要么干脆不报错。
 *
 * 这里**故意不写成 got.task || got**：容错等于把形状不匹配重新藏起来，
 * 而藏形状不匹配正是这个洞的成因。形状真变了就让它当场炸出一句中文，
 * 至少能在界面上看见，而不是抽屉里一片空白。 */
function detailTask(got, where) {
  const t = got && got.task;
  if (!t || typeof t !== "object" || t.id == null) {
    throw new Error("服务端返回的任务详情格式变了（" + where + " 取不到 task 字段）");
  }
  return t;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null && text !== "") n.textContent = text;
  return n;
}

/* ---------------------------------------------------------------- 工具市场
 *
 * 手动模式：**直接调工具**，不经 agent。
 *
 * 工具清单来自 API（服务端扫技能目录得到），每个工具有一份**参数契约**——
 * 契约外的参数名、类型不对、必填缺了，服务端一律拒，不猜。
 */
const MT = { tools: [], cat: "", q: "", sel: null };

/* 往一个 .market-msg 元素里写一行提示。
 *
 * **第三个参数是元素本身，不是 id 字符串**：静态检查靠正则扫
 * getElementById(...) 才知道脚本碰了哪些 DOM id，写成
 * getElementById(hostId || "mt-msg") 的话那个 id 在它眼里就没被引用过，
 * HTML 里对应的元素会变成没人认领的孤儿。
 */
function mtMsg(text, kind, host) {
  const n = host || document.getElementById("mt-msg");
  if (!n) return;
  n.hidden = !text;
  n.textContent = text || "";
  n.className = "market-msg" + (kind ? " is-" + kind : "");
}

async function loadTools() {
  const r = await api("GET", "/api/tools");
  MT.tools = r.tools || [];
}

function mtFiltered() {
  const q = MT.q.trim().toLowerCase();
  return MT.tools.filter((t) => {
    if (MT.cat && t.category !== MT.cat) return false;
    if (!q) return true;
    return ((t.display_name || "") + " " + (t.id || "") + " "
            + (t.category || "") + " " + (t.description || ""))
      .toLowerCase().includes(q);
  });
}

function mtCard(t) {
  const c = el("div", "mcard");
  const head = el("div", "mcard-head");
  head.appendChild(el("span", "mcard-name", t.display_name || t.id));
  if (t.batch) head.appendChild(el("span", "mcard-badge", "batch"));
  c.appendChild(head);
  c.appendChild(el("div", "mcard-id", (t.id || "") + " v" + (t.version || "1.0")));
  if (t.category) c.appendChild(el("span", "mcard-cat", t.category));
  const foot = el("div", "mcard-foot");
  foot.appendChild(el("span", "mcard-state",
    t.directly_callable ? "可直接调用" : "需 agent 辅助"));
  c.appendChild(foot);
  c.addEventListener("click", () => mtOpen(t.id));
  return c;
}

function mtRenderCats() {
  const box = document.getElementById("mt-cats");
  box.textContent = "";
  const cats = [...new Set(MT.tools.map((t) => t.category).filter(Boolean))].sort();
  const mk = (label, value) => {
    const b = el("button", "seg-btn" + (MT.cat === value ? " is-on" : ""), label);
    b.type = "button";
    b.addEventListener("click", () => { MT.cat = value; mtRenderCats(); mtRenderGrid(); });
    box.appendChild(b);
  };
  mk("全部", "");
  cats.forEach((c) => mk(c, c));
}

function mtRenderGrid() {
  const grid = document.getElementById("mt-grid");
  const empty = document.getElementById("mt-empty");
  grid.textContent = "";
  const list = mtFiltered();
  empty.hidden = list.length > 0;
  list.forEach((t) => grid.appendChild(mtCard(t)));
}

async function openMarket() {
  const dlg = document.getElementById("market");
  document.getElementById("mt-detail").hidden = true;
  document.getElementById("mt-grid").hidden = false;
  document.getElementById("mt-search").hidden = false;
  document.getElementById("mt-cats").hidden = false;
  try {
    await loadTools();
  } catch (e) {
    mtMsg("工具清单取不到：" + (e.message || e), "bad");
    return;
  }
  document.getElementById("mt-title").textContent =
    "工具市场（" + MT.tools.length + " 个）";
  mtRenderCats();
  mtRenderGrid();
  dlg.showModal();
}

function mtOpen(id) {
  const t = MT.tools.find((x) => x.id === id);
  if (!t) return;
  MT.sel = t;
  document.getElementById("mt-grid").hidden = true;
  document.getElementById("mt-search").hidden = true;
  document.getElementById("mt-cats").hidden = true;
  document.getElementById("mt-empty").hidden = true;
  const det = document.getElementById("mt-detail");
  det.hidden = false;
  document.getElementById("mt-d-title").textContent = t.display_name || t.id;
  document.getElementById("mt-d-meta").textContent =
    (t.id || "") + " v" + (t.version || "1.0")
    + (t.category ? " · " + t.category : "")
    + " · 超时上限 " + Math.round((t.timeout_s || 600) / 60) + " 分钟"
    + (t.description ? " · " + t.description.slice(0, 160) : "");
  const box = document.getElementById("mt-params");
  box.textContent = "";
  (t.params || []).forEach((p) => {
    const lab = el("label", "field");
    lab.appendChild(el("span", "field-k", p.label || p.name
      + (p.required ? "（必填）" : "")));
    let input;
    if (p.type === "int" || p.type === "float") {
      input = el("input", "market-input");
      input.type = "number";
      if (p.type === "int") input.step = "1"; else input.step = "any";
    } else if (p.type === "bool") {
      input = el("input", "market-input market-check");
      input.type = "checkbox";
    } else {
      input = el("input", "market-input");
      input.type = "text";
    }
    input.id = "mtp-" + p.name;
    input.dataset.ptype = p.type;
    input.dataset.pname = p.name;
    input.value = p.default === undefined || p.default === null ? "" : String(p.default);
    lab.appendChild(input);
    if (p.description) lab.appendChild(el("span", "field-hint", p.description));
    box.appendChild(lab);
  });
  const batch = document.getElementById("mt-batch");
  batch.checked = false;
  document.getElementById("mt-batch-list").hidden = true;
  document.getElementById("mt-run").disabled = !t.directly_callable;
  document.getElementById("mt-run").textContent = t.directly_callable ? "运行" : "这个工具不能直接调用";
  // 换工具就换一句话：上一句是给别的工具说的，别带过来。
  document.getElementById("mt-req").value = "";
  mtMsg("", "", document.getElementById("mt-fill-msg"));
  // 文档：工具的说明书就是它的文档（技能仓里那份 SKILL.md 原文）。
  loadToolDoc(t);
  // **打开就滑到底部**：点一个工具是为了跑它，参数表单和运行按钮在下面。
  // 用 requestAnimationFrame 等这一帧布局落定——不等的话 scrollHeight 还是旧的，
  // 参数多的时候差一屏，滑不到位。
  requestAnimationFrame(() => {
    const dlg = document.getElementById("market");
    if (dlg) dlg.scrollTop = dlg.scrollHeight;
  });
}

/* 取一个工具的详细文档（它的 SKILL.md 原文）。
 *
 * **平台不另写一份文档**：技能仓里那份 SKILL.md 才是「这个工具到底怎么用、
 * 有哪些坑」的权威来源。写两份就会漂移——而漂移出来的文档比没文档更坏。 */
async function loadToolDoc(t) {
  const box = document.getElementById("mt-doc");
  const sum = document.getElementById("mt-doc-sum");
  if (!box) return;
  box.textContent = "（正在取文档…）";
  try {
    const d = await api("GET", "/api/tools/" + encodeURIComponent(t.id) + "/doc");
    box.textContent = d.doc || "（这个工具没有文档）";
    if (sum) sum.textContent = "详细文档 · " + (d.skill || t.id);
  } catch (e) {
    box.textContent = "文档取不到：" + (e.message || e);
  }
}

/* agent 辅助填参数：一句自然语言要求 → 契约参数。
 *
 * **执行仍然由脚本做**：agent 在这里只干「翻译」这一件事，没有执行权，
 * 也改不了跑什么命令。填歪的参数会被契约校验挡住（类型不对、契约外的名字
 * 一律拒），填不出来的必填项摆出来让人补——不编。 */
async function mtFillParams() {
  const t = MT.sel;
  if (!t) return;
  const reqBox = document.getElementById("mt-req");
  const req = (reqBox ? reqBox.value : "").trim();
  if (!req) {
    mtMsg("先说说你想算什么", "bad", document.getElementById("mt-fill-msg"));
    return;
  }
  const btn = document.getElementById("mt-fill");
  if (btn) btn.disabled = true;
  mtMsg("正在把这句话翻译成参数…", "", document.getElementById("mt-fill-msg"));
  try {
    const r = await api("POST",
                        "/api/tools/" + encodeURIComponent(t.id) + "/fill-params",
                        { requirement: req });
    const params = r.params || {};
    Object.keys(params).forEach((name) => {
      const inp = document.getElementById("mtp-" + name);
      if (!inp) return;
      if (inp.dataset.ptype === "bool") { inp.checked = !!params[name]; return; }
      const v = params[name];
      inp.value = (v === null || v === undefined) ? "" : String(v);
    });
    const miss = r.missing || [];
    let msg = "填了 " + Object.keys(params).length + " 个参数";
    if (miss.length) msg += "；这几项还得你补：" + miss.join("、");
    if (r.notes) msg += "（" + r.notes + "）";
    mtMsg(msg, miss.length ? "" : "ok", document.getElementById("mt-fill-msg"));
    // 填完把参数表单带到眼前——不然填完还得自己找。
    const pbox = document.getElementById("mt-params");
    if (pbox && pbox.scrollIntoView) pbox.scrollIntoView({ block: "nearest" });
  } catch (e) {
    mtMsg("填不出来：" + (e.message || e), "bad",
          document.getElementById("mt-fill-msg"));
  } finally {
    if (btn) btn.disabled = false;
  }
}

function mtCollectParams() {
  const out = {};
  document.querySelectorAll("#mt-params [data-pname]").forEach((inp) => {
    const name = inp.dataset.pname;
    const type = inp.dataset.ptype;
    if (type === "bool") { out[name] = inp.checked; return; }
    const v = inp.value.trim();
    if (v === "") return;
    out[name] = (type === "int" || type === "float") ? Number(v) : v;
  });
  return out;
}

async function mtRun() {
  const t = MT.sel;
  if (!t) return;
  const params = mtCollectParams();
  const body = { params: params };
  const batchOn = document.getElementById("mt-batch").checked;
  if (batchOn) {
    const lines = document.getElementById("mt-batch-list").value
      .split("\n").map((s) => s.trim()).filter(Boolean);
    if (!lines.length) { mtMsg("批量模式至少要写一行输入", "bad"); return; }
    // 每行一个输入：**第一个参数**取这行，其余参数共享表单里的值。
    const first = (t.params || [])[0];
    if (!first) { mtMsg("这个工具没有可批量的参数", "bad"); return; }
    body.inputs = lines.map((line) => {
      const one = Object.assign({}, params);
      one[first.name] = line;
      return one;
    });
    body.params = {};
  }
  try {
    const r = await api("POST", "/api/tools/" + encodeURIComponent(t.id) + "/run", body);
    document.getElementById("market").close();
    mtMsg("");
    setMsg(document.getElementById("board-msg"),
           (r.tasks ? "已排入 " + r.tasks.length + " 个任务" : "已排入队列")
           + "：可以到任务列表看进度", "ok");
    await load();
  } catch (e) {
    mtMsg("跑不起来：" + (e.message || e), "bad");
  }
}

/* 时间戳：'2026-10-16T14:03:22.123' -> '10-16 14:03'。
 * 中间那个 T 是 ISO 格式留下的，不去掉就不是给人看的。 */
function fmtTime(ts) {
  if (typeof ts !== "string" || ts.length < 16 || ts[10] !== "T") return ts || "";
  return ts.slice(5, 10) + " " + ts.slice(11, 16);
}

/* 字节 → 人看得懂的大小。验收区要显示「这一轮交了多少东西」，
 * 光报一个字节数没人读得出来。 */
function fmtSize(bytes) {
  let n = Number(bytes || 0);
  if (!isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n = n / 1024; i += 1; }
  return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
}

/* 耗时：秒 -> 人看得懂的样子。跑了 3 小时的和跑了 40 秒的要能一眼分开。 */
function fmtDur(sec) {
  const n = Number(sec);
  if (!isFinite(n) || n < 0) return "";
  if (n < 60) return Math.round(n) + " 秒";
  if (n < 3600) return Math.round(n / 60) + " 分钟";
  if (n < 86400) return (n / 3600).toFixed(1) + " 小时";
  return (n / 86400).toFixed(1) + " 天";
}

/* 两个 ISO 时间戳之间的秒数。任一头缺了就说不知道，不猜。 */
function spanSec(from, to) {
  if (!from || !to) return null;
  const a = Date.parse(from), b = Date.parse(to);
  if (!isFinite(a) || !isFinite(b)) return null;
  return Math.max(0, Math.round((b - a) / 1000));
}

function setMsg(host, text, kind) {
  host.textContent = text;
  host.className = (host.id === "board-msg" ? "board-msg" : "drawer-msg") + (kind ? " is-" + kind : "");
  host.hidden = false;
}
function hideMsg(host) { host.hidden = true; host.textContent = ""; }

/* 抽屉里的提示条。**存在 state 里**，不直接写 DOM。
 *
 * 为什么：drawDrawer 每次都会把 #drawer-body 整个重建，而它会被
 * SSE 推一下、时间线补读、轮询刷新反复调用。提示直接写 DOM 的话，
 * 下一次重绘就把它冲没了——用户点了「验收通过」，屏幕上一点反应都没有，
 * 看起来就是按钮坏了。
 *
 * 存在 state 里，重绘时再贴回去，提示才活得住。 */
function setDrawerMsg(text, kind) {
  state.drawerMsg = text ? { text: text, kind: kind || "" } : null;
  const host = drawerMsgHost();
  if (!host) return;
  if (state.drawerMsg) {
    host.textContent = state.drawerMsg.text;
    host.className = "drawer-msg" + (state.drawerMsg.kind ? " is-" + state.drawerMsg.kind : "");
    host.hidden = false;
  } else {
    host.textContent = "";
    host.className = "drawer-msg";
    host.hidden = true;
  }
}

// ---------------------------------------------------------------- 流转轨

/* 失败任务的"当前站"是哪一站？主链上 status 已经变成 failed 了，得自己记。
 * 记的来源有三个，优先级从高到低：
 *   1. SSE 推来的 status 事件（准）
 *   2. 轮询途中见过的最后一个主链状态（够用）
 *   3. 兜底：还没开始算过（step 为空）停在第一站，否则停在计算中
 * 猜错的代价只是红点站位略偏，已过的站仍然保留各自颜色。
 * id 一律用 Number：dataset 里读出来的是字符串，拿字符串去查数字键的 Map
 * 会静默 miss，于是每次都掉到兜底分支。 */
function railIndex(id, status, step) {
  const i = STATUS_ORDER.indexOf(status);
  if (i >= 0) return i;
  const seen = state.reached.get(Number(id));
  if (seen != null) return seen;
  return step ? 1 : 0;
}

/* 就地把一条轨重画成 status 的样子。抽出来是为了 slideRail 能"先量后改"：
 * 同一个函数负责建轨和改态，滑动才不会因为走了另一条渲染路径而失效。 */
function paintRail(rail, status, failed, step) {
  const idx = railIndex(rail.dataset.id, status, step != null ? step : rail.dataset.step);
  rail.textContent = "";                     // 先清干净，避免重画时叠出两层格
  rail.dataset.status = failed ? "failed" : status;
  rail.dataset.step = step != null ? (step || "") : (rail.dataset.step || "");
  STATUS_ORDER.forEach((st, i) => {
    // 认不出的状态名（服务端白名单之外的脏值）不会让整条轨炸掉：
    // railIndex 兜底给它一个站位，四站里永远有一个是"当前"，圆点不会消失。
    const cls = i < idx ? "past" : i === idx ? "now" : "future";
    const seg = el("span", "rail-seg " + cls);
    seg.dataset.status = st;
    seg.style.setProperty("--seg", "var(--st-" + st + ")");
    seg.title = STATUS_LABEL[st];
    // **每一站都要有圆点**：地铁线的"站"是实体，缺了站就只剩一根线，
    // 读不出"卡在第几站"。
    const dot = el("span", "rail-dot");
    // 红色也只落在**当前站**身上——整条线涂红就丢掉了"走到哪一步"
    dot.style.setProperty("--dot", failed && i === idx ? "var(--st-failed)" : "var(--st-" + st + ")");
    seg.append(dot);
    rail.append(seg);
  });
  return rail;
}

function buildRail(status, failed, id, step) {
  const rail = el("div", "rail");
  if (id != null) rail.dataset.id = id;
  return paintRail(rail, status, !!failed, step);
}

/* 唯一一处动效：实心点从旧站滑到新站（SPEC 20.8）。
 *
 * 用 FLIP（First-Last-Invert-Play）而不是给 CSS 写死偏移：写死偏移等于把
 * "一格有多宽、圆点落在格子的什么位置"这套布局知识复制到 JS 里，格宽一变就飘。
 * 量旧位置 -> 改态 -> 量新位置 -> 反向位移归零，让 CSS 的 transition 去补间。
 * 遵守 prefers-reduced-motion 靠 CSS（那边把 transition 掐掉，位移仍瞬间归零）。 */
function slideRail(rail, toStatus, failed, step) {
  const same = rail.dataset.status === (failed ? "failed" : toStatus);
  // 必须取**当前站**那个圆点：querySelector(".rail-dot") 会抓到第一站
  // （永远在最左），量出来的位移恒为 0，动画直接失效。
  const dot = rail.querySelector(".rail-seg.now .rail-dot");
  const fromLeft = dot ? dot.getBoundingClientRect().left : 0;
  paintRail(rail, toStatus, failed, step);
  if (same) return;                         // 只是失败态翻转，没有位移可播
  const dot2 = rail.querySelector(".rail-seg.now .rail-dot");
  if (!dot2) return;                        // 目标态认不出来，没有可滑的圆点
  const dx = fromLeft - dot2.getBoundingClientRect().left;
  if (Math.abs(dx) < .5) return;            // 没有实际位移，不播动画
  dot2.style.transition = "none";
  dot2.style.transform = "translateX(" + dx + "px)";
  void dot2.offsetHeight;                   // 强制回流，把反向位移先落定再放行
  dot2.style.transition = "";               // 交回样式表里的 transition
  dot2.style.transform = "";
}

// ---------------------------------------------------------------- 卡片

function makeCard(t) {
  const c = el("article", "card");
  c.tabIndex = 0;
  /* **它是个按钮，就得说它是按钮。** 原来只挂了 click 和 Enter，
   * 缺 role="button"：读屏软件只会念出一段文字，读不出「点它能打开
   * 详情」；键盘用户按 Space 没反应；可访问性树里它干脆不算可点元素
   * （自动化按文本找它也找不到）。 */
  c.setAttribute("role", "button");
  c.dataset.id = t.id;
  c.append(buildRail(t.status, t.status === "failed", t.id, t.step));
  const body = el("div", "card-body");
  const title = el("div", "card-title");
  const meta = el("div", "card-meta");
  meta.append(el("span", "card-id", "#" + t.id));
  meta.append(el("span", "card-time"));
  body.append(title, meta);
  // 当前步骤：running 时显示在轨下方，一行可读，不必点开（SPEC 20.3）。
  // 后端已按 80 码点截断，前端直接显示不重截。
  const step = el("div", "card-step");
  step.append(el("span", "card-step-k", "当前"), el("span", "card-step-t"));
  const fail = el("div", "card-fail");
  fail.append(el("span", "card-fail-k", "失败原因"), el("span", "card-fail-t"));
  // 验收标记：**算完了但还没人认**。不放这里的话，看板上它和「已完成」
  // 长得一模一样——而这两个恰恰是用户最需要区分的状态。
  const review = el("div", "card-review");
  review.append(el("span", "card-review-k"), el("span", "card-review-t"));
  c.append(body, step, fail, review);
  c.addEventListener("click", () => openDrawer(t.id));
  /* Space 也是 button 的标准键——只认 Enter 的话，键盘用户按空格没反应。 */
  c.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDrawer(t.id); }
  });
  return c;
}

function paintCard(c, t) {
  const failed = t.status === "failed";
  const rail = c.querySelector(".rail");
  if (c.dataset.status !== t.status) {
    slideRail(rail, t.status, failed, t.step);   // 状态变了：先量后改，滑过去
  } else {
    paintRail(rail, t.status, failed, t.step);   // 只更新 step 之类的细节
  }
  c.dataset.status = t.status;
  c.classList.toggle("is-failed", failed);
  c.querySelector(".card-title").textContent = t.title || "";
  /* 标签跟着标题走：读屏软件先念这个。 */
  c.setAttribute("aria-label", (t.title || ("任务 " + t.id)) + "，查看详情");
  c.querySelector(".card-time").textContent = fmtTime(t.updated_at || t.created_at);
  const step = c.querySelector(".card-step");
  const st = failed ? "" : (t.step || "");
  step.hidden = !st;                                  // 依赖文件开头那条 [hidden]
  if (st) c.querySelector(".card-step-t").textContent = st;
  const fail = c.querySelector(".card-fail");
  fail.hidden = !(failed && t.error);
  if (!fail.hidden) c.querySelector(".card-fail-t").textContent = t.error;
  /* 验收标记。**只在这两种状态下出现**：待验收、验收没通过。
   * 「已验收」是常态，不挂标签——挂了就变成到处都在喊「已验收」。 */
  const rev = c.querySelector(".card-review");
  const a = (t.acceptance || "none");
  rev.hidden = !(t.status === "done" && (a === "pending" || a === "rejected"));
  if (!rev.hidden) {
    rev.classList.toggle("is-bad", a === "rejected");
    c.querySelector(".card-review-k").textContent =
      a === "rejected" ? "验收没通过" : "待验收";
    /* 被判不通过的不能说「还没人认」——它已经被人（或硬条件）否了。 */
    c.querySelector(".card-review-t").textContent =
      a === "rejected" ? "这一轮没通过验收" : "这一轮的结果还没人认";
  }
}

// ---------------------------------------------------------------- 看板

function buildCols() {
  const board = document.getElementById("board");
  /* 建过就别再建。boot() 会跑两次：页面加载一次，登录成功后再一次——
   * 而 boot() 是先建列、后问 /api/me 的，所以没登录的那一次已经把五列建出来了
   * （signedOut 只清列里的内容，不删列本身）。
   * 第二次建完是**十列**，并且 state.cols 这个 Map 被第二套覆盖，于是第一排五列
   * 再没有任何代码往里写数字，永远停在 0——看着像"没数据"，实际数据都在第二排。
   */
  if (state.cols.size) return;
  COLUMNS.forEach(col => {
    const box = el("section", "col");
    box.style.setProperty("--c", "var(--st-" + col.key + ")");
    const head = el("div", "col-head");
    head.append(el("span", "col-dot"), el("span", "col-count", "0"), el("span", "col-name", col.label));
    const list = el("div", "col-list");
    box.append(head, list);
    board.append(box);
    state.cols.set(col.key, { box, list, count: head.querySelector(".col-count") });
  });
}

function filtered() {
  const q = state.filter.q.toLowerCase();
  return state.items.filter(t => {
    if (state.filter.status === "pending") {
      /* 「待验收」是**另一根轴**（acceptance），不是 status。单独判：
       * 算完了但没人认的结果，在这批跑完之后会攒下一堆，
       * 没有这一档就找不到它们。 */
      if (t.acceptance !== "pending") return false;
    } else if (state.filter.status) {
      const allow = FILTER_TASKS[state.filter.status] || [];
      if (allow.indexOf(t.status) < 0) return false;
    }
    if (state.filter.activeOnly && ACTIVE_TASKS.indexOf(t.status) < 0) return false;
    if (!q) return true;
    return (t.title + " " + (t.brief || "") + " " + (t.step || "")).toLowerCase().indexOf(q) >= 0;
  });
}

/* 列头的数字是**筛选后**的数量：写 12 却只显示 3 张会让用户无法对账。
 * 卡片按 id 复用，只在状态真的变了才重画轨——重建就没有滑动动画了。 */
function render() {
  const items = filtered();
  const seen = new Set();
  COLUMNS.forEach(col => { state.cols.get(col.key).count.textContent = "0"; });
  items.forEach(t => {
    seen.add(t.id);
    let node = state.cards.get(t.id);
    if (!node) { node = makeCard(t); state.cards.set(t.id, node); }
    paintCard(node, t);
    const col = state.cols.get(t.status) || state.cols.get("failed");
    col.count.textContent = String(Number(col.count.textContent) + 1);
    if (node.parentNode !== col.list) col.list.appendChild(node);
  });
  state.cards.forEach((node, id) => {
    if (!seen.has(id)) { node.remove(); state.cards.delete(id); }
  });
}

function renderStatusFilter() {
  const sel = document.getElementById("status-filter");
  sel.textContent = "";
  FILTER_OPTIONS.forEach(o => sel.append(new Option(o.label, o.key)));
  sel.value = state.filter.status;
}

/* ---------------------------------------------------------------- 筛选条
 *
 * 视图范围（全部 / 普通任务 / 工作流）+ 状态 + 关键字。**纯前端筛选**：
 * GET /api/tasks 没有声明 q 参数（服务端只按 owner 过滤），拿关键词去请求
 * 后端只会白跑一趟，而且搜不搜得到全看后端怎么实现。
 */
function applyView() {
  const view = state.filter.view;
  document.getElementById("filters").dataset.view = view;   // 给 CSS 一个钩子
  document.getElementById("wf-zone").hidden = view === "task";
  document.getElementById("board").hidden = view === "workflow";
  document.querySelectorAll("#view-switch .seg-btn").forEach(b =>
    b.classList.toggle("is-on", b.dataset.view === view));
  render();
  renderWorkflows();
  renderFilterNote();
}

function renderFilterNote() {
  const view = state.filter.view;
  const t = filtered().length;
  const w = filteredWorkflows().length;
  const parts = [];
  if (view !== "workflow") parts.push(t + " 个计算任务");
  if (view !== "task") parts.push(w + " 个工作流");
  const note = document.getElementById("filter-note");
  note.textContent = parts.length ? parts.join(" · ") : "";
}

/* ---------------------------------------------------------------- 数据加载 */

let loadSeq = 0;

async function load() {
  const seq = ++loadSeq;
  // 服务端返回的是 {"tasks": [...]}，**不是**裸数组。少拆这一层的话
  // applyItems 拿到的是对象，items.map 直接抛 "items.map is not a function"，
  // 看板上一张卡片都渲染不出来——而页面骨架是静态 HTML，看着只是"空空如也"。
  const got = await api("GET", "/api/tasks");
  if (seq !== loadSeq) return;             // 已经有更新的请求在飞，丢掉这次
  applyItems(Array.isArray(got) ? got : (got.tasks || []));
  hideMsg(document.getElementById("board-msg"));
}

function applyItems(items) {
  state.items = items;
  state.byId = new Map(items.map(t => [t.id, t]));
  // 记住每个任务走到过的最远站序：failed 之后就没 status 可看了，
  // 不记住的话失败卡片上的红点无处安放。
  items.forEach(t => {
    const i = STATUS_ORDER.indexOf(t.status);
    if (i >= 0) {
      const cur = state.reached.get(t.id);
      if (cur == null || i > cur) state.reached.set(t.id, i);
    }
  });
  render();
  syncStreams();
  if (state.openId != null) redrawDrawerIfIdle(state.byId.get(state.openId));
}

function reload() { load().catch(showBoardError); }

function showBoardError(err) {
  if (err && err.status === 401) return signedOut("登录已过期，请重新登录");
  setMsg(document.getElementById("board-msg"), (err && err.message) || "读不到数据", "bad");
}

// ---------------------------------------------------------------- 实时流
//
// 鉴权走请求头，EventSource 发不了 Authorization，所以这里用 fetch +
// ReadableStream 自己读 text/event-stream。断线退避重连；重连带
// Last-Event-ID 游标，服务端从那儿续推，不丢事件。

function frameId(frame) {
  const n = Number(frame.id);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function parseFrame(raw) {
  let id = 0, ev = "", data = "";
  raw.split("\n").forEach(line => {
    if (!line || line[0] === ":") return;               // 注释/心跳
    const c = line.indexOf(":");
    const field = c < 0 ? line : line.slice(0, c);
    const value = c < 0 ? "" : line.slice(c + 1).replace(/^ /, "");
    if (field === "id") id = Number(value) || 0;
    else if (field === "event") ev = value;
    else if (field === "data") data += (data ? "\n" : "") + value;
  });
  if (!data && !ev) return null;
  let payload = null;
  try { payload = data ? JSON.parse(data) : null; } catch (e) { payload = null; }
  return { id, ev, data, payload };
}

function pushEvent(taskId, frame) {
  const p = frame.payload;
  // 帧里带的可能直接是事件行，也可能只是"有更新"的信号。按字段认，认不出
  // 就当信号处理（去拉一次任务详情），别因为形状不同就丢掉整条时间线。
  if (p && typeof p === "object" && (p.kind || p.to_status)) {
    const eid = Number(p.id) || frameId(frame);
    let m = state.events.get(taskId);
    if (!m) { m = new Map(); state.events.set(taskId, m); }
    if (!m.has(eid)) m.set(eid, p);
    if (p.to_status && STATUS_ORDER.indexOf(p.to_status) >= 0) {
      const i = STATUS_ORDER.indexOf(p.to_status);
      const cur = state.reached.get(taskId);
      if (cur == null || i > cur) state.reached.set(taskId, i);
    }
    return true;
  }
  return false;
}

async function readFrames(body, taskId, ctl) {
  const reader = body.getReader();
  const dec = new TextDecoder("utf-8");
  let buf = "";
  let got = false;
  for (;;) {
    const r = await reader.read();
    if (r.done) break;
    buf += dec.decode(r.value, { stream: true }).replace(/\r\n/g, "\n");
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const frame = parseFrame(raw);
      if (!frame) continue;
      if (ctl) ctl.lastId = Math.max(ctl.lastId, frameId(frame));
      pushEvent(taskId, frame);
      got = true;
      scheduleRefresh(taskId);
    }
  }
  return got;
}

async function startStream(id) {
  const ctl = { stop: false, ac: new AbortController(), lastId: 0 };
  state.streams.set(id, ctl);
  while (!ctl.stop) {
    try {
      const r = await fetch(apiBase() + "/api/tasks/" + id + "/stream", {
        headers: authHeaders(ctl.lastId),
        signal: ctl.ac.signal,
      });
      if (r.status === 401) { signedOut("登录已过期，请重新登录"); return; }
      if (!r.ok || !r.body) throw new Error("实时通道不可用");
      await readFrames(r.body, id, ctl);
    } catch (e) {
      if (ctl.stop) return;
    }
    if (ctl.stop) return;
    // 正常断开也要退避：服务端秒断的话，不 sleep 就是一条打死服务器的紧循环
    await sleep(3000);
  }
}

function stopStream(id) {
  const ctl = state.streams.get(id);
  if (!ctl) return;
  ctl.stop = true;
  try { ctl.ac.abort(); } catch (e) { /* 已经断了就算了 */ }
  state.streams.delete(id);
}

/* 终态任务没有实时流可听，但抽屉要时间线：开一次性的补读，从头拿，
 * 读几秒就掐断（服务端也可能一直挂着不发结束帧）。 */
async function catchUpHistory(id) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 5000);
  try {
    const r = await fetch(apiBase() + "/api/tasks/" + id + "/stream", {
      headers: authHeaders(0),
      signal: ac.signal,
    });
    if (r.status === 401) { signedOut("登录已过期，请重新登录"); return; }
    if (!r.ok || !r.body) return;
    await readFrames(r.body, id, null);
  } catch (e) {
    /* 一次性补读：中止或断开都算正常结束 */
  } finally {
    clearTimeout(timer);
    const t = state.byId.get(id);
    if (t && state.openId === id) redrawDrawerIfIdle(t);
  }
}

function syncStreams() {
  const want = new Set(state.items.filter(t => ACTIVE.has(t.status)).map(t => t.id));
  state.streams.forEach((ctl, id) => { if (!want.has(id)) stopStream(id); });
  want.forEach(id => { if (!state.streams.has(id)) startStream(id); });
}

const refreshTimers = new Map();
function scheduleRefresh(id) {
  if (refreshTimers.has(id)) return;       // 事件成串来，合并成一次拉取
  refreshTimers.set(id, setTimeout(async () => {
    refreshTimers.delete(id);
    try {
      const t = detailTask(await api("GET", "/api/tasks/" + id), "实时刷新");
      const i = state.items.findIndex(x => x.id === id);
      if (i >= 0) state.items[i] = t; else state.items.push(t);
      state.byId.set(id, t);
      const k = STATUS_ORDER.indexOf(t.status);
      if (k >= 0) {
        const cur = state.reached.get(id);
        if (cur == null || k > cur) state.reached.set(id, k);
      }
      render();
      if (state.openId === id) redrawDrawerIfIdle(t);
      syncStreams();
    } catch (e) {
      if (e && e.status === 401) signedOut("登录已过期，请重新登录");
    }
  }, 400));
}

/* ---------------------------------------------------------------- 验收 / 续跑
 *
 * 「算完」与「交付完成」是两根轴。这三个动作是人在第二根轴上说话：
 *
 *   验收通过        这一轮的结果算交付完成
 *   打回并补充要求   这一轮不认，带着「哪里不行」再跑一遍
 *   续跑并补充要求   接着上一轮再跑（产物留着，模型接着改）
 *
 * 续跑是**同一个任务接着干**，不是重开一个：工作流是「一行一个任务」，
 * 另起一轮会让那一行分裂成两个，归档进结果表时该写谁的结果就说不清了。 */

/* --- 验收方式 -------------------------------------------------------------
 *
 * 三级：任务 > 工作流 > 全局默认，空串 = 继承。
 *
 * **来源必须一起显示**：只给一个「现在生效：机器审核」而不说是跟着谁来的，
 * 用户就改不动一个看不见来路的设置——而改法恰恰是「我要自己设一个」。 */

/* 下拉控件。三个选项：继承上层 / 人工 + 条件判定 / 机器审核。 */
function reviewModeSelect(current, onPick) {
  const sel = el("select", "review-mode-select");
  [["", "继承上层"], ["human", "人工 + 条件判定"], ["llm", "机器审核"]]
    .forEach(([v, label]) => {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = label;
      if ((current || "") === v) o.selected = true;
      sel.append(o);
    });
  sel.onchange = () => onPick(sel.value);
  return sel;
}

/* 改一个任务的验收方式。改完要重画抽屉：下拉框自己不会变回去。 */
async function setTaskReviewMode(t, mode) {
  try {
    const got = await api("PATCH", "/api/tasks/" + t.id, { review_mode: mode });
    state.byId.set(t.id, detailTask(got, "改验收方式"));
    rememberReview(t.id, got);
    drawDrawer(t.id, state.byId.get(t.id));
    setDrawerMsg("验收方式已改", "ok");
  } catch (e) {
    setDrawerMsg(e.message || "改不动验收方式", "bad");
    drawDrawer(t.id, state.byId.get(t.id));
  }
}

/* 改一批的验收方式。**不弹对话框**：它是一个明确的设置控件，
 * 点一下就生效，跟改筛选条件是同一类动作。 */
async function setWorkflowReviewMode(w, mode) {
  try {
    const got = await api("PATCH", "/api/workflows/" + w.id, { review_mode: mode });
    const wf = got && got.workflow;
    if (wf && wf.id) {
      const i = state.workflows.findIndex(x => Number(x.id) === Number(wf.id));
      if (i >= 0) state.workflows[i] = wf;
    }
    await load();
  } catch (e) {
    setDrawerMsg(e.message || "改不动这一批的验收方式", "bad");
  }
}

/* 让机器审这一轮。**异步**：审核要另起一个独立进程跑几分钟，
 * 所以请求立刻就回，后台审完再回来刷新。
 *
 * **已经定了结论的要先说清影响面**：重新打开 = 回到「待验收」，
 * 机器判完直接改结论——**人打回过的意见也会被机器的结论盖掉**。
 * 这一步不是不可撤销（人还能再推翻），但它会让人之前点过的「打回」白点，
 * 所以要确认一次。 */
async function requestLlmReview(t) {
  const a = t.acceptance || "none";
  if (a === "accepted" || a === "rejected") {
    const ok = await confirmAction({
      title: "让机器重新审这一轮？",
      lead: "这一轮已经有结论了。重新审会把它放回「待验收」，机器判完直接改结论。",
      items: [
        a === "accepted" ? "当前的「已验收」会被撤掉" : "当前的「验收没通过」会被撤掉",
        "机器判完直接改成通过或不通过",
        "你之前打回时写的意见不会再影响这一轮的结论",
      ],
      okLabel: "确认重新审",
    });
    if (!ok) return;
  }
  try {
    const got = await api("POST", "/api/tasks/" + t.id + "/llm-review", {});
    state.byId.set(t.id, detailTask(got, "让机器审一下"));
    rememberReview(t.id, got);
    setDrawerMsg("已交给审核进程，过一会儿刷新看结论", "ok");
    await load();
    const nt = state.byId.get(t.id);
    if (nt && state.openId === t.id) drawDrawer(t.id, nt);
  } catch (e) {
    setDrawerMsg(e.message || "机器审核没排上", "bad");
  }
}

/* 验收通过。**不用两步确认**：它是把「已经算好的东西」标成「已交付」，
 * 没有不可撤销的后果——万一点错了，再打回一次就行。 */
async function doAccept(t) {
  try {
    const got = await api("POST", "/api/tasks/" + t.id + "/accept", {});
    state.byId.set(t.id, detailTask(got, "验收通过"));
    rememberReview(t.id, got);
    /* **先重画再写提示**：drawDrawer 会把抽屉内容整个重建，
     * 提示写在前面的话会被这次重建冲掉——用户点了「验收通过」，
     * 屏幕上什么反应都没有。 */
    drawDrawer(t.id, state.byId.get(t.id));
    setDrawerMsg( "已验收：这一轮的结果算交付完成", "ok");
    await load();
  } catch (e) {
    setDrawerMsg( e.message || "验收失败", "bad");
  }
}

/* 打回 / 续跑：要一句话，所以先开那张有输入框的表。 */
function doReview(t, mode) {
  const dlg = document.getElementById("review");
  document.getElementById("rv-title").textContent =
    mode === "reject" ? "打回并补充要求" : "续跑并补充要求";
  /* **说清楚结果表那一格会被覆盖**。这是平台的既有语义（最新一轮的结果
   * 就是表里那一格；失败轮次就写「失败」），跟「重跑」一样——但用户
   * 不知道这一条，事后看到好结果变成「失败」会以为出了故障。 */
  document.getElementById("rv-lead").textContent = mode === "reject"
    ? "这一轮的结果不认。写清楚哪里不行，它会带着这段话再跑一遍；"
      + "上一轮的产物留在工作目录里，模型可以接着改。"
      + "（结果表里那一格会被这一轮的结果覆盖。）"
    : "同一个任务接着干。上一轮的产物留在工作目录里，"
      + "写清楚还要什么，它会在那基础上继续。"
      + "（结果表里那一格会被这一轮的结果覆盖。）";
  document.getElementById("rv-note-k").textContent = "补充要求";
  document.getElementById("rv-ok").textContent =
    mode === "reject" ? "打回并续跑" : "续跑";
  const note = document.getElementById("rv-note");
  note.value = "";
  if (!dlg.open) dlg.showModal();
  note.focus();
  dlg.dataset.mode = mode;
  dlg.dataset.task = String(t.id);
}

/* 对话框里那个按钮点了之后做的事。**打回与续跑是两个端点**：
 * 打回=不认这一版（顺手续跑），续跑=只是接着干。 */
async function doReviewSubmit() {
  const dlg = document.getElementById("review");
  const mode = dlg.dataset.mode || "continue";
  const id = Number(dlg.dataset.task);
  const note = document.getElementById("rv-note").value.trim();
  if (!note) {
    setDrawerMsg( "要写一句补充要求——不然模型不知道要改什么", "bad");
    return;
  }
  const btn = document.getElementById("rv-ok");
  btn.disabled = true;
  try {
    /* **两个分支都写完整路径，不用变量拼。**
     *
     * 围栏（tests/test_frontend_response_shape.py）靠扫源码里的
     * `api("POST", "/api/tasks/...")` 字面量来确认「凡是拿详情信封的地方
     * 都经 detailTask() 拆过」。路径写成变量的话，扫描器**扫不到这一处**，
     * 而它恰恰是漏拆就静默做错事的那种地方——围栏形同虚设。
     *
     * 每个分支各自紧挨着 detailTask()：拆包与取数挨在一起，
     * 中间隔着一大段的话，扫描器那 200 字符的窗口就套不住了。
     */
    if (mode === "reject") {
      const got = await api("POST", "/api/tasks/" + id + "/reject",
                            { note: note, rerun: true });
      state.byId.set(id, detailTask(got, "打回"));
      rememberReview(id, got);
    } else {
      const got = await api("POST", "/api/tasks/" + id + "/continue",
                            { note: note, rerun: true });
      state.byId.set(id, detailTask(got, "续跑"));
      rememberReview(id, got);
    }
    dlg.close();
    /* **提示要写在用户看得到的地方，而且要写在 load() 之后**。
     *
     * ① 这个动作可能来自抽屉，也可能来自工作流的行列表——行列表那条路
     * 抽屉是关着的，提示写进抽屉里等于什么都没发生（用户看着像按钮坏了）。
     * ② load() 结尾会把看板提示条 **hideMsg 掉**（它是「拉数时的临时提示」）。
     *    提示写在 load() 前面会被这一句擦掉——顺序反了就是「点了没反应」。 */
    await load();
    /* 行列表那条路还要把工作流重画一遍：那一行的状态变了。 */
    state.wfOpen.forEach(wfId => refreshOpenWorkflow(wfId));
    const drawer = document.getElementById("drawer");
    const drawerOpen = !drawer.hidden && Number(state.openId) === id;
    const text = mode === "reject"
      ? "已打回，它会带着这段要求再跑一遍"
      : "已续跑，它会带着这段要求接着干";
    if (drawerOpen) {
      drawDrawer(id, state.byId.get(id));
      setDrawerMsg(text, "ok");          // 抽屉那条：存 state，重绘也活得住
    } else {
      setMsg(document.getElementById("board-msg"), text, "ok");
    }
  } catch (e) {
    setDrawerMsg( e.message || "操作失败", "bad");
  } finally {
    btn.disabled = false;
  }
}

document.getElementById("rv-ok").onclick = doReviewSubmit;
document.getElementById("rv-cancel").onclick =
  () => document.getElementById("review").close();

// ---------------------------------------------------------------- 抽屉

/* 记下详情信封里的另外两样东西。
 *
 * **任务本体不进这个函数**——它必须由调用点自己 `detailTask()` 拆。
 * 把拆包挪进辅助函数看着整齐，但围栏
 * （tests/test_frontend_response_shape.py）靠扫源码里的 `detailTask(` 字面量
 * 确认「凡是拿详情信封的地方都拆过」——挪进函数里，扫描器就**扫不到那一处**，
 * 而那恰恰是漏拆就静默做错事的地方。围栏形同虚设。
 */
function rememberReview(id, got) {
  state.taskArts.set(Number(id), got.artifacts || []);
  state.taskReview.set(Number(id), got.review || null);
}

/* 状态芯片：任务与工作流共用一套语汇，颜色只借那五个状态色。 */
function statusChip(key) {
  if (!key) return el("span", "chip chip-none", "—");
  return el("span", "chip chip-" + key, STATUS_LABEL[key] || key);
}

function wfStatusChip(key) {
  const k = key || "draft";
  return el("span", "chip " + (WF_CHIP[k] || "chip-none"), WF_STATUS_LABEL[k] || k);
}

function prettyEdited(msg) {
  const m = /^修改了：(.*)$/.exec(msg || "");
  if (!m) return "改了内容";
  const names = m[1].split("、").map(x => FIELD_CN[x] || x);
  return "改了" + names.join("、");
}

/* 区块 = 小标题 + 右侧小字「编辑」。内容默认只读，点「编辑」才进编辑态，
 * 存完回只读——详情是给人读的，不是给人填的。 */
function section(parent, title, action) {
  const sec = el("section", "sec");
  const head = el("div", "sec-head");
  head.append(el("h3", "sec-h", title));
  if (action) head.append(action);
  const slot = el("div", "sec-slot");
  sec.append(head, slot);
  parent.append(sec);
  return slot;
}

function miniBtn(text, title) {
  const b = el("button", "mini", text);
  b.type = "button";
  if (title) b.title = title;
  return b;
}

/* 只读 <-> 编辑 的字段块。
 *
 * 保存回调拿到的是**服务端回填后的值**再切回只读：服务端会规范化（去首尾空白、
 * 截断 step），不回填就会出现「存了但看着没变」。 */
function editable(host, value, renderView, makeEditor) {
  const view = el("div", "eb-view");
  const edit = el("div", "eb-edit");
  edit.hidden = true;                        // 开局就是只读
  host.append(view, edit);
  renderView(view, value);
  function show() { edit.hidden = true; view.hidden = false; state.editing = false; }
  return {
    show,
    view,
    toEdit() {
      state.editing = true;
      edit.textContent = "";
      makeEditor(edit, value, v => {
        value = v;
        // 必须先清空再渲染：只读态的 renderView 一律是 append（它只被调用一次
        // 时没问题），存完再调一次就会把旧内容留在上面——于是同一个 view 里
        // 叠了两三段 .body-text，页面上看着像存了两次。
        view.textContent = "";
        renderView(view, v);
        show();
      });
      view.hidden = true;
      edit.hidden = false;
    },
  };
}

/* 两个保存按钮：主次分明。「取消」等价于保存原值——它只是切回只读，
 * 不发请求，所以不会有"取消了一半写进账本"这种事。 */
function editActions(saveFn, cancelFn) {
  const bar = el("div", "eb-actions");
  const save = el("button", "primary", "保存");
  save.type = "button";
  save.onclick = saveFn;
  const cancel = el("button", "secondary", "取消");
  cancel.type = "button";
  cancel.onclick = cancelFn;
  bar.append(save, cancel);
  return bar;
}

/* PATCH 若服务端没开放（404/405），就把"编辑"整体关掉，退回纯只读。
 * 与其让用户点一次编辑再撞一个看不懂的报错，不如第一次撞上就固定成只读。 */
function patchFailed(e, id) {
  if (e && (e.status === 404 || e.status === 405)) {
    state.canEdit = false;
    const t = state.byId.get(id);
    // 先重绘成只读，再放提示：drawDrawer 会把 #drawer-body 整个换掉，
    // 顺序反了提示条连同旧节点一起被扔掉，用户什么都看不到。
    if (t) drawDrawer(id, t);
    setDrawerMsg( "服务端未开放编辑，以下内容改为只读", "warn");
    return true;
  }
  return false;
}

function drawerMsgHost() { return document.getElementById("drawer-msg"); }

async function openDrawer(id) {
  state.openId = id;
  state.editing = false;
  /* 换任务就把上一条提示清掉——提示是「对刚才那个任务说的」，
   * 留着会让人以为新任务也出事了。 */
  state.drawerMsg = null;
  const d = document.getElementById("drawer");
  d.hidden = false;
  const body = document.getElementById("drawer-body");
  body.textContent = "";
  let t = state.byId.get(id);
  try {
    const got = await api("GET", "/api/tasks/" + id);
    t = detailTask(got, "打开详情");
    state.byId.set(id, t);
    rememberReview(id, got);
  } catch (e) {
    if (e && e.status === 401) { signedOut("登录已过期，请重新登录"); return; }
    body.append(el("p", "drawer-error", "打不开：#" + id + " " + (e.message || "")));
    return;
  }
  if (state.openId !== id) return;           // 期间用户已经点了别的卡
  drawDrawer(id, t);
  // 抽屉开着就补一份时间线：在跑的任务本来就有流，终态的做一次性补读
  if (!state.streams.has(id)) catchUpHistory(id);
}

/* 抽屉内容有实时重绘，但**用户正在编辑时不许重绘**——否则一次 SSE 推送就会把
 * 正在敲的 textarea 从 DOM 里换掉，光标回到开头。 */
function redrawDrawerIfIdle(t) {
  if (state.editing || !t || state.openId !== t.id) return;
  drawDrawer(t.id, t);
}

function drawDrawer(id, t) {
  const body = document.getElementById("drawer-body");
  body.textContent = "";
  body.append(el("h2", "drawer-title", t.title || ""));
  const sub = el("div", "drawer-sub");
  sub.append(el("span", "drawer-id", "#" + t.id));
  sub.append(statusChip(t.status));
  sub.append(el("span", "drawer-time", "提交于 " + fmtTime(t.created_at)));
  body.append(sub);

  const msg = el("div", "drawer-msg");
  msg.id = "drawer-msg";
  msg.hidden = true;
  /* **重绘时把提示贴回去**（见 setDrawerMsg 的注释）：提示存在 state 里，
   * 这里只是把它重新显示出来——不贴回去的话，一次 SSE 推送就能把
   * 「已验收」四个字冲没。 */
  if (state.drawerMsg) {
    msg.textContent = state.drawerMsg.text;
    msg.className = "drawer-msg" + (state.drawerMsg.kind ? " is-" + state.drawerMsg.kind : "");
    msg.hidden = false;
  }
  body.append(msg);

  // 动作常驻可见：中止与下载是高频动作，不该藏在区块里等用户翻到
  const acts = el("div", "action-row");
  if (ACTIVE.has(t.status)) acts.append(abortBtn(t));
  if (t.package_path || t.status === "done") acts.append(downloadBtn(t));
  if (!ACTIVE.has(t.status) && !t.package_path) {
    acts.append(el("span", "action-note",
      t.status === "failed" ? "任务已中止，没有产物包。" : "产物还在生成，生成完就能下载。"));
  }
  body.append(acts);

  const rail = buildRail(t.status, t.status === "failed", id, t.step);
  rail.classList.add("drawer-rail");
  body.append(rail);

  /* --- 需求说明 ---------------------------------------------------------- */
  const briefBtn = state.canEdit ? miniBtn("编辑", "改需求说明") : null;
  const brief = editable(section(body, "需求说明", briefBtn), t.brief || "",
    (view, v) => {
      if (v) view.append(el("div", "body-text", v));
      else view.append(el("p", "field-empty", "没写需求说明。"));
    },
    (host, value, done) => {
      const ta = el("textarea", "drawer-body-edit");
      ta.rows = 3;
      ta.value = value;
      ta.placeholder = "要算什么、输入是什么、想要什么产物";
      // 自动增高：先归 auto 才量得到真实内容高，否则只会越加越长
      const grow = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; };
      ta.addEventListener("input", grow);
      grow();
      const bar = editActions(async () => {
        const s = bar.querySelector(".primary");
        s.disabled = true;
        try {
          const saved = detailTask(
            await api("PATCH", "/api/tasks/" + id, { brief: ta.value }), "保存需求说明");
          state.byId.set(id, saved);
          setDrawerMsg( "需求说明已保存", "ok");
          done(saved.brief == null ? "" : saved.brief);
        } catch (e) {
          s.disabled = false;
          if (!patchFailed(e, id)) setDrawerMsg( e.message, "bad");
        }
      }, () => done(value));
      host.append(ta, bar);
      ta.focus();
    });
  if (briefBtn) briefBtn.onclick = () => brief.toEdit();

  /* --- 基本信息 ---------------------------------------------------------- */
  const titleBtn = state.canEdit ? miniBtn("编辑", "改标题") : null;
  const kv = editable(section(body, "基本信息", titleBtn), t.title || "",
    (view, v) => {
      const box = el("div", "kv");
      box.append(el("div", "kv-value", v || "（无标题）"));
      if (t.done_at) {
        const r = el("div", "kv-row");
        r.append(el("div", "kv-label", "结束于"), el("div", "kv-value is-mono", fmtTime(t.done_at)));
        box.append(r);
      }
      view.append(box);
    },
    (host, value, done) => {
      const input = el("input", "eb-input");
      input.value = value;
      input.maxLength = 200;
      const bar = editActions(async () => {
        const s = bar.querySelector(".primary");
        s.disabled = true;
        try {
          const saved = detailTask(
            await api("PATCH", "/api/tasks/" + id, { title: input.value }), "保存标题");
          state.byId.set(id, saved);
          setDrawerMsg( "标题已保存", "ok");
          done(saved.title == null ? "" : saved.title);
        } catch (e) {
          s.disabled = false;
          if (!patchFailed(e, id)) setDrawerMsg( e.message, "bad");
        }
      }, () => done(value));
      host.append(input, bar);
      input.focus();
    });
  if (titleBtn) titleBtn.onclick = () => kv.toEdit();

  /* --- 当前步骤 / 结果 --------------------------------------------------- */
  const stepSlot = section(body, "当前步骤");
  if (t.step) stepSlot.append(el("div", "body-text", t.step));
  else stepSlot.append(el("p", "field-empty",
    ACTIVE.has(t.status) ? "还没上报步骤。" : "任务不在计算中，没有当前步骤。"));

  /* --- 上一轮 -------------------------------------------------------------
   * **续跑时上一轮的结论被人清空了**（清空是为了让新一轮的摘要占位，
   * 否则界面显示的还是上一轮那句话，会被当成这一轮的结果）。但那份上下文
   * 只喂给了模型、没给人看——「上一轮算出了什么、这次要改什么」恰恰是
   * 人自己想确认的东西。
   * followup 字段里就是那段（组装给模型的那一份），原样显示。 */
  if ((t.followup || "").trim()) {
    const prevSlot = section(body, "上一轮的要求与上下文");
    prevSlot.append(el("pre", "prev-followup", t.followup));
  }

  const resSlot = section(body, "结果");
  if (t.error) {
    resSlot.append(el("div", "kv-value is-bad", t.error));
  } else if (t.summary) {
    resSlot.append(el("div", "body-text", t.summary));
  } else {
    resSlot.append(el("p", "field-empty",
      t.status === "done" ? "完成判定通过，没有额外的文字说明。" : "还没有结果。"));
  }

  /* --- 验收 ---------------------------------------------------------------
   * **算完不等于交付完成。** 这一段是用户对「这一轮的结果到底行不行」
   * 说话的地方：通过了才算交付，没通过可以打回并补要求。
   *
   * 只在算完（done）之后出现——还在跑的时候没有东西可验收。 */
  if (t.status === "done") {
    const slot = section(body, "验收");
    const a = t.acceptance || "none";
    const head = el("div", "review-head");
    if (a === "pending") {
      head.append(el("span", "chip chip-review", "待验收"));
      head.append(el("span", "review-note",
        "这一轮算完了，但结果还没人认。看完产物和结论再决定认不认。"));
    } else if (a === "rejected") {
      head.append(el("span", "chip chip-failed", "验收没通过"));
      head.append(el("span", "review-note",
        "这一轮的结果没通过验收。你可以补要求让它接着改。"));
    } else if (a === "accepted") {
      head.append(el("span", "chip chip-done", "已验收"));
      head.append(el("span", "review-note", "这一轮的结果算交付完成。"));
    } else {
      head.append(el("span", "review-note", "这一轮算完了。"));
    }
    slot.append(head);

    /* **这一轮用哪种方式验收**，以及**它跟着谁来的**。
     * 放在验收区最上面：方式决定了「谁来判」，是做出决定之前的前提。 */
    const rev0 = state.taskReview.get(Number(id));
    const modeRow = el("div", "review-mode-row");
    modeRow.append(el("span", "review-mode-k", "验收方式"));
    modeRow.append(reviewModeSelect(t.review_mode || "", (v) => {
      setTaskReviewMode(t, v);
    }));
    const eff = (rev0 && rev0.mode) || "human";
    const src = (rev0 && rev0.mode_source) || "global";
    modeRow.append(el("span", "review-mode-note",
      "现在生效：" + (REVIEW_MODE_LABEL[eff] || eff)
      + "（" + (REVIEW_MODE_SOURCE[src] || src) + "）"));
    slot.append(modeRow);

    /* **没过的原因要摆在验收区里**，不能只躺在时间线最下面：用户正要
     * 在这一屏做决定（认不认、续不续跑），却要翻到最下面才知道自己
     * 为什么被拒。 */
    const rev = rev0;
    const fails = (rev && rev.failures) || [];
    if (fails.length) {
      const why = el("ul", "review-fails");
      fails.forEach(f => why.append(el("li", null, f)));
      slot.append(why);
    }

    /* 这一轮交了什么：产物清单。**没有产物清单就没法验收**——
     * 只看一句「算完了」是验不了收的。 */
    const arts = state.taskArts.get(Number(id)) || [];
    if (arts.length) {
      const list = el("ul", "review-arts");
      arts.slice(0, 12).forEach(a2 => {
        list.append(el("li", null,
          a2.name + "（" + fmtSize(a2.size) + "）"));
      });
      if (arts.length > 12) {
        list.append(el("li", "review-more", "……另外还有 " + (arts.length - 12) + " 项"));
      }
      slot.append(list);
    } else {
      slot.append(el("p", "field-empty", "这一轮没有登记任何产物。"));
    }

    /* **机器审核的结论**：机器说了什么，与「人认没认」是**两件事**。
     * 结论单独列出来，是因为它带着依据（理由），而依据是复核时唯一
     * 用得上的东西。 */
    const llm = rev && rev.llm;
    if (llm) {
      const box = el("div", "review-llm");
      const tag = llm.verdict === "pass" ? "机器判定通过"
        : (llm.verdict === "fail" ? "机器判定没通过" : "机器审核没跑成");
      box.append(el("span", "review-llm-k", tag));
      if (llm.reason) box.append(el("span", "review-llm-t", llm.reason));
      box.append(el("span", "review-llm-time", fmtTime(llm.created_at)));
      slot.append(box);
    }

    const acts2 = el("div", "action-row");
    if (a !== "accepted") {
      const ok = el("button", "btn btn-primary", "验收通过");
      ok.type = "button";
      ok.onclick = () => doAccept(t);
      acts2.append(ok);
      const bad = el("button", "btn btn-warn", "打回并补充要求");
      bad.type = "button";
      bad.onclick = () => doReview(t, "reject");
      acts2.append(bad);
    }
    /* 「让机器审一下」：把这一轮交给审核进程重判一遍。
     * 机器判完直接改验收状态，**人可以再推翻**——上面那两颗按钮还在。 */
    const llmBtn = el("button", "btn", llm ? "让机器重审一次" : "让机器审一下");
    llmBtn.type = "button";
    llmBtn.onclick = () => requestLlmReview(t);
    acts2.append(llmBtn);
    const cont = el("button", "btn", "续跑并补充要求");
    cont.type = "button";
    cont.onclick = () => doReview(t, "continue");
    acts2.append(cont);
    slot.append(acts2);
  }

  /* --- 时间线 ------------------------------------------------------------ */
  const tlSlot = section(body, "时间线");
  drawTimeline(tlSlot, id);

  /* --- 危险区 ------------------------------------------------------------ */
  if (state.canDelete) {
    const zone = el("div", "danger-zone");
    const btn = el("button", "danger-link", "删除这个计算任务");
    btn.type = "button";
    btn.onclick = () => deleteTask(t);
    zone.append(btn);
    body.append(zone);
  }
}

// ---------------------------------------------------------- 时间线视图
//
// 时间线是**活动流**，不是日志表：一条竖轴 + 一个节点，主文本在上、
// 时间与操作人在下。三件事是硬要求：
//
//   1. 关键事件突出（状态变更、报错、收口、打包起止），过程输出降级并收起来；
//   2. 能按类型过滤（全部 / 关键事件 / 只看报错 / 只看状态变化）；
//   3. **不裸露字段名**——服务端 message 是中文契约，裸字段名的翻译放显示层。

function taskEvents(id) {
  const m = state.events.get(id);
  const list = m ? [...m.values()].filter(e => e.kind !== "step") : [];
  return list.sort((a, b) => (a.id || 0) - (b.id || 0) || (a.created_at || "").localeCompare(b.created_at || ""));
}

function timelineKeeps(e) {
  switch (state.timelineFilter) {
    case "key": return KEY_KINDS.has(e.kind);
    case "error": return e.kind === "error";
    case "status": return e.kind === "status";
    default: return true;
  }
}

/* 一条事件的主文本。**不出现字段名**：状态跃迁渲染成状态芯片，
 * 「修改了：body」翻成「改了需求说明」，其余按 KIND_LABEL 拼。 */
function eventHead(e) {
  const head = el("div", "tl-head");
  if (e.kind === "status") {
    head.append(statusChip(e.from_status), el("span", "tl-arrow", "→"), statusChip(e.to_status));
    if (e.message) head.append(el("span", "tl-what", e.message));
  } else if (e.kind === "created") {
    head.append(el("span", "tl-what", "提交了这个计算任务"));
  } else if (e.kind === "edited") {
    head.append(el("span", "tl-what", prettyEdited(e.message)));
  } else if (e.kind === "error") {
    head.append(el("span", "tl-what", e.message || "出错了"));
  } else {
    head.append(el("span", "tl-what",
      (KIND_LABEL[e.kind] || e.kind) + (e.message ? "：" + e.message : "")));
  }
  return head;
}

function eventNode(e) {
  const li = el("li", "tl-node tl-" + e.kind + (KEY_KINDS.has(e.kind) ? " is-key" : " is-soft"));
  li.append(el("span", "tl-point"));
  const card = el("div", "tl-card");
  card.append(eventHead(e));
  card.append(el("div", "tl-meta",
    fmtTime(e.created_at) + " · " + (ACTOR_LABEL[e.actor] || e.actor)));
  li.append(card);
  return li;
}

function drawTimeline(host, id) {
  const evs = taskEvents(id).filter(timelineKeeps);
  if (!evs.length) {
    host.append(el("p", "tl-empty",
      "这一页只显示打开之后收到的进展。要完整历史请在任务运行时打开。"));
    return;
  }
  /* 过滤器：放在区块头上，点一下就换一批事件。 */
  const bar = el("div", "tl-bar");
  TIMELINE_FILTERS.forEach(f => {
    const b = el("button", "tl-chip", f.label);
    b.type = "button";
    b.dataset.tlfilter = f.key;
    if (state.timelineFilter === f.key) b.classList.add("is-on");
    b.onclick = () => {
      state.timelineFilter = f.key;
      host.textContent = "";
      drawTimeline(host, id);
    };
    bar.append(b);
  });
  host.append(bar);

  const key = evs.filter(e => KEY_KINDS.has(e.kind));
  const soft = evs.filter(e => !KEY_KINDS.has(e.kind));

  const track = el("ol", "tl-track");
  key.forEach(e => track.append(eventNode(e)));
  host.append(track);

  if (soft.length) {
    /* 过程输出收起来。**收起的理由写在这里**：这类事件一次计算能有几百条，
     * 摊开就把真正要读的那几条埋掉了。 */
    const det = el("details", "tl-fold");
    const sum = el("summary", null, "过程输出（" + soft.length + " 条）");
    det.append(sum);
    const inner = el("ol", "tl-track is-soft");
    soft.forEach(e => inner.append(eventNode(e)));
    det.append(inner);
    det.open = state.timelineOpen;
    det.addEventListener("toggle", () => { state.timelineOpen = det.open; });
    host.append(det);
  }
}

/* ---------------------------------------------------------------- 危险动作 */

function abortBtn(t) {
  const b = el("button", "btn btn-warn", "中止任务");
  b.type = "button";
  b.onclick = async () => {
    const ok = await confirmAction({
      title: "中止「" + (t.title || "") + "」？",
      lead: "中止后这个计算任务不会再往下走，已产生的中间文件仍留在工作目录里。",
      items: ["算到一半的技能链会被终止", "中止之后不能续跑，只能重新提交一次"],
      okLabel: "确认中止",
    });
    if (!ok) return;
    b.disabled = true;
    try {
      state.byId.set(t.id, detailTask(
        await api("POST", "/api/tasks/" + t.id + "/abort", {}), "中止任务"));
      setDrawerMsg( "已请求中止", "warn");
      stopStream(t.id);
      await load().catch(() => {});
      const nt = state.byId.get(t.id);
      if (nt && state.openId === t.id) drawDrawer(t.id, nt);
    } catch (e) {
      b.disabled = false;
      setDrawerMsg( e.message, "bad");
    }
  };
  return b;
}

/* 下载走 fetch 而不是 <a href>：token 在 Authorization 头里，
 * <a> 带不了头，一 href 就变成未登录的 401 页面被存成 zip。
 * 名字优先取服务端给的 Content-Disposition。 */
function filenameFrom(r, id) {
  const cd = r.headers.get("Content-Disposition") || "";
  let m = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  if (m) { try { return decodeURIComponent(m[1]); } catch (e) { /* 编码坏了就用兜底 */ } }
  m = /filename="?([^";]+)"?/i.exec(cd);
  return m ? m[1] : "task-" + id + ".zip";
}

function downloadBtn(t) {
  const b = el("button", "btn btn-primary", "下载产物包");
  b.type = "button";
  b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await fetch(apiBase() + "/api/tasks/" + t.id + "/package", { headers: authHeaders() });
      if (!r.ok) throw await errFrom(r);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = el("a");
      a.href = url;
      a.download = filenameFrom(r, t.id);
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 20000);
      setDrawerMsg( "已开始下载 " + a.download, "ok");
    } catch (e) {
      setDrawerMsg( e.message, "bad");
    } finally {
      b.disabled = false;
    }
  };
  return b;
}

async function deleteTask(t) {
  const evN = taskEvents(t.id).length;
  const ok = await confirmAction({
    title: "删除「" + (t.title || "") + "」？",
    lead: "删掉之后找不回来。会一起消失的有：",
    items: [
      "这个计算任务的标题与需求说明",
      evN ? evN + " 条时间线记录" : "（这条还没有时间线记录）",
      "工作目录里的中间文件不会被删，只是平台不再记录它",
    ],
    okLabel: "确认删除",
  });
  if (!ok) return;
  try {
    await api("DELETE", "/api/tasks/" + t.id);
    closeDrawer();
    setMsg(document.getElementById("board-msg"), "已删除「" + (t.title || "") + "」", "ok");
    await load();
  } catch (e) {
    if (e && (e.status === 404 || e.status === 405)) {
      state.canDelete = false;
      setMsg(document.getElementById("board-msg"), "服务端未开放删除", "warn");
      const nt = state.byId.get(t.id);
      if (nt && state.openId === t.id) drawDrawer(t.id, nt);
    } else if (e.status === 401) {
      signedOut("登录已过期，请重新登录");
    } else {
      setDrawerMsg( e.message, "bad");
    }
  }
}

/* 两步确认。中止与删除这类不可撤销的动作都走这张表。
 *
 * <dialog> 的 close() **不清事件监听**，而 #confirm 是复用节点：第一次确认留下的
 * 监听会让第二次确认被第一次的闭包直接 resolve 掉——那就不是"取消"而是中止错任务。
 * 所以收尾统一走 finish()，且**幂等**（answered 守卫）+ 解绑监听。
 * 监听 close 而不是 cancel：Esc、点「算了」、点确认都走 close，
 * 于是"没点确认就关掉了"一律判成取消，不会出现 Promise 悬着、按钮卡 disabled。 */
function confirmAction(spec) {
  const dlg = document.getElementById("confirm");
  document.getElementById("confirm-title").textContent = spec.title;
  document.getElementById("confirm-lead").textContent = spec.lead || "";
  const ok = document.getElementById("confirm-ok");
  ok.textContent = spec.okLabel || "确认";
  const list = document.getElementById("confirm-list");
  list.textContent = "";
  (spec.items || []).forEach(t => list.append(el("li", null, t)));

  return new Promise(resolve => {
    const no = document.getElementById("confirm-cancel");
    let answered = false;
    function finish(v) {
      if (answered) return;
      answered = true;
      ok.removeEventListener("click", onOk);
      no.removeEventListener("click", onNo);
      dlg.removeEventListener("close", onClose);
      if (dlg.open) dlg.close();
      resolve(v);
    }
    function onOk() { finish(true); }
    function onNo() { finish(false); }
    function onClose() { finish(false); }
    ok.addEventListener("click", onOk);
    no.addEventListener("click", onNo);
    dlg.addEventListener("close", onClose);
    dlg.showModal();
  });
}

function closeDrawer() {
  document.getElementById("drawer").hidden = true;
  state.openId = null;
  state.editing = false;
  setDrawerFull(false);         // 关掉之前把全屏态复位，否则下次打开是全屏的
}

function setDrawerFull(on) {
  const d = document.getElementById("drawer");
  d.classList.toggle("is-full", !!on);
  const b = document.getElementById("drawer-full");
  const label = on ? "退出全屏" : "展开到全屏";
  b.textContent = on ? "⤡" : "⤢";
  b.setAttribute("aria-label", label);
  b.title = label;
}
function isDrawerFull() { return document.getElementById("drawer").classList.contains("is-full"); }

// ---------------------------------------------------------------- 工作流
//
// 工作流以前是另一个页面（workflow.html）。现在它是看板上的**主卡片**：
// 展开后是行列表（行号、状态、当前步骤、产物、耗时），旁边是控制按钮。
//
// 判据与旧页完全一致，**只动界面不改业务**：
//   - 跳过的行按 skipped 显示（判据只有 workflow_skips 一处）；
//   - 「失败行数」在服务端没给出来的时候写「没算出来」，不当成 0；
//   - 动作按状态开关：跑完了再点开始没有意义。

/** 跳过的行集合。库里它是 failed（tasks.status 没有 skipped）。 */
function skipSet(d) {
  return new Set((d && d.skips) || []);
}

/** 面向用户的那一行是什么状态。**跳过优先于 status**。 */
function rowStatus(row, skips) {
  return skips.has(Number(row.workflow_row)) ? "skipped" : (row.status || "queued");
}

/** 「只看失败行」与两个失败计数共用这一条判据。 */
function isFailedRow(row, skips) {
  return rowStatus(row, skips) === "failed";
}

function filteredWorkflows() {
  const q = state.filter.q.toLowerCase();
  return state.workflows.filter(w => {
    if (state.filter.status === "pending") {
      /* 工作流这边：「待验收」指的是**这一批里还有行没人认**，
       * 不是工作流这个对象的状态。 */
      if (!(Number(w.pending_acceptance_rows) > 0)) return false;
    } else if (state.filter.status) {
      const allow = FILTER_WORKFLOWS[state.filter.status] || [];
      if (allow.indexOf(w.status) < 0) return false;
    }
    if (state.filter.activeOnly && ACTIVE_WORKFLOWS.indexOf(w.status) < 0) return false;
    if (!q) return true;
    return (w.name || "").toLowerCase().indexOf(q) >= 0;
  });
}

async function loadWorkflows() {
  const got = await api("GET", "/api/workflows");
  const list = Array.isArray(got) ? got : (got.workflows || []);
  state.workflows = list;
  state.wfById = new Map(list.map(w => [Number(w.id), w]));
  renderWorkflows();
  renderFilterNote();
}

function renderWorkflows() {
  const host = document.getElementById("wf-cards");
  const empty = document.getElementById("wf-empty");
  if (!host) return;
  const list = filteredWorkflows();
  const seen = new Set();
  list.forEach(w => {
    const id = Number(w.id);
    seen.add(id);
    let node = state.wfCards.get(id);
    if (!node) { node = makeWorkflowCard(w); state.wfCards.set(id, node); }
    paintWorkflowCard(node, w);
    if (node.parentNode !== host) host.appendChild(node);
  });
  state.wfCards.forEach((node, id) => {
    if (!seen.has(id)) { node.remove(); state.wfCards.delete(id); }
  });
  empty.hidden = list.length > 0;
  document.getElementById("wf-zone-sub").textContent = list.length
    ? (list.length + " 个 · 表格每行一个计算任务，逐行串行跑")
    : "";
}

function makeWorkflowCard(w) {
  const card = el("article", "wf-card");
  card.dataset.id = w.id;

  const head = el("div", "wfc-head");
  head.append(el("div", "wfc-title", w.name || "（未命名工作流）"));
  head.append(el("span", "wfc-chip"));
  card.append(head);

  const meta = el("div", "wfc-meta");
  meta.append(el("span", "wfc-id", "#" + w.id));
  meta.append(el("span", "wfc-count"));
  meta.append(el("span", "wfc-failed"));
  meta.append(el("span", "wfc-share-mark"));
  meta.append(el("span", "wfc-pending"));
  card.append(meta);

  const bar = el("div", "bar");
  bar.append(el("div", "bar-fill"));
  card.append(bar);

  const stats = el("div", "wfc-stats");
  card.append(stats);

  /* 这一批用哪种方式验收。几十行的批量场景里，「这批我全要机器审」
   * 比逐行去任务里点现实得多。 */
  const wrev = el("div", "wfc-review");
  card.append(wrev);

  const acts = el("div", "wfc-acts");
  const toggle = el("button", "btn wfc-toggle", "展开");
  toggle.type = "button";
  toggle.onclick = () => toggleWorkflow(Number(w.id));
  acts.append(toggle);
  const edit = el("button", "btn", "编辑配置");
  edit.type = "button";
  edit.onclick = () => openComposerForWorkflow(Number(w.id));
  acts.append(edit);
  /* 「提交新任务」：**从这张卡片直接开提交框**。
   *
   * 场景是「暂停这一批，先去干别的」——用户点了暂停，接着要交新任务，
   * 而新任务的入口原来只在页面顶部那一栏。绕回去找「＋」在批量场景下
   * 很难注意到（页面一滚，卡片就看不见了）。
   *
   * 它开的仍然是**单个任务**的提交框（setComposerMode("task") 由
   * openComposer 负责），不是又开一个工作流。 */
  const newTask = el("button", "btn", "提交新任务");
  newTask.type = "button";
  newTask.onclick = () => openComposer();
  acts.append(newTask);
  /* 「共享」按钮。**能不能看见由 paintWorkflowCard 决定**（owner 或管理员），
   * 这里只是先把节点建出来——卡片是复用的，重建会丢滚动位置与展开状态。 */
  const share = el("button", "btn wfc-share-btn", "共享");
  share.type = "button";
  share.onclick = () => openShare(Number(w.id));
  acts.append(share);
  /* 「验收」按钮：**未验收行数 > 0 时才出现**。61 行那种批次逐行点是不现实的，
   * 所以这里是「把这一批还没人认的都认了」。 */
  const accept = el("button", "btn wfc-accept-btn", "验收");
  accept.type = "button";
  accept.onclick = () => acceptWorkflowRows(Number(w.id));
  acts.append(accept);
  acts.append(el("span", "wfc-ctrl"));
  card.append(acts);

  const rows = el("div", "wfc-rows");
  rows.hidden = true;
  card.append(rows);
  return card;
}

function paintWorkflowCard(card, w) {
  const id = Number(w.id);
  const failed = w.failed_rows;
  const hasFailed = failed !== undefined && failed !== null;
  const failedN = Number(failed) || 0;
  const total = Number(w.total_rows) || 0;
  const done = Number(w.cursor) || 0;

  /* **这里必须留着 "wfc-chip" 这个类**：它是下面 querySelector(".wfc-chip")
   * 的选择器。原先这里写的是 chip.className = "chip " + ...，把 wfc-chip
   * 整个换掉了——于是**第二次**重绘（轮询十分钟一次的那种）时
   * querySelector 返回 null，这行直接抛 TypeError，整个工作流区跟着
   * 渲染中断：统计、进度条、按钮状态全停在第一次的样子，展开也点不动。
   * 真机抓到过：控制台里一条 "Uncaught (in promise)"，每轮重绘报一次。 */
  const chip = card.querySelector(".wfc-chip");
  if (chip) {
    chip.className = "wfc-chip chip " + (WF_CHIP[w.status] || "chip-none");
    chip.textContent = WF_STATUS_LABEL[w.status] || w.status;
  }

  card.querySelector(".wfc-id").textContent = "#" + id;
  card.querySelector(".wfc-count").textContent = done + " / " + total + " 行";
  const fb = card.querySelector(".wfc-failed");
  /* **两个键缺一都要照实说**：缺 failed_rows 时写「没算出来」而不是 0，
   * 把「没这个数」显示成「没有失败行」等于对着一个没发生过的故障报平安。 */
  fb.textContent = !hasFailed
    ? "失败行数没算出来"
    : failedN > 0 ? ("失败 " + failedN + " 行") : "没有失败行";
  fb.className = "wfc-failed" + (failedN > 0 ? " is-bad" : "");
  card.classList.toggle("is-failed", failedN > 0);

  const bar = card.querySelector(".bar");
  const fill = bar.querySelector(".bar-fill");
  fill.style.width = (total > 0 ? Math.round(done * 100 / total) : 0) + "%";
  bar.classList.toggle("is-running", w.status === "running");

  /* 预计还要多久、每小时能跑几行：服务端给了就用，没给就不说。 */
  const st = state.wfStatus.get(id);
  const stats = card.querySelector(".wfc-stats");
  if (st) {
    const bits = [];
    if (st.eta_seconds != null) bits.push("预计还要 " + fmtDur(st.eta_seconds));
    if (st.throughput_rows_per_hour != null) {
      bits.push("每小时 " + st.throughput_rows_per_hour + " 行");
    }
    if (st.duration && st.duration.avg_row_seconds != null) {
      bits.push("每行平均 " + fmtDur(st.duration.avg_row_seconds));
    }
    stats.textContent = bits.join(" · ");
  } else {
    stats.textContent = "";
  }

  /* 验收方式（工作流级）。**跟着谁来的也要写出来**：不写的话，
   * 「这一批到底走的哪条路」就成了一件猜的事。 */
  const rv = card.querySelector(".wfc-review");
  if (rv) {
    rv.textContent = "";
    rv.append(el("span", "wfc-review-k", "验收方式"));
    rv.append(reviewModeSelect(w.review_mode || "", (v) => {
      setWorkflowReviewMode(w, v);
    }));
    const weff = w.review_mode_effective || "human";
    rv.append(el("span", "wfc-review-n",
      "现在生效：" + (REVIEW_MODE_LABEL[weff] || weff)));
  }

  /* 控制按钮按状态开关：跑完了再点开始没有意义，别的状态下点暂停是空转。 */
  const ctrl = card.querySelector(".wfc-ctrl");
  ctrl.textContent = "";
  if (w.status === "running") {
    ctrl.append(wfControlBtn(w.id, "pause", "暂停（当前行跑完再停）"));
    ctrl.append(wfControlBtn(w.id, "abort", "中止整批"));
  } else if (w.status === "paused") {
    ctrl.append(wfControlBtn(w.id, "resume", "继续（接着跑下一行）"));
    ctrl.append(wfControlBtn(w.id, "abort", "中止整批"));
  } else if (w.status === "draft") {
    ctrl.append(wfControlBtn(w.id, "start", "开始（逐行跑）"));
  } else {
    ctrl.append(el("span", "who", "已跑完。整批重跑要重新上传表格。"));
  }

  const toggle = card.querySelector(".wfc-acts .btn");
  const open = state.wfOpen.has(id);
  toggle.textContent = open ? "收起" : "展开";
  card.querySelector(".wfc-rows").hidden = !open;
  if (open) paintWorkflowRows(card.querySelector(".wfc-rows"), id);

  /* 共享标注：**共享来的**要写明是谁共享给我的、**我共享出去的**要写明有几个人。
   * 不写的话，共享者会在自己的列表里看到一个自己不能删的东西，主人也看不出
   * 这批活是不是已经摊给了别人。 */
  const mark = card.querySelector(".wfc-share-mark");
  if (mark) {
    const n = Number(w.share_count) || 0;
    if (w.shared_with_me === true) {
      mark.textContent = "共享给我 · 来自 " + (w.owner_username || "别人");
      mark.className = "wfc-share-mark is-shared";
    } else if (n > 0) {
      mark.textContent = "共享给 " + n + " 人";
      mark.className = "wfc-share-mark";
    } else {
      mark.textContent = "";
      mark.className = "wfc-share-mark";
    }
  }
  /* 「共享」只给创建者与管理员。共享者自己不能加人、不能转让——那是主人的权限。 */
  const shareBtn = card.querySelector(".wfc-share-btn");
  if (shareBtn) {
    shareBtn.hidden = !(w.is_owner === true || isAdmin());
  }
  /* 「未验收 N 行」。**这个数必须显眼**——它是「这一批的结果有多少还没人认」
   * 的唯一入口。行照跑照归档，所以它会一直挂在那儿直到有人处理。 */
  const pend = card.querySelector(".wfc-pending");
  const nPend = Number(w.pending_acceptance_rows) || 0;
  if (pend) {
    pend.textContent = nPend > 0 ? ("未验收 " + nPend + " 行") : "";
    pend.className = "wfc-pending" + (nPend > 0 ? " is-warn" : "");
  }
  const acceptBtn = card.querySelector(".wfc-accept-btn");
  if (acceptBtn) {
    acceptBtn.hidden = !(nPend > 0 && (w.is_owner === true || isAdmin()));
  }
}

function wfControlBtn(id, action, label) {
  const b = el("button", "btn", label);
  b.type = "button";
  b.onclick = async () => {
    if (action === "abort") {
      const ok = await confirmAction({
        title: "中止整批？",
        lead: "中止之后这一批不会再往下走，已经算出来的部分仍然留着。",
        items: [
          "当前正在跑的那一行会被立刻停掉",
          "已经归档的行结果仍能下载",
          "中止之后只能继续或者重新上传一份表",
        ],
        okLabel: "确认中止",
      });
      if (!ok) return;
    }
    b.disabled = true;
    try {
      await api("POST", "/api/workflows/" + id + "/" + action);
      await loadWorkflows();
      await refreshOpenWorkflow(id);
      setMsg(document.getElementById("board-msg"), "已" + actionLabel(action), "ok");
    } catch (e) {
      b.disabled = false;
      setMsg(document.getElementById("board-msg"), e.message || "操作失败", "bad");
    }
  };
  return b;
}

function actionLabel(action) {
  return { pause: "暂停", resume: "继续", abort: "中止", start: "开始" }[action] || action;
}

/* 展开 / 收起。展开时才去取行列表——没展开就不打那个接口。 */
async function toggleWorkflow(id) {
  if (state.wfOpen.has(id)) {
    state.wfOpen.delete(id);
    const node = state.wfCards.get(id);
    if (node) paintWorkflowCard(node, state.wfById.get(id));
    return;
  }
  state.wfOpen.add(id);
  const node = state.wfCards.get(id);
  if (node) paintWorkflowCard(node, state.wfById.get(id));
  await refreshOpenWorkflow(id);
}

async function refreshOpenWorkflow(id) {
  if (!state.wfOpen.has(id)) return;
  try {
    const d = await api("GET", "/api/workflows/" + id);
    state.wfDetail.set(id, d);
  } catch (e) {
    /* 读不到就保持上一次的样子，不把卡片清空。 */
  }
  /* 状态端点是**可选**的（还没上线时 404）：读不到就不显示预计时间，
   * 而不是显示一个空进度条。 */
  try {
    const st = await api("GET", "/api/status/workflow/" + id);
    state.wfStatus.set(id, st);
  } catch (e) {
    state.wfStatus.delete(id);
  }
  const node = state.wfCards.get(id);
  const w = state.wfById.get(id);
  if (node && w) paintWorkflowCard(node, w);
}

function paintWorkflowRows(host, id) {
  const d = state.wfDetail.get(id);
  host.textContent = "";
  if (!d) {
    host.append(el("p", "tl-empty", "正在取这一批的行…"));
    return;
  }
  const rows = d.rows || [];
  const skips = skipSet(d);
  const table = el("table", "wf-rows-table");
  const head = el("thead");
  const htr = el("tr");
  ["行", "状态", "当前步骤", "产物", "耗时", "操作"].forEach(t => htr.append(el("th", null, t)));
  head.append(htr);
  table.append(head);
  const body = el("tbody");
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "wf-rows-empty", "还没有跑起来的行");
    td.colSpan = 6;
    tr.append(td);
    body.append(tr);
  }
  rows.forEach(row => {
    const tr = el("tr");
    /* **整行可点**，跟看板上的任务卡一个动作：点开右侧抽屉看详情。
     * 抽屉自己会去拉 GET /api/tasks/{id}，而那个端点把工作流行任务也一起
     * 给了（行任务属于发起人），所以这里不用另外取数据。
     * 下面那个「下载产物包」按钮早就在 stopPropagation，不会误触发。 */
    tr.classList.add("is-clickable");
    tr.tabIndex = 0;
    tr.setAttribute("role", "button");
    tr.setAttribute("aria-label",
      "第 " + (Number(row.workflow_row) + 1) + " 行，"
      + (ROW_STATUS_LABEL[rowStatus(row, skips)] || row.status || "")
      + "，查看详情");
    tr.onclick = () => openDrawer(row.id);
    tr.onkeydown = e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDrawer(row.id); }
    };
    tr.append(el("td", "wf-rownum", String(Number(row.workflow_row) + 1)));
    const st = el("td");
    const dot = el("span", "wf-dot");
    dot.dataset.status = rowStatus(row, skips);
    st.append(dot);
    st.append(el("span", null, ROW_STATUS_LABEL[rowStatus(row, skips)] || row.status || ""));
    /* 验收标记。**行照跑照归档，验收状态逐行记**——所以「已完成」与
     * 「已完成但没人认」在行列表里必须分得开，不然用户扫一眼以为
     * 整批都交付了。 */
    const rc = reviewChip(row);
    if (rc) st.append(rc);
    tr.append(st);
    tr.append(el("td", "wf-row-step", row.step || "—"));
    const art = el("td");
    if (row.package_path) {
      const b = el("button", "mini", "下载产物包");
      b.type = "button";
      b.onclick = (e) => {
        e.stopPropagation();
        downloadTaskPackage(row.id, Number(row.workflow_row) + 1).catch(err =>
          setMsg(document.getElementById("board-msg"), err.message || "下载失败", "bad"));
      };
      art.append(b);
    } else {
      art.append(el("span", "who", "—"));
    }
    tr.append(art);
    const dur = spanSec(row.created_at, row.done_at || row.updated_at);
    tr.append(el("td", "wf-row-dur", dur == null ? "—" : fmtDur(dur)));
    /* 重跑这一行。**整行已经能点开详情了**，这个按钮是另一个动作，
     * 所以它自己 stopPropagation，不跟整行的点击抢。 */
    const act = el("td", "wf-row-act");
    const rb = el("button", "mini", "重跑");
    rb.type = "button";
    rb.onclick = (e) => {
      e.stopPropagation();
      retryTask(row.id, Number(row.workflow_row) + 1, id);
    };
    act.append(rb);
    /* 「续跑」也放在行列表上：**失败行就在这一列里**，让人点进详情才找得到
     * 续跑，等于把功能藏起来了。续跑与重跑的区别是「带着补充要求接着算」，
     * 对话框里写得很清楚。 */
    const cb = el("button", "mini", "续跑");
    cb.type = "button";
    /* 只有终态的行能续跑（跑着的行续跑会 422）。 */
    cb.disabled = !["done", "failed"].includes(row.status);
    cb.onclick = (e) => {
      e.stopPropagation();
      /* 复用抽屉里那一套：doReview 收的是任务对象，而工作流行本身就是
       * 任务行，字段（id/status/summary/acceptance）都是齐的。 */
      doReview(row, "continue");
    };
    act.append(cb);
    tr.append(act);
    body.append(tr);
  });
  table.append(body);
  host.append(table);
}


/* 重跑一个任务（工作流行任务也行）。**重跑要等当前这行跑完**——工作流是串行的，
 * 调度器会自己排队，所以这里提交完就完事，不用等它跑完。 */
async function retryTask(taskId, rowNo, wfId) {
  const ok = await confirmAction({
    title: "重跑第 " + rowNo + " 行？",
    lead: "这一行会从头再算一遍，几小时不等。",
    items: [
      "这一行的旧结果会被新结果覆盖",
      "工作流会等当前正在跑的那行跑完，再轮到它",
      "产物包会重新生成（旧的留在工作目录里）",
    ],
    okLabel: "确认重跑",
  });
  if (!ok) return;
  try {
    /* 动作端点返回详情信封，得经 detailTask() 拆过再用——
     * 这一层的约定是「凡是拿 {task, events, artifacts} 的地方都拆」，
     * 漏一处就是拿整个信封当任务对象用。 */
    state.byId.set(taskId, detailTask(
      await api("POST", "/api/tasks/" + taskId + "/retry", {}), "重跑任务"));
    setMsg(document.getElementById("board-msg"),
      "第 " + rowNo + " 行已重新排队，它会等当前这行跑完再开始", "good");
    if (wfId != null) refreshOpenWorkflow(wfId);
  } catch (e) {
    setMsg(document.getElementById("board-msg"),
      "重跑失败：" + (e.message || ""), "bad");
  }
}
/* 单独下某一行的产物包。走 fetch + blob：令牌在请求头里，<a> 带不了。 */
async function downloadTaskPackage(taskId, rowNo) {
  const r = await fetch(apiBase() + "/api/tasks/" + taskId + "/package", { headers: authHeaders() });
  if (!r.ok) throw await errFrom(r);
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = el("a");
  a.href = url;
  a.download = "第" + rowNo + "行-产物包.zip";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 20000);
}

// ---------------------------------------------------------------- 共享 / 转让
//
// 两件事，语义完全不同，界面上也必须分开：
//   - **共享**：加一个协作者。他能看、能跑、能改配置，但不能删除、不能再分享。
//   - **转让**：换主人。旧主人**立刻失去访问**，已有的行任务/产物/结果
//     全部转到新主人名下。
//
// 谁能做：创建者本人，或者管理员（管理员代替 owner 操作——他对别人的工作流
// 本来是只读的，但归属问题得有人能处理）。共享者**只能看**这份名单。
//
// **转让的二次确认做在对话框内部**，不套第二层 dialog：两个 <dialog> 叠着时
// 关闭顺序不可靠（Esc 传给谁、先关哪个都不确定），而转让是不可撤销的。
const shareCtx = {
  wfId: null,
  wf: null,          // 工作流视图（含 is_owner / owner_username）
  shares: [],        // [{user_id, username, ...}]
  canManage: false,  // 能不能加/删/转让
  users: [],         // 搜到的账户
  mode: "browse",    // browse | transfer
  transferTo: null,  // 待确认转让给谁
};

async function openShare(id) {
  try {
    const d = await api("GET", "/api/workflows/" + id);
    shareCtx.wfId = Number(id);
    shareCtx.wf = d.workflow || {};
    shareCtx.shares = d.shares || [];
    shareCtx.canManage = shareCtx.wf.is_owner === true || isAdmin();
    shareCtx.users = [];
    shareCtx.mode = "browse";
    shareCtx.transferTo = null;
    renderShare();
    const dlg = document.getElementById("share");
    if (!dlg.open) dlg.showModal();
  } catch (e) {
    setMsg(document.getElementById("board-msg"),
      e.message || "打不开共享面板", "bad");
  }
}

function renderShare() {
  const body = document.getElementById("sh-body");
  const wf = shareCtx.wf || {};
  document.getElementById("sh-title").textContent =
    "共享「" + (wf.name || "未命名工作流") + "」#" + (wf.id || shareCtx.wfId);
  document.getElementById("sh-lead").textContent = shareCtx.canManage
    ? "共享是加一个人一起用，转让是把整个工作流送走——两回事，别点错。"
    : "这个工作流是共享给你的。你能看、能跑、能改配置，但不能删除，也不能再分享给别人。";
  body.textContent = "";
  if (shareCtx.mode === "transfer") { renderTransferConfirm(); return; }

  /* 谁是主人、现在有谁在用。**共享者也看得到这份名单**——他要知道还有谁
   * 跟自己用着同一批活。 */
  const head = el("div", "share-sec");
  head.append(el("h3", "share-h", "现在有谁在用"));
  head.append(el("p", "share-note",
    "创建者：" + (wf.owner_username || "（未知）")
    + "。共享出去的人能看、能跑、能改配置，但不能删除，也不能再分享给别人。"));
  const list = el("ul", "share-list");
  if (!shareCtx.shares.length) {
    list.append(el("li", "share-empty", "还没有共享给任何人。"));
  }
  shareCtx.shares.forEach(s => {
    const li = el("li", "share-row");
    li.append(el("span", "share-name", s.username || ("#" + s.user_id)));
    if (shareCtx.canManage) {
      const rm = el("button", "btn btn-warn", "取消共享");
      rm.type = "button";
      rm.onclick = () => doRemoveShare(s);
      li.append(rm);
    }
    list.append(li);
  });
  head.append(list);
  body.append(head);

  if (!shareCtx.canManage) {
    body.append(el("p", "share-note",
      "你不是创建者，所以这里只能看。要加人或转让，请找创建者。"));
    return;
  }

  /* 加一个人：搜账号。**结果里直接给「共享给他」**，省得再挑一次。 */
  const add = el("div", "share-sec");
  add.append(el("h3", "share-h", "加一个人"));
  const input = el("input", "share-search");
  input.type = "search";
  input.placeholder = "搜用户名";
  input.autocomplete = "off";
  input.setAttribute("aria-label", "搜用户名");
  add.append(input);
  const hits = el("ul", "share-list");
  hits.id = "sh-hits";
  add.append(hits);
  body.append(add);

  /* 转让：另起一节，与「共享」在视觉上分开——一个是加一个人，一个是把
   * 整个工作流送走，混在一张列表里点错一次就是不可撤销的损失。 */
  const tr = el("div", "share-sec");
  tr.append(el("h3", "share-h", "转让整个工作流"));
  tr.append(el("p", "share-note",
    "转让之后你立刻失去这个工作流的访问权，已有的行任务、产物、结果全部转到"
    + "新主人名下。这一步不能撤销。"));
  const row = el("div", "share-row");
  const sel = el("select", "share-select");
  sel.id = "sh-transfer-sel";
  sel.setAttribute("aria-label", "选一个接管人");
  row.append(sel);
  const go = el("button", "btn btn-warn", "转让给他");
  go.type = "button";
  go.onclick = () => {
    const uid = Number(sel.value);
    const u = shareCtx.users.find(x => Number(x.id) === uid);
    if (!u) { setShareMsg("先在上面搜出一个人，再选他", "bad"); return; }
    shareCtx.transferTo = u;
    shareCtx.mode = "transfer";
    renderShare();
  };
  row.append(go);
  tr.append(row);
  body.append(tr);

  body.append(el("p", "share-msg", ""));
  refreshTransferOptions();

  let timer = null;
  input.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => searchShareUsers(input.value, hits), 220);
  };
  searchShareUsers("", hits);
}

/* 转让前的二次确认。**不套第二层 dialog**（见上面那段说明）。 */
function renderTransferConfirm() {
  const body = document.getElementById("sh-body");
  const wf = shareCtx.wf || {};
  const u = shareCtx.transferTo || {};
  const box = el("div", "share-sec");
  box.append(el("h3", "share-h",
    "确认把「" + (wf.name || "未命名工作流") + "」转让给 " + (u.username || "") + "？"));
  const ul = el("ul", "share-list");
  ["你会立刻失去访问这个工作流的权限",
   "已有的行任务、产物、结果全部转到 " + (u.username || "") + " 名下",
   "这一步不能撤销"].forEach(t => ul.append(el("li", null, t)));
  box.append(ul);
  const row = el("div", "share-row");
  const no = el("button", "btn", "算了");
  no.type = "button";
  no.onclick = () => { shareCtx.mode = "browse"; shareCtx.transferTo = null; renderShare(); };
  const ok = el("button", "btn btn-warn", "确认转让");
  ok.type = "button";
  ok.onclick = () => doTransfer(u);
  row.append(no, ok);
  box.append(row);
  body.append(box);
}

function setShareMsg(text, kind) {
  const p = document.querySelector("#sh-body .share-msg");
  if (!p) return;
  p.textContent = text || "";
  p.className = "share-msg" + (kind ? " is-" + kind : "");
}

async function searchShareUsers(q, host) {
  host.textContent = "";
  let users = [];
  try {
    const got = await api("GET", "/api/users?q=" + encodeURIComponent(q || ""));
    users = got.users || [];
  } catch (e) {
    host.append(el("li", "share-empty", "搜账号失败：" + (e.message || "")));
    return;
  }
  shareCtx.users = users;
  const owner = shareCtx.wf && Number(shareCtx.wf.owner_id);
  const sharedIds = new Set(shareCtx.shares.map(s => Number(s.user_id)));
  if (!users.length) {
    host.append(el("li", "share-empty", "没有匹配的账号。"));
  }
  users.forEach(u => {
    const li = el("li", "share-row");
    li.append(el("span", "share-name", u.username));
    if (Number(u.id) === owner) {
      li.append(el("span", "share-tag", "创建者"));
    } else if (sharedIds.has(Number(u.id))) {
      li.append(el("span", "share-tag", "已共享"));
    } else {
      const add = el("button", "btn", "共享给他");
      add.type = "button";
      add.onclick = () => doAddShare(u);
      li.append(add);
    }
    host.append(li);
  });
  /* **只显示前 N 个时要写明**。不写，用户会以为平台上就这么些人，
   * 而要找的人恰好在被截掉的那一半里。 */
  if (!q && users.length >= 50) {
    host.append(el("li", "share-empty",
      "只显示前 50 个账号，输名字可以精确找到。"));
  }
  refreshTransferOptions();
}

/* 转让的下拉：候选是**搜出来的账号 + 已经在共享名单里的人**。
 * 下拉里不列创建者自己——把工作流转给自己没有任何意义。 */
function refreshTransferOptions() {
  const sel = document.getElementById("sh-transfer-sel");
  if (!sel) return;
  const owner = shareCtx.wf && Number(shareCtx.wf.owner_id);
  const seen = new Map();
  shareCtx.users.forEach(u => seen.set(Number(u.id), u));
  shareCtx.shares.forEach(s => {
    if (!seen.has(Number(s.user_id))) {
      seen.set(Number(s.user_id), { id: s.user_id, username: s.username });
    }
  });
  const picked = sel.value;
  sel.textContent = "";
  let n = 0;
  seen.forEach(u => {
    if (Number(u.id) === owner) return;
    sel.append(new Option(u.username || ("#" + u.id), String(u.id)));
    n += 1;
  });
  if (!n) sel.append(new Option("（没有可选的人）", ""));
  if (picked) sel.value = picked;
}

async function doAddShare(u) {
  try {
    const got = await api("POST", "/api/workflows/" + shareCtx.wfId + "/shares",
                          { user_id: Number(u.id) });
    shareCtx.shares = got.shares || [];
    /* **先重画再写提示**：renderShare 会把整个 body 重建，提示写在
     * 前面的话会被这一次重建冲掉——用户看到的就永远是「什么都没发生」。 */
    renderShare();
    setShareMsg("已共享给 " + u.username, "good");
    await loadWorkflows();
  } catch (e) {
    setShareMsg(e.message || "共享失败", "bad");
  }
}

async function doRemoveShare(s) {
  try {
    const got = await api("DELETE",
      "/api/workflows/" + shareCtx.wfId + "/shares/" + Number(s.user_id));
    shareCtx.shares = got.shares || [];
    renderShare();
    setShareMsg("已取消与 " + (s.username || "") + " 的共享", "good");
    await loadWorkflows();
  } catch (e) {
    setShareMsg(e.message || "取消共享失败", "bad");
  }
}

async function doTransfer(u) {
  try {
    await api("POST", "/api/workflows/" + shareCtx.wfId + "/transfer",
              { user_id: Number(u.id) });
    const dlg = document.getElementById("share");
    if (dlg.open) dlg.close();
    setMsg(document.getElementById("board-msg"),
      "已把工作流转给 " + u.username, "good");
    await loadWorkflows();
  } catch (e) {
    shareCtx.mode = "browse";
    renderShare();
    setShareMsg(e.message || "转让失败", "bad");
  }
}

/* 批量验收：把这一批**还没人认**的行都认了。
 *
 * 61 行那种批次逐行点 61 次是不现实的，所以要有「一批都认了」这一个入口。
 * 只影响还没人认的行——已经认过的、还没跑的都不动。 */
async function acceptWorkflowRows(wfId) {
  const ok = await confirmAction({
    title: "把这一批还没人认的行都认了？",
    lead: "这会把这一批里所有「待验收」的行标成已验收。",
    items: [
      "认了就算交付完成（结果表里那一格照旧，不受影响）",
      "只影响还没人认的行：已经认过的、还没跑的不动",
      "之后对某一行不满意，仍然可以单独打回续跑那一行",
    ],
    okLabel: "确认验收",
  });
  if (!ok) return;
  try {
    const got = await api("POST", "/api/workflows/" + wfId + "/accept", {});
    setMsg(document.getElementById("board-msg"),
      (got.count || 0) + " 行已验收", "good");
    await loadWorkflows();
    await refreshOpenWorkflow(wfId);
  } catch (e) {
    setMsg(document.getElementById("board-msg"), e.message || "验收失败", "bad");
  }
}

// ---------------------------------------------------------------- 状态面板
//
// 三条硬口径：
//   1. **系统指标与全局速度只对管理员显示**，判据是**账号角色**（/api/me 的 role），
//      不是「哪个面板」——同一个账号在哪个面板上看到的范围都一样。
//   2. 普通用户**根本不发**那个请求：不是先请求再藏起来。普通用户调
//      /api/status/system 服务端会 403，前端连问都不问。
//   3. 接口还没上线时（404）显示「暂无数据」，不报错、不空白。

function isAdmin() { return !!(state.me && state.me.role === "admin"); }

async function loadStatus() {
  const admin = isAdmin();
  let ov = null;
  try {
    ov = await api("GET", "/api/status/overview");
  } catch (e) {
    if (e && e.status === 401) { signedOut("登录已过期，请重新登录"); return; }
    ov = null;
  }
  renderStatusOverview(ov, admin);
  if (!admin) return;
  let sys = null;
  try {
    sys = await api("GET", "/api/status/system");
  } catch (e) {
    sys = null;                       // 403 / 404 都按「拿不到」处理
  }
  renderStatusSystem(sys);
}

function metric(label, value, sub, tone) {
  const m = el("div", "sp-metric" + (tone ? " is-" + tone : ""));
  m.append(el("div", "sp-m-label", label));
  m.append(el("div", "sp-m-value", value));
  if (sub) m.append(el("div", "sp-m-sub", sub));
  return m;
}

function renderStatusOverview(ov, admin) {
  const host = document.getElementById("sp-metrics");
  const note = document.getElementById("sp-note");
  host.textContent = "";
  if (!ov || !ov.tasks) {
    host.append(el("p", "sp-empty", "暂无数据"));
    note.textContent = "";
    return;
  }
  const t = ov.tasks || {};
  const d = ov.duration || {};
  /* scope 写到面板上：管理员视角与本人视角不是同一件事，
   * CSS 可以据此把「全局」那几格标出来。 */
  document.getElementById("status-panel").dataset.scope = ov.scope || "";
  /* 口径写清楚：这个「任务总数」是独立计算任务 + 工作流行任务加在一起的，
   * 而下面的列表只列独立任务。两个数字对不上时，读者要能一眼看出为什么。 */
  const bd = ov.breakdown || {};
  note.textContent = (ov.scope === "admin" ? "全局" : "我的任务")
    + " · 计算任务 " + (bd.regular || 0)
    + " · 工作流行 " + (bd.workflow || 0);
  const total = Number(t.total) || 0;
  const running = (Number(t.queued) || 0) + (Number(t.running) || 0) + (Number(t.packaging) || 0);
  const failed = Number(t.failed) || 0;

  /* 口径写在这一格的副标题里：数字是「独立计算任务 + 工作流行任务」
   * 加起来的，而下面的列表只列独立任务。**写在这里而不是面板标题上**——
   * 标题那行要留给「管理员视角 / 我的任务」，那是另一件事。 */
  host.append(metric("任务总数", String(total),
    "计算 " + (bd.regular || 0) + " · 工作流行 " + (bd.workflow || 0)));
  host.append(metric("进行中", String(running),
    "排队 " + (Number(t.queued) || 0) + " · 计算 " + (Number(t.running) || 0)
    + " · 打包 " + (Number(t.packaging) || 0)));
  host.append(metric("已完成", String(Number(t.done) || 0), null));
  host.append(metric("失败", String(failed), failed ? "要处理" : null, failed ? "bad" : null));
  const rate = ov.success_rate;
  host.append(metric("成功率", (rate == null ? "—" : Math.round(rate * 100) + "%"),
    null, failed ? "warn" : null));
  if (d.avg_seconds != null) {
    host.append(metric("平均耗时", fmtDur(d.avg_seconds),
      d.median_seconds != null ? ("中位 " + fmtDur(d.median_seconds)) : null));
  }
  /* 全局速度**只给管理员**：普通用户的 overview 里压根没有这个字段。 */
  if (admin && ov.throughput_per_hour != null) {
    host.append(metric("全局速度", ov.throughput_per_hour + " 个/小时", "全部任务", "accent"));
  }
  note.textContent = ov.scope === "admin" ? "管理员视角 · 全平台" : "我的任务";
}

function renderStatusSystem(sys) {
  const host = document.getElementById("sp-system");
  host.textContent = "";
  if (!sys) {
    host.hidden = false;
    host.append(el("p", "sp-empty", "系统指标暂无数据"));
    return;
  }
  host.hidden = false;
  const bar = (label, pct, text) => {
    const b = el("div", "sp-bar-item");
    const top = el("div", "sp-bar-head");
    top.append(el("span", "sp-bar-label", label));
    top.append(el("span", "sp-bar-val", text));
    b.append(top);
    const track = el("div", "sp-bar");
    const fill = el("div", "sp-bar-fill");
    fill.style.width = Math.max(0, Math.min(100, Number(pct) || 0)) + "%";
    track.append(fill);
    b.append(track);
    return b;
  };
  const cpu = sys.cpu || {};
  const mem = sys.memory || {};
  const gpu = sys.gpu || {};
  const disk = sys.disk || {};

  host.append(bar("CPU", cpu.percent,
    (cpu.percent == null ? "—" : cpu.percent + "%") + " · " + (cpu.cores || 0) + " 核"));
  host.append(bar("内存", mem.percent,
    (mem.percent == null ? "—" : mem.percent + "%") + " · "
    + (mem.used_gb == null ? "—" : mem.used_gb) + " / " + (mem.total_gb == null ? "—" : mem.total_gb) + " GB"));
  if (gpu.available === false) {
    host.append(el("div", "sp-note-line", "无 GPU"));
  } else {
    host.append(bar("GPU", gpu.util_percent,
      (gpu.util_percent == null ? "—" : gpu.util_percent + "%") + " · "
      + (gpu.used_gb == null ? "—" : gpu.used_gb) + " / " + (gpu.total_gb == null ? "—" : gpu.total_gb) + " GB"));
  }
  host.append(bar("磁盘", disk.total_gb ? ((Number(disk.used_gb) || 0) / disk.total_gb * 100) : 0,
    (disk.used_gb == null ? "—" : disk.used_gb) + " / " + (disk.total_gb == null ? "—" : disk.total_gb)
    + " GB · 剩 " + (disk.free_gb == null ? "—" : disk.free_gb) + " GB"));
}

// ---------------------------------------------------------------- 提交表单
//
// 两种模式：**单个任务** 与 **工作流（表格 + 模板）**。
// 工作流不再是另一个界面——新建与修改都走这一张表，字段按模式切换。

function composerMode() {
  const on = document.querySelector("#mode-switch .seg-btn.is-on");
  return on ? on.dataset.mode : "task";
}

function setComposerMode(mode) {
  document.querySelectorAll("#mode-switch .seg-btn").forEach(b =>
    b.classList.toggle("is-on", b.dataset.mode === mode));
  const isTask = mode === "task";
  /* 工作流那一侧要塞一张表（列的用途 + 提示词模板），32rem 宽根本转不开——
   * 列名下拉框被挤成两列，看着就还是「毛坯」。给它宽一点。 */
  const dlg = document.getElementById("composer");
  if (dlg) dlg.classList.toggle("is-wide", !isTask);
  document.getElementById("c-task-fields").hidden = !isTask;
  document.getElementById("c-wf-fields").hidden = isTask;
  document.getElementById("c-title-h").textContent =
    isTask ? "提交计算任务" : (state.wfEdit ? "修改工作流配置" : "新建工作流");
  document.getElementById("c-lead").textContent = isTask
    ? "用一句话说清想算什么。提交后平台自己编排技能链，中间不用管，进度在看板上看得到。"
    : "上传一张表格，每一行算一个计算任务，逐行串行跑。表格绑定的变量写成两个大括号包起来，例如 {{seq}}。";
  document.getElementById("c-save").textContent = isTask
    ? "提交" : (state.wfEdit ? "保存配置" : "开始（逐行跑）");
  /* 单个任务的两个必填项在切到工作流模式时不该再挡着提交。 */
  document.getElementById("c-title").required = isTask;
  document.getElementById("c-brief").required = isTask;
}

function openComposer() {
  state.wfEdit = null;
  state.wfDraft = null;
  state.wfCols = [];
  document.getElementById("c-title").value = "";
  document.getElementById("c-brief").value = "";
  document.getElementById("c-wf-file").value = "";
  document.getElementById("c-wf-template").value = "";
  document.getElementById("c-wf-writeback").checked = true;
  document.getElementById("c-wf-cols").textContent = "";
  document.getElementById("c-wf-note").textContent = "";
  setComposerMode("task");
  const dlg = document.getElementById("composer");
  dlg.showModal();
  document.getElementById("c-title").focus();
}

/* 修改一个已存在的工作流：把它的列与模板读进来改。 */
async function openComposerForWorkflow(id) {
  state.wfEdit = Number(id);
  state.wfDraft = null;
  let d;
  try {
    d = await api("GET", "/api/workflows/" + id);
  } catch (e) {
    setMsg(document.getElementById("board-msg"), e.message || "打不开这个工作流", "bad");
    return;
  }
  const wf = d.workflow || {};
  state.wfCols = (d.columns || []).map(c => ({
    col_index: Number(c.col_index),
    header: c.header || "",
    role: c.role,
    slot: c.slot || "",
    field: c.field || "",
  }));
  document.getElementById("c-title").value = "";
  document.getElementById("c-brief").value = "";
  document.getElementById("c-wf-template").value = wf.prompt_template || "";
  document.getElementById("c-wf-writeback").checked = !!Number(wf.writeback);
  document.getElementById("c-wf-note").textContent =
    "正在修改「" + (wf.name || ("#" + id)) + "」· " + (Number(wf.total_rows) || 0) + " 行";
  setComposerMode("workflow");
  renderWfCols();
  const dlg = document.getElementById("composer");
  dlg.showModal();
}

/* 列的角色：每一列一个下拉（忽略 / 标识列 / 输入列 / 上下文 / 结果列），
 * 选了输入列或上下文再给一个变量名，结果列给一个结果字段。
 *
 * **按角色分组摆**：三组各有各的目的，平铺在一起时用户得逐行读下拉框
 * 才知道这一列要干什么——那正是「毛坯」的样子。
 *   不参与提示词：ignore / key
 *   写进提示词　：input / context（还要一个变量名）
 *   写回结果表　：output（还要一个结果字段）
 * 每一行用**同一套三列网格**（列名 / 角色 / 附加字段），于是所有行天然对齐。
 */
const WF_COL_GROUPS = [
  { key: "plain", roles: ["ignore", "key"], title: "不参与提示词",
    hint: "这些列只是给人看的，运行时不会写进任务描述。" },
  { key: "prompt", roles: ["input", "context"], title: "写进提示词",
    hint: "这些列的内容会进到每一行的任务描述里，变量名要跟提示词模板对得上。" },
  { key: "out", roles: ["output"], title: "写回结果表",
    hint: "这些列留空，跑完由平台把结果填回去。" },
];

function renderWfCols() {
  const host = document.getElementById("c-wf-cols");
  host.textContent = "";
  if (!state.wfCols.length) {
    host.append(el("p", "who", "先上传一张表格，表头出来之后在这里选每一列的用途。"));
    return;
  }
  const bound = new Set();
  state.wfCols.forEach(c => {
    if ((c.role === "input" || c.role === "context") && c.slot) bound.add(c.slot);
  });

  for (const g of WF_COL_GROUPS) {
    const cols = state.wfCols.filter(c => g.roles.indexOf(c.role) !== -1);
    if (!cols.length) continue;
    /* 每组按**自己那一列的宽度**排：三列的组（写进提示词）留出变量名，
     * 只有两列的组（不参与提示词 / 写回结果表）让角色下拉吃掉剩余宽度，
     * 否则右边空一大块，看着还是没收拾利索。 */
    const box = el("section", "wf-col-group wf-col-group--" + g.key);
    const head = el("div", "wf-col-group-head");
    head.append(el("h3", "wf-col-group-title", g.title));
    head.append(el("span", "wf-col-group-n", cols.length + " 列"));
    box.append(head);
    box.append(el("p", "wf-col-group-hint", g.hint));
    const list = el("div", "wf-col-list");
    for (const c of cols) list.append(wfColRow(c, bound));
    box.append(list);
    host.append(box);
  }

  /* 模板里用到、却还没绑到列上的变量——直接点名，省得用户自己数。 */
  const tpl = (document.getElementById("c-wf-template") || {}).value || "";
  const used = templateSlots(tpl);
  if (used.length) {
    const missing = used.filter(v => !bound.has(v));
    const p = el("p", "wf-col-warn" + (missing.length ? " is-bad" : ""));
    p.textContent = missing.length
      ? ("模板里的变量还没有对应的列：" + missing.join("、"))
      : ("模板用到的 " + used.length + " 个变量都已绑到列上。");
    host.append(p);
  }
}

/* 一行：列名 / 角色 / 附加字段（变量名或结果字段）。 */
function wfColRow(c, bound) {
  const row = el("div", "wf-col-row");
  row.append(el("span", "wf-col-name", c.header || ("列" + (c.col_index + 1))));
  const sel = el("select", "wf-col-role");
  sel.setAttribute("aria-label", "这一列的用途");
  WF_ROLES.forEach(r => sel.append(new Option(WF_ROLE_LABEL[r], r)));
  sel.value = c.role;
  sel.onchange = () => {
    const role = sel.value;
    c.role = role;
    if (role !== "input" && role !== "context") c.slot = "";
    if (role !== "output") c.field = "";
    if (role === "output" && !c.field) c.field = WF_OUTPUT_FIELDS[1];
    renderWfCols();
  };
  row.append(sel);
  if (c.role === "input" || c.role === "context") {
    const slot = el("input", "wf-col-slot");
    slot.type = "text";
    slot.placeholder = "变量名，例如 seq";
    slot.value = c.slot || "";
    slot.oninput = () => { c.slot = slot.value; };
    row.append(slot);
    if (c.slot) row.classList.add("is-bound");
  } else if (c.role === "output") {
    const f = el("select", "wf-col-field");
    f.setAttribute("aria-label", "结果字段");
    WF_OUTPUT_FIELDS.forEach(k => f.append(new Option(WF_OUTPUT_LABEL[k], k)));
    f.value = c.field || WF_OUTPUT_FIELDS[1];
    f.onchange = () => { c.field = f.value; };
    row.append(f);
  }
  return row;
}

/* 模板里的变量名，按出现顺序去重。 */
function templateSlots(tpl) {
  const out = [];
  const re = /\{\{\s*([^{}\s]+)\s*\}\}/g;
  let m;
  while ((m = re.exec(tpl || "")) !== null) {
    if (out.indexOf(m[1]) === -1) out.push(m[1]);
  }
  return out;
}

/* 第 2 步还缺什么：返回空串就是配好了。
 * 判据与服务端 validate_binding 同一套——模板不是那句占位文本、每条输入/
 * 上下文列都绑了变量名、模板里的变量都有列、绑了的变量模板里都用上了。 */
function wfMissingConfig() {
  const tpl = (document.getElementById("c-wf-template").value || "").trim();
  if (!tpl) return "先写提示词模板";
  const bound = state.wfCols.filter(c => c.role === "input" || c.role === "context");
  if (!bound.length) return "至少把一列设成输入列或上下文";
  if (bound.some(c => !(c.slot || "").trim())) return "有输入列或上下文列还没绑变量名";
  const slots = bound.map(c => (c.slot || "").trim());
  const used = templateSlots(tpl);
  const unbound = used.filter(n => slots.indexOf(n) === -1);
  if (unbound.length) return "提示词里的这些变量没有列绑定：" + unbound.join("、");
  const unused = slots.filter(n => used.indexOf(n) === -1);
  if (unused.length) return "这些列绑了变量但模板里没用到：" + unused.join("、");
  return "";
}

/* 上传表格。FormData 自己带 boundary，**不能**手写 Content-Type，
 * 否则浏览器不会补 boundary，服务端那边 multipart 解析直接失败。 */
async function uploadWorkflowFile() {
  const f = document.getElementById("c-wf-file").files[0];
  const note = document.getElementById("c-wf-note");
  if (!f) { note.textContent = "先选一个 xlsx 表格。"; return; }
  note.textContent = "上传中…";
  const fd = new FormData();
  fd.append("file", f);
  try {
    const r = await fetch(apiBase() + "/api/workflows/upload",
      { method: "POST", headers: authHeaders(), body: fd });
    if (!r.ok) throw await errFrom(r);
    const d = await r.json();
    state.wfDraft = d;
    state.wfEdit = null;
    state.wfCols = (d.headers || []).map((h, i) => ({
      col_index: i, header: h, role: "ignore", slot: "", field: "",
    }));
    note.textContent = f.name + " 共 " + (Number(d.row_count) || 0)
      + " 行，选好每一列的用途再写模板。";
    setComposerMode("workflow");
    renderWfCols();
  } catch (e) {
    note.textContent = "上传失败：" + (e.message || "");
  }
}

/* 保存列配置与模板。列是**整体替换**，所以服务端要的是完整的一份。 */
async function saveWorkflowConfig(id) {
  return api("POST", "/api/workflows", {
    workflow_id: id,
    prompt_template: document.getElementById("c-wf-template").value,
    columns: state.wfCols.map(c => ({
      col_index: c.col_index,
      header: c.header,
      role: c.role,
      slot: (c.role === "input" || c.role === "context") ? (c.slot || "").trim() : "",
      field: c.role === "output" ? c.field : "",
    })),
    writeback: document.getElementById("c-wf-writeback").checked,
  });
}

document.getElementById("c-cancel").onclick = () => document.getElementById("composer").close();

/* 共享面板的关闭。**顺带把状态清干净**：转让确认态是 shareCtx.mode 里的
 * 一个值，下次打开若忘了复位，会直接落在「确认转让」那一屏上。 */
document.getElementById("sh-cancel").onclick = () => {
  shareCtx.mode = "browse";
  shareCtx.transferTo = null;
  document.getElementById("share").close();
};

document.getElementById("composer-form").addEventListener("submit", async e => {
  e.preventDefault();
  const save = document.getElementById("c-save");
  save.disabled = true;
  try {
    if (composerMode() === "task") {
      const created = detailTask(
        await api("POST", "/api/tasks", {
          title: document.getElementById("c-title").value.trim(),
          brief: document.getElementById("c-brief").value.trim(),
        }), "提交任务");
      document.getElementById("composer").close();
      await load();
      openDrawer(created.id);
      return;
    }

    /* 工作流模式：新建要先有表（上传那一步已经建了记录），
     * 修改则直接用现有 id。 */
    const id = state.wfEdit || (state.wfDraft && state.wfDraft.workflow
      ? Number(state.wfDraft.workflow.id) : null);
    if (!id) throw new Error("先上传一张表格");
    const gap = wfMissingConfig();
    if (gap) throw new Error(gap);
    await saveWorkflowConfig(id);
    if (!state.wfEdit) {
      await api("POST", "/api/workflows/" + id + "/start");
    }
    document.getElementById("composer").close();
    await loadWorkflows();
    setMsg(document.getElementById("board-msg"),
      state.wfEdit ? "工作流配置已保存" : "工作流已提交，逐行开跑", "ok");
  } catch (err) {
    if (err && err.status === 401) signedOut("登录已过期，请重新登录");
    else setMsg(document.getElementById("board-msg"), err.message || "提交失败", "bad");
  } finally {
    save.disabled = false;
  }
});

document.getElementById("c-wf-upload").onclick = uploadWorkflowFile;

// ---------------------------------------------------------------- 登录

function showLogin(msg) {
  const dlg = document.getElementById("login");
  const err = document.getElementById("login-error");
  err.textContent = msg || "";
  err.hidden = !msg;
  if (!dlg.open) dlg.showModal();
  document.getElementById("l-user").focus();
}

function signedOut(msg) {
  setToken("");
  state.me = null;                 // 身份没了：兜底轮询与重试都以它为闸，不清就一直空转
  state.streams.forEach((ctl, id) => stopStream(id));
  state.streams.clear();
  state.items = [];
  state.byId.clear();
  state.cards.forEach(n => n.remove());
  state.cards.clear();
  state.cols.forEach(c => { c.list.textContent = ""; c.count.textContent = "0"; });
  state.workflows = [];
  state.wfById.clear();
  state.wfCards.forEach(n => n.remove());
  state.wfCards.clear();
  closeDrawer();
  document.getElementById("who").textContent = "";
  document.getElementById("sp-metrics").textContent = "";
  document.getElementById("sp-system").hidden = true;
  showLogin(msg || "请登录");
}

document.getElementById("login-form").addEventListener("submit", async e => {
  // <form method="dialog"> 默认会直接关掉；先 preventDefault，自己走异步提交，
  // 失败时对话框要留着让人改，成功才关。
  e.preventDefault();
  const btn = document.getElementById("login-btn");
  const err = document.getElementById("login-error");
  btn.disabled = true;
  err.hidden = true;
  try {
    const got = await api("POST", "/api/auth/login", {
      username: document.getElementById("l-user").value.trim(),
      password: document.getElementById("l-pass").value,
    });
    setToken(got.token || "");
    document.getElementById("l-pass").value = "";
    document.getElementById("login").close();
    await boot();
  } catch (ex) {
    err.textContent = ex.message || "登录失败";
    err.hidden = false;
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- 工具条

let qTimer = null;
document.getElementById("q").oninput = e => {
  // 纯前端筛选：GET /api/tasks 没有声明 q 参数（服务端只按 owner 过滤），
  // 拿关键词去请求后端只会白跑一趟，而且搜不搜得到全看后端怎么实现。
  state.filter.q = e.target.value.trim();
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { render(); renderWorkflows(); renderFilterNote(); }, 120);
};
document.getElementById("status-filter").onchange = e => {
  state.filter.status = e.target.value;
  render(); renderWorkflows(); renderFilterNote();
};
document.getElementById("only-active").onchange = e => {
  state.filter.activeOnly = e.target.checked;
  render(); renderWorkflows(); renderFilterNote();
};
document.getElementById("view-switch").onclick = e => {
  const b = e.target.closest(".seg-btn");
  if (!b) return;
  state.filter.view = b.dataset.view;
  applyView();
};
document.getElementById("mode-switch").onclick = e => {
  const b = e.target.closest(".seg-btn");
  if (!b) return;
  setComposerMode(b.dataset.mode);
};
document.getElementById("new-btn").onclick = () => openComposer();
document.getElementById("market-btn").onclick = () => openMarket();
document.getElementById("mt-cancel").onclick = () => document.getElementById("market").close();
document.getElementById("mt-d-back").onclick = () => {
  document.getElementById("mt-detail").hidden = true;
  document.getElementById("mt-grid").hidden = false;
  document.getElementById("mt-search").hidden = false;
  document.getElementById("mt-cats").hidden = false;
  mtRenderGrid();
};
document.getElementById("mt-run").onclick = () => mtRun();
document.getElementById("mt-search").addEventListener("input", (e) => {
  MT.q = e.target.value || "";
  mtRenderGrid();
});
document.getElementById("mt-batch").addEventListener("change", (e) => {
  document.getElementById("mt-batch-list").hidden = !e.target.checked;
});
document.getElementById("mt-fill").onclick = () => mtFillParams();
document.getElementById("mt-doc-box").addEventListener("toggle", (e) => {
  if (!e.target.open) return;
  // 展开文档就把它带到眼前——不然内容在视口外，看不见自己展开了什么。
  e.target.scrollIntoView({ block: "start" });
});
document.getElementById("logout-btn").onclick = () => signedOut("已退出");
document.getElementById("drawer-close").onclick = () => closeDrawer();
document.getElementById("drawer-full").onclick = () => setDrawerFull(!isDrawerFull());

/* Esc：全屏时先退出全屏，再按一次才关抽屉。直接一步关掉会让"我只是想看全屏"
 * 变成丢上下文。
 *
 * **任何一个 <dialog> 开着时一律不插手**：模态 dialog 自己吃 Esc，关掉的是它。
 * 不避让的话，确认表开着按 Esc 会连底下的抽屉一起关掉，而 cancel 事件又没
 * 正常走到，Promise 永远不 resolve，按钮卡在 disabled 再也点不动。 */
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (document.querySelector("dialog[open]")) return;
  const d = document.getElementById("drawer");
  if (d.hidden) return;
  e.preventDefault();
  if (isDrawerFull()) setDrawerFull(false);
  else closeDrawer();
});

/* 兜底轮询：SSE 不可用时（反代没开、通道被中间层掐了）至少别让看板僵住。
 * SSE 可用时它只是兜底，不承担实时性。页面在后台标签时不跑，省得空转。 */
setInterval(() => {
  if (!state.me) return;          // 还没通过 /api/me 确认身份（含没登录）就别空转
  if (document.visibilityState !== "visible") return;
  reload();
  loadWorkflows().catch(() => {});
  state.wfOpen.forEach(id => refreshOpenWorkflow(id));
}, 10000);

// 计划里说这批名字暴露给控制台/端到端调试。顶层 const 不会挂到 window 上，
// 显式挂一份，控制台敲 window.state 才拿得到。
Object.assign(window, {
  api, state, el, render, load, reload, filtered, fmtTime,
  STATUS_ORDER, STATUS_LABEL, COLUMNS, buildRail, paintRail, slideRail,
  railIndex, openDrawer, closeDrawer, setDrawerFull, confirmAction,
  taskEvents, FIELD_CN, KIND_LABEL, ACTOR_LABEL, apiBase, detailTask,
  loadWorkflows, renderWorkflows, filteredWorkflows, toggleWorkflow, loadStatus,
  drawTimeline, WF_STATUS_LABEL, ROW_STATUS_LABEL,
});

/* 登录与否由服务端说了算，不由 localStorage 里有没有令牌说了算。
 * 早先这里写的是 if (!token()) return showLogin() —— 那是**替服务端猜**：
 * 令牌存在与否只说明"上次点过登录"，不代表"现在还登录着"。服务端开了旁路
 * （或换成别的鉴权方式）时压根没有令牌，/api/me 却完全通，于是面板永远
 * 卡在登录框，接口明明是 200。改成先问 /api/me，401 才弹登录框。 */
async function boot() {
  buildCols();
  renderStatusFilter();
  try {
    state.me = await api("GET", "/api/me");
    const who = state.me || {};
    document.getElementById("who").textContent =
      (who.username || "") + (who.role === "admin" ? "（管理员）" : "");
    await load();
    /* 工作流读不到不该挡住看板：接口 404 时只是没有工作流这一块。 */
    await loadWorkflows().catch(() => {});
    await loadStatus().catch(() => {});
  } catch (e) {
    if (e && e.status === 401) {
      // 有过令牌才叫"过期"；一进来就没有是"还没登录"，提示语不该混成一句
      signedOut(token() ? "登录已过期，请重新登录" : "请登录");
    } else {
      setMsg(document.getElementById("board-msg"), (e && e.message) || "连不上服务", "bad");
    }
  }
  applyView();
}

boot();
