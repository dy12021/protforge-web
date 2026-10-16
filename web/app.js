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
  summary: "结果说明", error: "失败原因",
};

const state = {
  items: [],            // GET /api/tasks 的快照（最新在前）
  byId: new Map(),      // id -> task
  cards: new Map(),     // id -> 卡片元素（复用，不要每次重建：重建就没有滑轨动画）
  events: new Map(),    // taskId -> Map(eventId -> event)
  reached: new Map(),   // taskId -> 走到的站序；failed 时用它把红点放对位置
  streams: new Map(),   // taskId -> { stop, ac, lastId }
  cols: new Map(),      // status -> 列的 .col-list
  filter: { q: "", status: "", activeOnly: false },
  openId: null,         // 抽屉当前开着的任务
  editing: false,       // 抽屉里有字段正在编辑 —— 此时**不重绘抽屉**
  me: null,
  canEdit: true,        // 服务端没开放编辑时降级成纯只读（见 PATCH 分支）
  canDelete: true,
};

const TOKEN_KEY = "protforge.token";

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
function setToken(v) { if (v) localStorage.setItem(TOKEN_KEY, v); else localStorage.removeItem(TOKEN_KEY); }

/* base 走同源 /api。开发时后端可能不在同一个源（PROTFORGE_WEB_BASE 由服务端
 * 注入成 window 全局，或用 ?base= 临时指过去），所以留一个覆盖口。 */
function apiBase() {
  const q = new URLSearchParams(location.search).get("base");
  const b = q || window.PROTFORGE_WEB_BASE || "";
  return (b ? b.replace(/\/+$/, "") : "") + "/api";
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

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null && text !== "") n.textContent = text;
  return n;
}

/* 时间戳：'2026-10-16T14:03:22.123' -> '10-16 14:03'。
 * 中间那个 T 是 ISO 格式留下的，不去掉就不是给人看的。 */
function fmtTime(ts) {
  if (typeof ts !== "string" || ts.length < 16 || ts[10] !== "T") return ts || "";
  return ts.slice(5, 10) + " " + ts.slice(11, 16);
}

function setMsg(host, text, kind) {
  host.textContent = text;
  host.className = (host.id === "board-msg" ? "board-msg" : "drawer-msg") + (kind ? " is-" + kind : "");
  host.hidden = false;
}
function hideMsg(host) { host.hidden = true; host.textContent = ""; }

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
  c.append(body, step, fail);
  c.addEventListener("click", () => openDrawer(t.id));
  c.addEventListener("keydown", e => { if (e.key === "Enter") openDrawer(t.id); });
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
  c.querySelector(".card-time").textContent = fmtTime(t.updated_at || t.created_at);
  const step = c.querySelector(".card-step");
  const st = failed ? "" : (t.step || "");
  step.hidden = !st;                                  // 依赖文件开头那条 [hidden]
  if (st) c.querySelector(".card-step-t").textContent = st;
  const fail = c.querySelector(".card-fail");
  fail.hidden = !(failed && t.error);
  if (!fail.hidden) c.querySelector(".card-fail-t").textContent = t.error;
}

// ---------------------------------------------------------------- 看板

function buildCols() {
  const board = document.getElementById("board");
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
    if (state.filter.status && t.status !== state.filter.status) return false;
    if (state.filter.activeOnly && !ACTIVE.has(t.status)) return false;
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
  sel.append(new Option("全部状态", ""));
  COLUMNS.forEach(c => sel.append(new Option(c.label, c.key)));
  sel.value = state.filter.status;
}

// ---------------------------------------------------------------- 数据加载

let loadSeq = 0;

async function load() {
  const seq = ++loadSeq;
  const items = await api("GET", "/api/tasks");
  if (seq !== loadSeq) return;             // 已经有更新的请求在飞，丢掉这次
  applyItems(items);
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
      const t = await api("GET", "/api/tasks/" + id);
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

// ---------------------------------------------------------------- 抽屉

/* 时间线是活动流，不是日志表：一条竖线 + 一个点，主文本在上、时间与操作人在下。
 * 两点硬要求：不裸露字段名（FIELD_CN 翻译），状态跃迁染成状态芯片而不是文字箭头。
 * `step` 事件**不进时间线**：一次计算里它能被调用几百次，混进来时间线就被刷屏，
 * 而"当前在干什么"只需要卡片上那一条（SPEC 20.3）。 */
function statusChip(key) {
  if (!key) return el("span", "chip chip-none", "—");
  return el("span", "chip chip-" + key, STATUS_LABEL[key] || key);
}

function prettyEdited(msg) {
  const m = /^修改了：(.*)$/.exec(msg || "");
  if (!m) return "改了内容";
  const names = m[1].split("、").map(x => FIELD_CN[x] || x);
  return "改了" + names.join("、");
}

function eventLine(e) {
  const li = el("li", "tl-item tl-" + e.kind);
  li.append(el("span", "tl-dot"));
  const main = el("div", "tl-main");
  const line = el("div", "tl-line");
  if (e.kind === "status") {
    line.append(statusChip(e.from_status), el("span", "tl-arrow", "→"), statusChip(e.to_status));
    if (e.message) line.append(el("span", "tl-what", e.message));
  } else if (e.kind === "created") {
    line.append(el("span", "tl-what", "提交了这个计算任务"));
  } else if (e.kind === "edited") {
    line.append(el("span", "tl-what", prettyEdited(e.message)));
  } else {
    line.append(el("span", "tl-what",
      (KIND_LABEL[e.kind] || e.kind) + (e.message ? "：" + e.message : "")));
  }
  main.append(line, el("div", "tl-meta",
    fmtTime(e.created_at) + " · " + (ACTOR_LABEL[e.actor] || e.actor)));
  li.append(main);
  return li;
}

function taskEvents(id) {
  const m = state.events.get(id);
  const list = m ? [...m.values()].filter(e => e.kind !== "step") : [];
  return list.sort((a, b) => (a.id || 0) - (b.id || 0) || (a.created_at || "").localeCompare(b.created_at || ""));
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
    setMsg(drawerMsgHost(), "服务端未开放编辑，以下内容改为只读", "warn");
    return true;
  }
  return false;
}

function drawerMsgHost() { return document.getElementById("drawer-msg"); }

async function openDrawer(id) {
  state.openId = id;
  state.editing = false;
  const d = document.getElementById("drawer");
  d.hidden = false;
  const body = document.getElementById("drawer-body");
  body.textContent = "";
  let t = state.byId.get(id);
  try {
    t = await api("GET", "/api/tasks/" + id);
    state.byId.set(id, t);
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
          const got = await api("PATCH", "/api/tasks/" + id, { brief: ta.value });
          state.byId.set(id, got);
          setMsg(drawerMsgHost(), "需求说明已保存", "ok");
          done(got.brief == null ? "" : got.brief);
        } catch (e) {
          s.disabled = false;
          if (!patchFailed(e, id)) setMsg(drawerMsgHost(), e.message, "bad");
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
          const got = await api("PATCH", "/api/tasks/" + id, { title: input.value });
          state.byId.set(id, got);
          setMsg(drawerMsgHost(), "标题已保存", "ok");
          done(got.title == null ? "" : got.title);
        } catch (e) {
          s.disabled = false;
          if (!patchFailed(e, id)) setMsg(drawerMsgHost(), e.message, "bad");
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

  const resSlot = section(body, "结果");
  if (t.error) {
    resSlot.append(el("div", "kv-value is-bad", t.error));
  } else if (t.summary) {
    resSlot.append(el("div", "body-text", t.summary));
  } else {
    resSlot.append(el("p", "field-empty",
      t.status === "done" ? "完成判定通过，没有额外的文字说明。" : "还没有结果。"));
  }

  /* --- 时间线 ------------------------------------------------------------ */
  const tlSlot = section(body, "时间线");
  const evs = taskEvents(id);
  if (!evs.length) {
    tlSlot.append(el("p", "tl-empty",
      "这一页只显示打开之后收到的进展。要完整历史请在任务运行时打开。"));
  } else {
    const ul = el("ul", "timeline");
    evs.forEach(e => ul.append(eventLine(e)));
    tlSlot.append(ul);
  }

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
      const got = await api("POST", "/api/tasks/" + t.id + "/abort", {});
      state.byId.set(t.id, got);
      setMsg(drawerMsgHost(), "已请求中止", "warn");
      stopStream(t.id);
      await load().catch(() => {});
      const nt = state.byId.get(t.id);
      if (nt && state.openId === t.id) drawDrawer(t.id, nt);
    } catch (e) {
      b.disabled = false;
      setMsg(drawerMsgHost(), e.message, "bad");
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
      setMsg(drawerMsgHost(), "已开始下载 " + a.download, "ok");
    } catch (e) {
      setMsg(drawerMsgHost(), e.message, "bad");
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
      setMsg(drawerMsgHost(), e.message, "bad");
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
  state.streams.forEach((ctl, id) => stopStream(id));
  state.streams.clear();
  state.items = [];
  state.byId.clear();
  state.cards.forEach(n => n.remove());
  state.cards.clear();
  state.cols.forEach(c => { c.list.textContent = ""; c.count.textContent = "0"; });
  closeDrawer();
  document.getElementById("who").textContent = "";
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

// ---------------------------------------------------------------- 提交表单

function openComposer() {
  document.getElementById("c-title").value = "";
  document.getElementById("c-brief").value = "";
  const dlg = document.getElementById("composer");
  dlg.showModal();
  document.getElementById("c-title").focus();
}

document.getElementById("c-cancel").onclick = () => document.getElementById("composer").close();

document.getElementById("composer-form").addEventListener("submit", async e => {
  e.preventDefault();
  const save = document.getElementById("c-save");
  save.disabled = true;
  try {
    const got = await api("POST", "/api/tasks", {
      title: document.getElementById("c-title").value.trim(),
      brief: document.getElementById("c-brief").value.trim(),
    });
    document.getElementById("composer").close();
    await load();
    openDrawer(got.id);
  } catch (err) {
    if (err && err.status === 401) signedOut("登录已过期，请重新登录");
    else setMsg(document.getElementById("board-msg"), err.message, "bad");
  } finally {
    save.disabled = false;
  }
});

// ---------------------------------------------------------------- 工具条

let qTimer = null;
document.getElementById("q").oninput = e => {
  // 纯前端筛选：GET /api/tasks 没有声明 q 参数（服务端只按 owner 过滤），
  // 拿关键词去请求后端只会白跑一趟，而且搜不搜得到全看后端怎么实现。
  state.filter.q = e.target.value.trim();
  clearTimeout(qTimer);
  qTimer = setTimeout(render, 120);         // 连续输入时合并重排，别每敲一下就重画整板
};
document.getElementById("status-filter").onchange = e => { state.filter.status = e.target.value; render(); };
document.getElementById("only-active").onchange = e => { state.filter.activeOnly = e.target.checked; render(); };
document.getElementById("new-btn").onclick = () => openComposer();
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
  if (!token()) return;
  if (document.visibilityState !== "visible") return;
  reload();
}, 10000);

// 计划里说这批名字暴露给控制台/端到端调试。顶层 const 不会挂到 window 上，
// 显式挂一份，控制台敲 window.state 才拿得到。
Object.assign(window, {
  api, state, el, render, load, reload, filtered, fmtTime,
  STATUS_ORDER, STATUS_LABEL, COLUMNS, buildRail, paintRail, slideRail,
  railIndex, openDrawer, closeDrawer, setDrawerFull, confirmAction,
  taskEvents, eventLine, FIELD_CN, KIND_LABEL, ACTOR_LABEL, apiBase,
});

async function boot() {
  buildCols();
  renderStatusFilter();
  if (!token()) return showLogin();
  try {
    state.me = await api("GET", "/api/me");
    const who = state.me || {};
    document.getElementById("who").textContent =
      (who.username || "") + (who.role === "admin" ? "（管理员）" : "");
    await load();
  } catch (e) {
    if (e && e.status === 401) signedOut("登录已过期，请重新登录");
    else setMsg(document.getElementById("board-msg"), (e && e.message) || "连不上服务", "bad");
  }
}

boot();
