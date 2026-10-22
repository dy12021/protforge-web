/* 自动化计算平台 · 值班台前端
 *
 * 与 web/app.js 同一套模式：零外链零构建、鉴权走 Authorization: Bearer、
 * detail 恒为中文字符串。四条硬口径：
 *
 *   1. 界面口径统一「自动化计算平台」「计算任务」「计算进程」。
 *      tests/test_admin_static.py 会把违规字眼连注释一起扫出来。
 *   2. 危险动作一律两步确认，且确认表**列出会丢什么**。
 *   3. 干预（steer / follow_up / set_model / set_thinking_level）与中止
 *      都转发给面板后端，再由它转发到主服务的内部端口——前端不直接连 8475。
 *   4. 实时流用 fetch + ReadableStream：EventSource 发不了 Authorization 头。
 */

/* ---------------------------------------------------------------- 状态机
 *
 * 流转轨只画四站主链，failed 是终点不占一站。失败时只把当前站染红，
 * 线不整条涂红——整条变红会丢掉"走到哪一步"。 */
const STATUS_ORDER = ["queued", "running", "packaging", "done"];
const STATUS_LABEL = {
  queued: "已排队", running: "计算中", packaging: "打包中",
  done: "已完成", failed: "失败",
};
/* 还在跑的三态：值班台第一屏盯的就是这三类。 */
const ACTIVE = new Set(["queued", "running", "packaging"]);
const ACTIVE_STATUSES = new Set(ACTIVE);

/* 显示层翻译。服务端 message 是 API 契约不能改，裸字段名的翻译放这里。 */
const KIND_LABEL = {
  bash: "命令调用", bash_denied: "守卫拦截", tool: "技能调用",
  admin_rpc: "管理员干预",
  login: "登录", admin: "管理员操作", spawn: "启动进程", abort: "中止",
  rpc: "干预命令",
};
const ACTOR_LABEL = {
  human: "提交人", agent: "计算进程", admin: "管理员", system: "系统",
};
const ROLE_LABEL = { user: "普通用户", admin: "管理员" };
/* 产物来源（spec §8.2 两条路径）。 */
const SOURCE_LABEL = { prefix: "前缀路径", timewindow: "时间窗路径" };
/* 干预命令的中文说明。按钮与确认表共用，读法只有一处。 */
const CMD_LABEL = {
  steer: "插话", follow_up: "追加要求", abort: "中止",
  set_model: "切换模型", set_thinking_level: "切换思考档位",
};

/* 任务范围（GET /api/admin/tasks?scope=）。
 *
 * 默认「常规」是**服务端的设计**：值班台是「现在有什么不对」的地方，
 * 几百个正常跑的行任务会把排查视图糊满。但行任务恰恰是管理员来这儿
 * 要查的东西，所以前端必须有开关——不然这个默认值等于把行任务彻底藏起来了，
 * 而管理员排查卡住的行任务正是这个功能对他的全部价值。 */
const SCOPES = [
  { value: "regular", label: "常规", hint: "只看常规计算任务（不含工作流行任务）" },
  { value: "workflow", label: "工作流", hint: "只看工作流里的行任务" },
  { value: "all", label: "全部", hint: "常规与工作流行任务一起看" },
];
const SCOPE_LABEL = { regular: "常规", workflow: "工作流", all: "全部" };

const state = {
  tasks: [],                 // GET /api/admin/tasks 的快照（最新在前）
  byId: new Map(),           // id -> task
  users: [],                 // 全部账号
  audit: [],                 // 当前这一批审计行（过滤与导出都在前端做）
  auditAll: [],              // 未过滤的原始行（导出的必须是这个，过滤会让人以为只有那么多）
  streams: new Map(),        // taskId -> { stop, ac, lastId }
  cards: new Map(),          // taskId -> 卡片元素（复用，重建就没有滑轨动画）
  reached: new Map(),        // taskId -> 走到的站序；failed 时用它把红点放对位置
  pending: new Map(),        // artifactId -> 产物行（确认/排除后回填状态）
  scope: "regular",         // GET /api/admin/tasks 的 scope：常规/工作流/全部
  counts: {},               // 同上，按当前范围统计的各状态计数
  filter: { q: "", status: "", activeOnly: false },
  auditFilter: { task: "", kind: "", q: "", failOnly: false },
                           // 轮询会整块重画列表，存在元素上的开合状态活不过一轮
                                // 轮询会整块重画列表：每次都新建输入框，管理员刚敲的
                                // 路径就被换掉，点按钮只剩「先填要改哪个文件」
  tab: "live",
  me: null,
  clockTimer: null,
};

const TOKEN_KEY = "protforge.admin.token";

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
function setToken(v) { if (v) localStorage.setItem(TOKEN_KEY, v); else localStorage.removeItem(TOKEN_KEY); }

/* base 走同源。面板只在本机开，与后端同源；开发时用 ?base= 指到别处。 */
function apiBase() {
  const q = new URLSearchParams(location.search).get("base");
  const b = q || window.PROTFORGE_ADMIN_BASE || "";
  return (b ? b.replace(/\/+$/, "") : "") + "/api/admin";
}

function authHeaders(lastEventId) {
  const h = {};
  const t = token();
  if (t) h["Authorization"] = "Bearer " + t;
  if (lastEventId != null) h["Last-Event-ID"] = String(lastEventId);
  return h;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// detail 恒为中文字符串（含 pydantic 框架层错误）。数组透出去前端拿到
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
    e.status = r.status;
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

/* 耗时：毫秒。跑了 3 小时的任务和跑了 40 毫秒的命令要能一眼分开。 */
function fmtMs(ms) {
  if (ms === null || ms === undefined || ms === "") return "";
  const n = Number(ms);
  if (!Number.isFinite(n)) return String(ms);
  if (n < 1000) return n + " ms";
  if (n < 60000) return (n / 1000).toFixed(1) + " s";
  if (n < 3600000) return Math.round(n / 60000) + " min";
  return (n / 3600000).toFixed(1) + " h";
}

function fmtSize(bytes) {
  // n 必须用 let：下面那个循环要靠它自己递减值。
  // 写成 const 时，任何一条**大小非零**的待确认产物都会在这里抛
  // "Assignment to constant variable."——而 pendRow 是 load() 里的一环，
  // 于是整个 boot() 半路夭折：用户列表不拉、任务表不画，只留下一条错误提示。
  // 症状与真因隔了三层，全靠翻代码是翻不出来的。
  let n = Number(bytes || 0);
  if (!Number.isFinite(n) || n <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
}

/* 审计里的命令行可能极长（循环、轮询、grep 日志），渲染前截断，
 * 但**导出时给全文**——追责要的是原文，不是能上屏的那部分。 */
const CMD_VIEW_MAX = 240;
function cmdView(cmd) {
  const s = String(cmd || "");
  return s.length > CMD_VIEW_MAX ? s.slice(0, CMD_VIEW_MAX) + " …（完整内容见导出）" : s;
}

function setMsg(host, text, kind) {
  host.textContent = "";
  host.append(el("span", null, text || ""));
  host.className = "board-msg" + (kind ? " is-" + kind : "");
  host.hidden = false;
}
function hideMsg(host) { host.hidden = true; host.textContent = ""; }

function setRowMsg(text, kind) {
  const host = document.getElementById("users-msg");
  host.textContent = text || "";
  host.className = "row-msg" + (kind ? " is-" + kind : "");
  host.hidden = !text;
}

function statusChip(key) {
  return el("span", "chip chip-" + (key || "queued"), STATUS_LABEL[key] || key);
}

// ---------------------------------------------------------------- 流转轨

/* 失败任务的"当前站"在哪一站？主链上 status 已经变成 failed，得自己记。 */
function railIndex(id, status, step) {
  const i = STATUS_ORDER.indexOf(status);
  if (i >= 0) return i;
  const seen = state.reached.get(Number(id));
  if (seen != null) return seen;
  return step ? 1 : 0;
}

/* 建轨。**每一站都必须有圆点**——缺了站就只剩一根线，读不出卡在第几站。 */
function buildRail(status, failed, id, step) {
  const rail = el("div", "rail");
  paintRail(rail, status, failed, id, step);
  return rail;
}

function paintRail(rail, status, failed, id, step) {
  const idx = railIndex(id, status, step);
  rail.textContent = "";
  rail.dataset.idx = String(idx);       // slideRail 据此拿到"从哪一站来"
  STATUS_ORDER.forEach((st, i) => {
    const seg = el("div", "rail-seg");
    const dot = el("i", "rail-dot");
    if (i < idx) {
      seg.className = "rail-seg past";
      seg.style.setProperty("--seg", "var(--st-" + st + ")");
      seg.style.setProperty("--dot", "var(--st-" + st + ")");
    } else if (i === idx) {
      // 当前站大一号 + 白环（把点从线上抬起来）。**失败时只染当前站**。
      seg.className = "rail-seg now";
      seg.style.setProperty("--seg", failed ? "var(--st-failed)" : "var(--st-" + st + ")");
      seg.style.setProperty("--dot", failed ? "var(--st-failed)" : "var(--st-" + st + ")");
    } else {
      seg.className = "rail-seg future";
    }
    seg.append(dot);
    seg.title = STATUS_LABEL[st];
    rail.append(seg);
  });
}

/* 动效只做这一处（SPEC 20.8）：点从旧态滑到新态。
 *
 * 先量后改：滑轨要能从"旧位置"开始，所以旧坐标必须在重绘之前取。
 * 量不到（首帧、prefers-reduced-motion）就只重绘，不做动画。 */
function slideRail(rail, toStatus, failed, id, step) {
  const to = railIndex(id, toStatus, step);
  const segs = rail.querySelectorAll(".rail-seg");
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || !segs.length) {
    paintRail(rail, toStatus, failed, id, step);
    return;
  }
  const from = rail.classList.contains("now") ? rail.dataset.idx : null;
  paintRail(rail, toStatus, failed, id, step);
  if (from === null || from === to) return;
  const delta = (Number(from) - to) * 26;      // 一站的像素宽（见 CSS 轨宽）
  const dot = segs[to] && segs[to].querySelector(".rail-dot");
  if (!dot) return;
  dot.animate(
    [{ transform: "translateX(" + delta + "px) scale(1.45)" },
     { transform: "translateX(0) scale(1)" }],
    { duration: 300, easing: "cubic-bezier(.2,.7,.3,1)" }
  );
}

// ---------------------------------------------------------------- 1 · 在跑的

/* 运行中任务的实时卡片：轨 + 当前步骤 + 干预框 + 中止。
 * 卡片复用（不每次重建）：重建就没有滑轨动画，输入框里正在敲的字也会没。 */
function liveCard(t) {
  const card = el("div", "live-card");
  card.dataset.id = String(t.id);

  const head = el("div", "lc-head");
  head.append(el("span", "lc-id", "#" + t.id), el("span", "lc-title", t.title || ""));
  head.append(el("span", "lc-time", fmtTime(t.created_at)));
  card.append(head, buildRail(t.status, t.status === "failed", t.id, t.step));

  const step = el("div", "card-step");
  step.append(el("span", "card-step-k", "当前"), el("span", "card-step-t", t.step || "（还没报步骤）"));
  step.hidden = !t.step;
  card.append(step);

  const fail = el("div", "card-fail");
  fail.append(el("span", "card-fail-k", "失败"), el("span", "card-fail-t", t.error || ""));
  fail.hidden = !t.error;
  card.append(fail);

  // 干预框：默认收起（值班台第一眼是"在干什么"）。steer 与 follow_up 分开两个
  // 按钮，因为发错了等于没发——插话晚了 agent 已经跑过去了。
  const acts = el("div", "lc-actions");
  const openIv = el("button", "btn", "干预");
  openIv.type = "button";
  const intervene = buildIntervene(t);
  openIv.onclick = () => {
    const opening = intervene.hidden;
    intervene.hidden = !opening;
    openIv.textContent = opening ? "收起干预" : "干预";
    if (opening) {
      const ta = intervene.querySelector("textarea");
      if (ta) ta.focus();
    }
  };
  const abort = el("button", "btn btn-warn", "中止");
  abort.type = "button";
  abort.onclick = () => doAbort(t);
  acts.append(openIv, abort);
  card.append(acts, intervene);

  state.cards.set(Number(t.id), card);
  return card;
}

/* 干预区：一个框两个发法 + 模型/思考档位。
 * 关掉时不发请求，只是把 hidden 切回去——不存在"取消了一半写进账本"。 */
function buildIntervene(t) {
  const box = el("div", "intervene");
  box.hidden = true;
  box.append(el("span", "iv-hint",
    "插话会立刻进入当前这一轮的后续步骤；追加要求排队，等本轮结束再读。"));

  const ta = el("textarea", "iv-text");
  ta.rows = 2;
  ta.placeholder = "例如：改用 foldseek 复核一下，或者先停下等我确认参数";
  box.append(ta);

  const row = el("div", "iv-row");
  const steer = el("button", "btn", "插话");
  const follow = el("button", "btn", "追加要求");
  const note = el("span", "iv-note", "");
  steer.type = follow.type = "button";
  steer.onclick = () => sendRpc(t, "steer", ta, note);
  follow.onclick = () => sendRpc(t, "follow_up", ta, note);
  row.append(steer, follow, note);
  box.append(row);

  // 模型与思考档位：两个小下拉 + 一个发送。改档位是值班台的低频动作，
  // 不值得给它一个常驻输入框。
  const row2 = el("div", "iv-row");
  const model = el("select", "btn");
  model.setAttribute("aria-label", "模型");
  model.append(new Option("沿用当前模型", ""));
  ["claude-sonnet-4", "claude-opus-4", "qwen3-max"].forEach(m =>
    model.append(new Option(m, m)));
  const think = el("select", "btn");
  think.setAttribute("aria-label", "思考档位");
  think.append(new Option("沿用当前档位", ""));
  ["off", "low", "medium", "high"].forEach(m => think.append(new Option(m, m)));
  const sendModel = el("button", "btn", "切换模型");
  const sendThink = el("button", "btn", "切换档位");
  sendModel.type = sendThink.type = "button";
  sendModel.onclick = () => sendRpc(t, "set_model", null, note, { model: model.value });
  sendThink.onclick = () => sendRpc(t, "set_thinking_level", null, note, { thinking_level: think.value });
  row2.append(model, sendModel, think, sendThink);
  box.append(row2);
  return box;
}

function renderLive() {
  const list = document.getElementById("live-list");
  const empty = document.getElementById("live-empty");
  const live = state.tasks.filter(t => ACTIVE_STATUSES.has(t.status));
  const wanted = new Set(live.map(t => Number(t.id)));

  // 先摘掉已经不在跑/被筛掉的卡片，再补新的。
  state.cards.forEach((node, id) => {
    if (!wanted.has(id)) { node.remove(); state.cards.delete(id); }
  });
  // 复用已有卡片（不重建：重建会把正在敲的干预内容和滑轨动画一起扔掉），
  // 没有的才新建。新建的 liveCard() 自己就画好了，不要再 paintLiveCard 一遍——
  // 那会让新卡片的轨刚建好就滑一次。
  live.forEach(t => {
    const node = state.cards.get(Number(t.id));
    if (node) { paintLiveCard(node, t); list.append(node); }
    else list.append(liveCard(t));
  });

  empty.hidden = live.length > 0;
  if (!live.length) fillEmpty(empty, "live");
  // 队列与失败数**跟着 scope 走**（服务端就这么算的），所以这里必须把
  // 当前视图写出来——不然「常规视图 0 个在跑」会被读成「全平台 0 个在跑」。
  const q = (state.queue || {}).queued || 0;
  const failed = (state.counts || {}).failed || 0;
  document.getElementById("live-sub").textContent = scopeName() + "视图 · "
    + live.length + " 个在跑 · 队列 " + q + " 个待启动"
    + (failed ? " · 失败 " + failed : "");
}

/* 就地重画一张卡。不重建节点：重建会把正在敲的干预内容和滑轨动画一起扔掉。 */
function paintLiveCard(card, t) {
  if (!card) return;
  card.classList.toggle("is-failed", t.status === "failed");
  const oldRail = card.querySelector(".rail");
  if (oldRail) slideRail(oldRail, t.status, t.status === "failed", t.id, t.step);
  const step = card.querySelector(".card-step");
  if (step) {
    step.hidden = !t.step;
    step.querySelector(".card-step-t").textContent = t.step || "（还没报步骤）";
  }
  const fail = card.querySelector(".card-fail");
  if (fail) {
    fail.hidden = !t.error;
    fail.querySelector(".card-fail-t").textContent = t.error || "";
  }
}

// --------------------------------------------------- 4 · 时间窗产物确认区

/* 时间窗产物**可见但不可下载**，要在这里转正（spec §8.2）。
 *
 * 判断归属的依据只有两个，都必须原样显示：完整路径与 mtime。所以这一块
 * **不截断路径**——截了就正好把区分两个候选目录的那部分截掉了。
 * 每条两个动作：确认归属（转正，打进结果包）/ 排除（不是它的）。
 * 排除是不可逆的判断，所以走两步确认并列出后果。 */
function renderPending() {
  const host = document.getElementById("pending-list");
  const empty = document.getElementById("pending-empty");
  host.textContent = "";

  const groups = new Map();          // task_id -> [产物行]
  state.pending.forEach(row => {
    const k = Number(row.task_id);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(row);
  });

  groups.forEach((rows, taskId) => {
    const t = state.byId.get(taskId);
    const g = el("div", "pending-group");
    const head = el("div", "pg-head");
    head.append(el("b", null, t ? (t.title || "") : ("#" + taskId)));
    head.append(el("span", null, "#" + taskId + " · " + (t ? STATUS_LABEL[t.status] || t.status : "")));
    head.append(el("span", null, rows.length + " 项待确认"));
    g.append(head);
    rows.forEach(row => g.append(pendRow(row, t)));
    host.append(g);
  });

  const total = state.pending.size;
  empty.hidden = total > 0;
  document.getElementById("pending-sub").textContent =
    total ? (total + " 项待确认 · 确认后才会被打进结果包") : "";
  const badge = document.getElementById("badge-pending");
  badge.textContent = String(total);
  badge.hidden = total === 0;
}

function pendRow(row, t) {
  const box = el("div", "pend");
  if (row.__done) box.classList.add("is-done");
  if (row.__gone) box.classList.add("is-gone");

  const top = el("div", "pend-top");
  top.append(el("span", "pend-name", row.name || "(无名)"));
  if (t) top.append(el("span", "pend-task", "属于「" + (t.title || "") + "」"));
  top.append(el("span", "pend-size", fmtSize(row.size)));
  box.append(top);

  const path = el("div", "pend-path");
  path.append(document.createTextNode("路径 "));
  path.append(el("b", null, row.path || ""));
  box.append(path);

  const why = el("div", "pend-why");
  why.textContent = "来源：" + (SOURCE_LABEL[row.source] || row.source) +
    " · 登记于 " + fmtTime(row.created_at) +
    " · 尚未确认，**不进结果包、用户下载不到**";
  box.append(why);

  const acts = el("div", "pend-acts");
  if (row.__done) {
    acts.append(el("span", "iv-note", "已确认归属，会打进结果包"));
  } else if (row.__gone) {
    acts.append(el("span", "iv-note", "已排除，不进结果包"));
  } else {
    const ok = el("button", "btn btn-ok", "确认归属");
    ok.type = "button";
    ok.onclick = () => doVerify(row, true);
    const no = el("button", "btn", "排除");
    no.type = "button";
    no.onclick = () => doVerify(row, false);
    acts.append(ok, no);
  }
  box.append(acts);
  return box;
}

async function doVerify(row, ok) {
  // 排除会永久改变这条产物的归属判断，且没有任何撤销路径，所以两步确认。
  // 确认归属同样要确认：它会把**整个目录**（可能几十 GB）打进用户的结果包。
  const spec = ok
    ? {
        title: "确认「" + (row.name || "") + "」属于这个计算任务？",
        lead: "确认之后这条产物会立刻转正，后果是：",
        items: [
          "该目录会被打进用户下载的结果包（当前大小 " + (fmtSize(row.size) || "未知") + "）",
          "用户从此刻起就能下载到它",
          "「" + (row.name || "") + "」会从待确认区消失",
        ],
        okLabel: "确认归属",
      }
    : {
        title: "排除「" + (row.name || "") + "」？",
        lead: "排除之后这条产物不会进任何结果包。会失去的是：",
        items: [
          "该目录里的全部计算结果（" + (fmtSize(row.size) || "大小未知") + "）",
          "算力已经花掉了，但用户永远拿不到这部分产物",
          "排除没有撤销入口——判错了只能重新提交一次计算任务",
        ],
        okLabel: "确认排除",
      };
  const yes = await confirmAction(spec);
  if (!yes) return;
  try {
    const got = await api("POST", "/artifacts/" + row.id + "/verify",
                          { task_id: Number(row.task_id), ok: !!ok });
    row.verified = got.artifact.verified;
    row.__done = !!ok;
    row.__gone = !ok;
    if (ok) state.pending.delete(Number(row.id));
    renderPending();
    setMsg(document.getElementById("board-msg"),
      (ok ? "已确认归属：" : "已排除：") + (row.name || ""), ok ? "ok" : "warn");
  } catch (e) {
    setMsg(document.getElementById("board-msg"), e.message || "操作失败", "bad");
  }
}

// ---------------------------------------------------------------- 3 · 任务表

function filteredTasks() {
  const q = state.filter.q.toLowerCase();
  return state.tasks.filter(t => {
    if (state.filter.activeOnly && !ACTIVE_STATUSES.has(t.status)) return false;
    if (state.filter.status && t.status !== state.filter.status) return false;
    if (!q) return true;
    return ((t.title || "") + " " + (t.brief || "") + " " + (t.step || ""))
      .toLowerCase().includes(q);
  });
}

function renderTasks() {
  const body = document.getElementById("task-rows");
  const rows = filteredTasks();
  body.textContent = "";
  rows.forEach(t => body.append(taskRow(t)));
  const empty = document.getElementById("tasks-empty");
  empty.hidden = rows.length > 0;
  if (!rows.length) {
    // 有任务却筛没了，与这个视图本来就没有，是两回事。
    fillEmpty(empty, state.tasks.length ? "filter" : "scope");
  }
  document.getElementById("tasks-sub").textContent = scopeName() + "视图 · "
    + rows.length + " / " + state.tasks.length + " 个计算任务";
}

function taskRow(t) {
  const tr = el("tr");
  tr.append(el("td", "c-id", "#" + t.id));
  const title = el("td", "c-title");
  title.append(el("div", null, t.title || ""));
  if ((t.pending_artifacts || []).length) {
    title.append(el("div", "iv-note",
      t.pending_artifacts.length + " 项时间窗产物待确认"));
  }
  tr.append(title);
  const st = el("td", "c-st");
  st.append(statusChip(t.status));
  tr.append(st);
  tr.append(el("td", "c-step", t.step || "—"));
  tr.append(el("td", "c-owner", ownerName(t.owner_id)));
  tr.append(el("td", "c-time", fmtTime(t.created_at)));
  // 操作列装的是**元素**，不能走 el(tag, cls, text)：
  // 那个 text 参数会进 textContent，于是整列被字面渲染成
  // "[object HTMLDivElement]"，三个按钮一个都按不到。
  const act = el("td", "c-act");
  act.append(taskActs(t));
  tr.append(act);
  return tr;
}

function ownerName(id) {
  const u = state.users.find(x => Number(x.id) === Number(id));
  return u ? u.username : ("#" + id);
}

/* 危险动作（中止 / 改状态 / 重跑）一律两步确认，与用户端的删除同理。 */
function taskActs(t) {
  const box = el("div", "row-acts");
  const live = ACTIVE_STATUSES.has(t.status);

  const abort = el("button", "btn btn-warn", "中止");
  abort.type = "button";
  abort.disabled = !live;
  abort.onclick = () => doAbort(t);
  box.append(abort);

  const st = el("button", "btn", "改状态");
  st.type = "button";
  st.onclick = () => doStatus(t);
  box.append(st);

  const retry = el("button", "btn", "重跑");
  retry.type = "button";
  retry.disabled = live;
  retry.onclick = () => doRetry(t);
  box.append(retry);
  return box;
}

async function doAbort(t) {
  const yes = await confirmAction({
    title: "中止计算任务 #" + t.id + "？",
    lead: "中止之后会失去：",
    items: [
      "正在跑的技能链会被终止，" + (t.step ? ("当前在做：" + t.step) : "当前步骤未知"),
      "已经算出的中间结果留在磁盘上，但不会被打包",
      "不能续跑，只能重新提交一次（重跑会从头再算）",
    ],
    okLabel: "确认中止",
  });
  if (!yes) return;
  try {
    const got = await api("POST", "/tasks/" + t.id + "/abort", {});
    stopStream(t.id);
    await load();
    // 内部通道不通时中止照样落地（状态已是失败），但进程树可能还在——
    // 这条必须说给值班的人听，否则他会以为 GPU 已经放出来了。
    setMsg(document.getElementById("board-msg"),
      got.warning ? ("已置为失败，但计算进程可能仍在运行：" + got.warning)
                  : "已中止计算任务 #" + t.id,
      got.warning ? "warn" : "ok");
  } catch (e) {
    setMsg(document.getElementById("board-msg"), e.message || "中止失败", "bad");
  }
}

async function doStatus(t) {
  // 改状态要带一个下拉，所以自拼表体 + 用 read() 把选中值带出来。
  const sel = el("select");
  sel.style.width = "100%";
  ["queued", "running", "packaging", "done", "failed"].forEach(k =>
    sel.append(new Option(STATUS_LABEL[k] + "（" + k + "）", k)));
  sel.value = t.status;

  const body = el("div");
  body.append(el("h2", "confirm-title", "强制改状态：" + (t.title || ("#" + t.id))));
  body.append(el("p", "confirm-lead",
    "这是绕过全部自动判断的直接写库。选一个目标状态后会失去的是："));
  body.append(sel);
  const list = el("ul", "confirm-list");
  ["技能链不会因为状态变化而停下或启动",
   "调度器下一次 tick 会按新状态重新决定要不要接这个任务",
   "任务时间线上会留下一条「管理员改写了状态」的记录，用户看得到"].forEach(s =>
    list.append(el("li", null, s)));
  body.append(list);

  const chosen = await confirmBody(body, "确认改状态", () => sel.value);
  if (!chosen) return;
  try {
    await api("POST", "/tasks/" + t.id + "/status",
              { status: chosen, note: "值班台强制改状态" });
    await load();
    setMsg(document.getElementById("board-msg"),
      "已把 #" + t.id + " 改成 " + STATUS_LABEL[chosen] + "（" + chosen + "）", "ok");
  } catch (e) {
    setMsg(document.getElementById("board-msg"), e.message || "改状态失败", "bad");
  }
}

async function doRetry(t) {
  const pend = (t.pending_artifacts || []).length;
  const yes = await confirmAction({
    title: "重跑计算任务 #" + t.id + "？",
    lead: "重跑会把这个任务放回队列。会失去的是：",
    items: [
      "上一次的失败原因与步骤会被清空",
      "上一轮的结果包入口会被清掉（磁盘上的中间文件还留着）",
      "会再占一次 GPU 与队列位置，" + (pend ? ("并且当前还有 " + pend + " 项产物没确认归属") : ""),
    ],
    okLabel: "确认重跑",
  });
  if (!yes) return;
  try {
    await api("POST", "/tasks/" + t.id + "/retry", {});
    await load();
    setMsg(document.getElementById("board-msg"), "已把 #" + t.id + " 放回队列", "ok");
  } catch (e) {
    setMsg(document.getElementById("board-msg"), e.message || "重跑失败", "bad");
  }
}

// ---------------------------------------------------------------- 3 · 审计

function auditKindLabel(kind) { return KIND_LABEL[kind] || kind || "—"; }

/* 过滤在**前端**做：一次拉 1000 行（store.audit.list 默认 200、这里给足），
 * 按任务/类型/关键词/非零退出筛。导出的是未过滤的原始行——追责要全量。 */
function auditFiltered() {
  const q = state.auditFilter.q.toLowerCase();
  return state.auditAll.filter(a => {
    if (state.auditFilter.task && Number(a.task_id) !== Number(state.auditFilter.task)) return false;
    if (state.auditFilter.kind && a.kind !== state.auditFilter.kind) return false;
    if (state.auditFilter.failOnly) {
      const code = a.exit_code;
      if (code === null || code === undefined || Number(code) === 0) return false;
    }
    if (!q) return true;
    return String(a.command || "").toLowerCase().includes(q);
  });
}

function renderAudit() {
  const body = document.getElementById("audit-rows");
  const rows = auditFiltered();
  body.textContent = "";
  rows.slice(0, 500).forEach(a => body.append(auditRow(a)));
  document.getElementById("audit-empty").hidden = rows.length > 0;
  document.getElementById("audit-sub").textContent =
    rows.length + " / " + state.auditAll.length + " 条 · 显示前 " + Math.min(500, rows.length) + " 条";
}

function auditRow(a) {
  const tr = el("tr");
  tr.append(el("td", "c-id", "#" + a.id));
  tr.append(el("td", "c-time", fmtTime(a.created_at)));
  const task = el("td", "c-task");
  task.append(el("span", null, a.task_id ? "#" + a.task_id : "—"));
  tr.append(task);
  tr.append(el("td", "c-kind", auditKindLabel(a.kind)));
  // 命令原文：**不折行**。折了就看不出有没有 rm、有没有拼错路径。
  tr.append(el("td", "c-cmd", cmdView(a.command)));
  const code = a.exit_code;
  const bad = code !== null && code !== undefined && Number(code) !== 0;
  const td = el("td", "c-code" + (bad ? " is-bad" : ""),
    code === null || code === undefined ? "" : String(code));
  tr.append(td);
  tr.append(el("td", "c-ms", fmtMs(a.elapsed_ms)));
  return tr;
}

/* 导出 CSV。**给未过滤的原始行与命令全文**——事后追责要的是"它到底跑了什么"，
 * 过滤后的 20 条拿去交差等于把证据裁掉。加 BOM：Excel 不认 UTF-8 无 BOM 的中文。 */
function exportAuditCsv() {
  const rows = state.auditAll;
  if (!rows.length) {
    setMsg(document.getElementById("board-msg"), "还没有审计记录可导出", "warn");
    return;
  }
  const head = ["序号", "时间", "任务", "类型", "操作人", "命令原文", "退出码", "耗时毫秒"];
  const esc = v => {
    const s = String(v === null || v === undefined ? "" : v);
    return /[",\n]/.test(s) ? ('"' + s.replace(/"/g, '""') + '"') : s;
  };
  const lines = [head.join(",")];
  rows.forEach(a => lines.push([
    a.id, a.created_at || "", a.task_id || "", a.kind || "",
    ACTOR_LABEL[a.actor] || a.actor || "", a.command || "",
    a.exit_code === null || a.exit_code === undefined ? "" : a.exit_code,
    a.elapsed_ms === null || a.elapsed_ms === undefined ? "" : a.elapsed_ms,
  ].map(esc).join(",")));
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = el("a");
  a.href = url;
  a.download = "audit-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "") + ".csv";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 20000);
  setMsg(document.getElementById("board-msg"),
    "已导出 " + rows.length + " 条审计记录（未经筛选，含命令全文）", "ok");
}

async function loadAudit() {
  const taskId = state.auditFilter.task;
  const got = await api("GET", taskId
    ? "/tasks/" + taskId + "/audit?limit=1000"
    : "/audit?limit=1000");
  state.auditAll = got.audit || [];
  renderAudit();
}

// ---------------------------------------------------------------- 5 · 用户

function renderUsers() {
  const body = document.getElementById("user-rows");
  body.textContent = "";
  state.users.forEach(u => body.append(userRow(u)));
  document.getElementById("users-sub").textContent =
    state.users.length + " 个账号 · " +
    state.users.filter(u => u.role === "admin" && !u.disabled).length + " 个可用管理员";
}

function userRow(u) {
  const tr = el("tr");
  tr.append(el("td", "c-id", "#" + u.id));
  tr.append(el("td", "c-name", u.username));

  const role = el("td", "c-role");
  const sel = el("select");
  sel.append(new Option(ROLE_LABEL.user, "user"), new Option(ROLE_LABEL.admin, "admin"));
  sel.value = u.role;
  sel.disabled = Number(u.id) === Number(state.me && state.me.id);
  sel.onchange = async () => {
    if (sel.value === u.role) return;
    const yes = await confirmAction({
      title: "把「" + u.username + "」改成" + ROLE_LABEL[sel.value] + "？",
      lead: "角色变化之后：",
      items: sel.value === "admin"
        ? ["这个人从此可以打开本机值班台，中止任何人的计算任务",
           "可以导出全部审计日志（含所有人的命令原文）",
           "可以建用户、停用他人、重置他人密码"]
        : ["这个人从此不能再打开本机值班台",
           "已经打开的页面会在下一次请求时被拒绝",
           "他自己的计算任务不受影响"],
      okLabel: "确认改角色",
    });
    if (!yes) { sel.value = u.role; return; }
    try {
      await api("PATCH", "/users/" + u.id, { role: sel.value });
      await loadUsers();
      setRowMsg("已把「" + u.username + "」改成" + ROLE_LABEL[sel.value], "ok");
    } catch (e) {
      sel.value = u.role;                    // 退回服务端真值，不留一个假的本地态
      setRowMsg(e.message || "改角色失败", "bad");
    }
  };
  role.append(sel);
  tr.append(role);

  const st = el("td", "c-state");
  st.append(el("span", "chip " + (u.disabled ? "chip-failed" : "chip-done"),
    u.disabled ? "已停用" : "可用"));
  tr.append(st);
  tr.append(el("td", "c-time", fmtTime(u.created_at)));
  // 同上：userActs 返回的是元素，走 text 会被字面渲染成 [object HTML…]
  const act = el("td", "c-act");
  act.append(userActs(u));
  tr.append(act);
  return tr;
}

function userActs(u) {
  const box = el("div", "row-acts");
  const self = Number(u.id) === Number(state.me && state.me.id);

  const toggle = el("button", "btn" + (u.disabled ? "" : " btn-warn"),
    u.disabled ? "启用" : "停用");
  toggle.type = "button";
  toggle.disabled = self;
  toggle.onclick = () => doDisable(u);
  box.append(toggle);

  const reset = el("button", "btn", "重置密码");
  reset.type = "button";
  reset.onclick = () => doResetPassword(u);
  box.append(reset);
  return box;
}

async function doDisable(u) {
  const on = !u.disabled;
  // 停用是不可撤销的（没有"找回"这条路径，用户由管理员创建，无自助注册）。
  const yes = await confirmAction({
    title: (on ? "停用" : "启用") + "账号「" + u.username + "」？",
    lead: on ? "停用之后会失去：" : "启用之后：",
    items: on
      ? ["这个人立刻无法登录，已有会话全部失效",
         "他名下 " + (state.tasks.filter(t => Number(t.owner_id) === Number(u.id)).length) +
           " 个计算任务仍然保留，但不能再提交新的",
         "已经跑完的产物不受影响"]
      : ["这个人可以重新登录，并继续提交计算任务"],
    okLabel: on ? "确认停用" : "确认启用",
  });
  if (!yes) return;
  try {
    await api("PATCH", "/users/" + u.id, { disabled: on });
    await loadUsers();
    setRowMsg("已" + (on ? "停用" : "启用") + "「" + u.username + "」", "ok");
  } catch (e) {
    setRowMsg(e.message || "操作失败", "bad");
  }
}

async function doResetPassword(u) {
  // 重置口令没有找回路径，所以必须**当场把新口令念出来**，否则管理员
  // 重置完就再也进不去了（用户端没有自助找回）。
  const input = el("input");
  input.type = "text";                 // 明文：要能当场抄走，type=password 反而挡住
  input.maxLength = 200;
  input.autocomplete = "off";
  const field = el("label", "field");
  field.append(el("span", null, "新密码（至少 10 位）"));
  field.append(input);

  const body = el("div");
  body.append(el("h2", "confirm-title", "重置「" + u.username + "」的密码"));
  body.append(el("p", "confirm-lead", "重置之后这个账号的所有会话立即失效，且没有找回入口："));
  body.append(field);
  const list = el("ul", "confirm-list");
  ["这个人此刻在线的话会被踢下线",
   "新密码你必须自己抄走传给本人——平台不会发邮件，也没有找回功能",
   "他自己改不了密码，只能再找你重置一次"].forEach(s => list.append(el("li", null, s)));
  body.append(list);

  const pw = await confirmBody(body, "确认重置", () => input.value.trim());
  if (!pw) return;
  try {
    await api("PATCH", "/users/" + u.id, { password: pw });
    // 重置成功把新口令留在**自己的提示条**上：管理员正要抄，别让下一次
    // 点击把它抹掉。（放在确认表里会被下一次 confirmBody 重建掉。）
    setRowMsg("已重置。立刻抄走，只显示这一次：" + pw, "ok");
  } catch (e) {
    setRowMsg(e.message || "重置失败", "bad");
  }
}

// ---------------------------------------------------------------- 干预

/* 发干预命令。前端只打面板自己的 /rpc/{task_id}，由面板转发到内部端口。 */
async function sendRpc(t, command, ta, note, extra) {
  const message = ta ? ta.value.trim() : "";
  if ((command === "steer" || command === "follow_up") && !message) {
    note.textContent = "先写一句要说的";
    if (ta) ta.focus();
    return;
  }
  note.textContent = "发送中…";
  try {
    const body = Object.assign({ command: command, message: message }, extra || {});
    await api("POST", "/rpc/" + t.id, body);
    note.textContent = "已发给计算进程（" + (CMD_LABEL[command] || command) + "）";
    if (ta) ta.value = "";
  } catch (e) {
    const why = e.message || "发送失败";
    note.textContent = why;
    /* 失败还要往顶部长提示条上再写一份。卡片里的 note 活不过这一轮轮询：
     * 任务刚好在这 10 秒里收口，整张卡片连同 note 一起被移除，
     * 管理员点完插话转个身，失败提示就没了——一个干预动作的**结果**消失了，
     * 而不是"没发生过"。这条板级提示不会随卡片移除而消失。 */
    setMsg(document.getElementById("board-msg"),
      "#" + t.id + " 干预失败（" + (CMD_LABEL[command] || command) + "）：" + why, "bad");
  }
}

// ---------------------------------------------------------------- 实时流

/* Bearer 走请求头，EventSource 发不了 Authorization，所以这里用 fetch +
 * ReadableStream 自己读 text/event-stream。断线退避重连，重连带
 * Last-Event-ID 游标。收帧的唯一作用是叫醒刷新——面板的真相在库里。 */
function parseFrame(raw) {
  let data = "";
  raw.split("\n").forEach(line => {
    if (!line || line[0] === ":") return;               // 注释 / 心跳
    const c = line.indexOf(":");
    if (c < 0) return;
    const field = line.slice(0, c);
    const value = line.slice(c + 1).replace(/^ /, "");
    if (field === "data") data += (data ? "\n" : "") + value;
  });
  try { return data ? JSON.parse(data) : null; } catch (e) { return null; }
}

async function readFrames(body, taskId, ctl) {
  const reader = body.getReader();
  const dec = new TextDecoder("utf-8");
  let buf = "";
  for (;;) {
    const r = await reader.read();
    if (r.done) break;
    buf += dec.decode(r.value, { stream: true }).replace(/\r\n/g, "\n");
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = parseFrame(buf.slice(0, i));
      buf = buf.slice(i + 2);
      if (frame && frame.seq && ctl) ctl.lastId = Math.max(ctl.lastId, Number(frame.seq));
    }
    scheduleRefresh(taskId);
  }
}

async function startStream(id) {
  const ctl = { stop: false, ac: new AbortController(), lastId: 0 };
  state.streams.set(id, ctl);
  while (!ctl.stop) {
    try {
      const r = await fetch(apiBase() + "/stream/" + id, {
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
    // 正常断开也要退避：服务端秒断时，不 sleep 就是一条打死服务器的紧循环
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

/* 在跑的任务才值得开流；终态的没有可听的内容。 */
function syncStreams() {
  const live = state.tasks.filter(t => ACTIVE_STATUSES.has(t.status));
  const wanted = new Set(live.map(t => Number(t.id)));
  state.streams.forEach((ctl, id) => { if (!wanted.has(id)) stopStream(id); });
  wanted.forEach(id => { if (!state.streams.has(id)) startStream(id); });
}

const refreshTimers = new Map();
function scheduleRefresh(id) {
  if (refreshTimers.has(id)) return;
  refreshTimers.set(id, setTimeout(async () => {
    refreshTimers.delete(id);
    await load().catch(() => {});
  }, 1200));
}

// ---------------------------------------------------------------- 任务范围

/* 控件挂在工具条末尾。**不能挂进 #tabs**：那里的点击是委托到
 * closest(".tab") 的，混进去点一下就会顺手把页签切成 showTab(undefined)，
 * 五个面板一起消失。选中态用既有的 .primary 上色，因此不必改 style.css。 */
function buildScopeSwitch() {
  const bar = document.querySelector(".toolbar");
  if (!bar) return;              // 工具条没了就不摆控件，页面其余部分照常
  bar.append(el("span", "toolbar-gap"));
  bar.append(el("span", "who", "任务范围"));
  SCOPES.forEach(s => {
    const b = el("button", "secondary", s.label);
    b.type = "button";
    b.dataset.scope = s.value;
    b.title = s.hint;
    b.setAttribute("aria-pressed", String(s.value === state.scope));
    bar.append(b);
  });
  paintScopeSwitch();
}

function paintScopeSwitch() {
  document.querySelectorAll(".toolbar [data-scope]").forEach(b => {
    const on = b.dataset.scope === state.scope;
    b.className = on ? "primary" : "secondary";
    b.setAttribute("aria-pressed", String(on));
  });
}

/* 默认视图**不发** scope：服务端的默认值就是 regular，多发一个与默认相同的
 * 参数只是噪音（前端冒烟是按精确路径核对接口的，多一个 ? 就对不上）。 */
function scopeQuery() {
  return state.scope === "regular"
    ? ""
    : "?scope=" + encodeURIComponent(state.scope);
}

function setScope(v) {
  if (!SCOPES.some(s => s.value === v) || state.scope === v) return;
  state.scope = v;
  paintScopeSwitch();
  load().catch(e => setMsg(document.getElementById("board-msg"),
    e.message || "切换任务范围失败", "bad"));
}

function scopeName() { return SCOPE_LABEL[state.scope] || state.scope; }

/* 空视图不能只写一句「没有任务」。默认视图按设计**不含行任务**，
 * 而管理员来值班台正是要查卡住的行任务——只留一句「没有任务」，
 * 他面对空列表只能猜。所以范围空的时候要把话说全，并给一个一键切换。
 *
 * why='filter' 是另一回事：那是搜索/状态筛选没匹配上，与范围无关。 */
function fillEmpty(node, why) {
  let text, next = null;
  if (why === "filter") {
    text = "没有匹配的计算任务。";
  } else if (state.scope === "all") {
    text = why === "live"
      ? "此刻没有在跑的计算任务。"
      : "一个计算任务都没有。";
  } else if (state.scope === "workflow") {
    text = why === "live"
      ? "此刻没有在跑的工作流行任务。"
      : "本视图只看工作流里的行任务，现在一行都没有。";
    next = "all";
  } else {
    text = why === "live"
      ? "此刻没有在跑的常规任务。本视图不含工作流行任务。"
      : "本视图只看常规计算任务，不含工作流行任务。";
    next = "workflow";
  }
  node.textContent = "";          // 先清干净：复用节点，忘了清就会两句叠在一起
  node.append(el("span", null, text));
  if (next) {
    const b = el("button", "secondary", "切到" + SCOPE_LABEL[next] + "视图");
    b.type = "button";
    b.dataset.scope = next;
    node.append(b);
  }
}

// ---------------------------------------------------------------- 数据

async function load() {
  const got = await api("GET", "/tasks" + (scopeQuery()));
  state.tasks = got.tasks || [];
  state.byId = new Map(state.tasks.map(t => [Number(t.id), t]));
  state.queue = got.queue;
  state.counts = got.counts || {};   // 同样只覆盖当前视图
  // 待确认产物的合并：**已确认/已排除的本地结论不许被下一轮拉取冲掉**，
  // 否则操作完的那一条会立刻又冒回待确认区，看起来像"没生效"。
  const fresh = new Map();
  state.tasks.forEach(t => (t.pending_artifacts || []).forEach(a => fresh.set(Number(a.id), a)));
  state.pending.forEach((row, id) => {
    if (!fresh.has(id)) { row.__done = row.__done || row.verified === 1; fresh.set(id, row); }
  });
  state.pending = fresh;

  renderLive();
  renderPending();
  renderTasks();
  syncStreams();
  syncAuditFilters();
}

async function loadUsers() {
  const got = await api("GET", "/users");
  state.users = got.users || [];
  renderUsers();
  renderTasks();                 // 表格里有"提交人"，改名后要跟着更新
}

/* 下拉选项跟着数据走，但**保留当前选中值**：每 10 秒刷一次就会把值班的人
 * 正在选的下拉重置掉，那比不刷新还糟。 */
function fillSelect(sel, items, current) {
  const want = current !== undefined ? current : sel.value;
  sel.textContent = "";
  items.forEach(it => sel.append(new Option(it.label, it.value)));
  if (items.some(it => it.value === want)) sel.value = want;
}

function syncAuditFilters() {
  fillSelect(document.getElementById("status-filter"),
    [{ value: "", label: "全部状态" }].concat(
      Object.keys(STATUS_LABEL).map(k => ({ value: k, label: STATUS_LABEL[k] }))),
    state.filter.status);
  fillSelect(document.getElementById("audit-task"),
    [{ value: "", label: "全部任务" }].concat(
      state.tasks.map(t => ({ value: String(t.id), label: "#" + t.id + " " + (t.title || "") }))),
    state.auditFilter.task);
  const kinds = [];
  state.auditAll.forEach(a => { if (a.kind && !kinds.includes(a.kind)) kinds.push(a.kind); });
  fillSelect(document.getElementById("audit-kind"),
    [{ value: "", label: "全部类型" }].concat(
      kinds.map(k => ({ value: k, label: auditKindLabel(k) }))),
    state.auditFilter.kind);
}

// ---------------------------------------------------------------- 两步确认

/* 两步确认的**标准入口**（静态文案：中止 / 重跑 / 确认归属 / 排除 / 改角色 /
 * 停用用户）。少数需要带输入框的动作（改状态、重置密码）走 confirmIn。
 *
 * <dialog> 的 close() **不清事件监听**，而 #confirm 是复用节点：第一次确认
 * 留下的监听会让**第二次确认被第一次的闭包直接 resolve 掉**——那就不是
 * 「取消」而是中止错任务。所以收尾统一走 finish()，且**幂等**（answered 守卫）
 * + 解绑监听。监听 close 而不是 cancel：Esc、点「算了」、点确认都走 close，
 * 于是「没点确认就关掉了」一律判成取消，不会出现 Promise 悬着、按钮卡 disabled。 */
function confirmAction(spec) {
  const body = el("div");
  body.append(el("h2", "confirm-title", spec.title || ""));
  body.append(el("p", "confirm-lead", spec.lead || ""));
  const list = el("ul", "confirm-list");
  (spec.items || []).forEach(t => list.append(el("li", null, t)));
  body.append(list);
  return confirmBody(body, spec.okLabel || "确认");
}

/* 唯一的确认表入口。
 *
 * **每次都把 .confirm-inner 整个重建**（而不是往静态节点里填字）：
 * 上一次可能塞过下拉框与输入框，复用静态节点就得记得把它们清干净，
 * 而"忘了清"的症状是上一次的重置密码框留在改状态表里——不报错，只是
 * 看错了值。重建没有这个分支。
 *
 * <dialog> 的 close() **不清事件监听**，而 #confirm 是复用节点：第一次确认
 * 留下的监听会让**第二次确认被第一次的闭包直接 resolve 掉**——那就不是
 * 「取消」而是中止错任务。所以收尾统一走 finish()，且**幂等**（answered 守卫）
 * + 解绑监听。监听 close 而不是 cancel：Esc、点「算了」、点确认都走 close，
 * 于是「没点确认就关掉了」一律判成取消，不会出现 Promise 悬着、按钮卡 disabled。
 *
 * **第三个参数 `read` 是「读值」回调**：确认被点了才跑，而且**不传参**——
 * 它的返回值就是这张表 resolve 出去的东西。于是只有两种用法：
 *   - 要读表单值：read 返回那个值（改状态读下拉、重置密码读输入框）；
 *   - 只是「确认完就跑动作」：read 里把动作做了，返回 true。
 *
 * 别按「拿到一个回调再调它」的写法来：那个参数是 undefined，一调就抛。
 * 抛在接口调用之前，按钮就是完全空转；抛在之后，那张表永远不关。 */
function confirmBody(content, okLabel, read) {
  const dlg = document.getElementById("confirm");
  const inner = el("div", "confirm-inner");
  inner.append(content);
  const acts = el("div", "confirm-actions");
  const cancel = el("button", "secondary", "算了");
  cancel.type = "button";
  const ok = el("button", "danger", okLabel || "确认");
  ok.type = "button";
  acts.append(cancel, ok);
  inner.append(acts);
  dlg.textContent = "";
  dlg.append(inner);

  return new Promise(resolve => {
    let answered = false;
    function finish(v) {
      if (answered) return;              // 幂等守卫：三条路径都会调到它
      answered = true;
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onNo);
      dlg.removeEventListener("close", onClose);
      if (dlg.open) dlg.close();
      resolve(v);
    }
    function onOk() { finish(read ? read() : true); }
    function onNo() { finish(false); }
    function onClose() { finish(false); }        // Esc / 点外面 = 取消
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onNo);
    dlg.addEventListener("close", onClose);
    if (!dlg.open) dlg.showModal();
  });
}

// ---------------------------------------------------------------- 页签

function showTab(name) {
  state.tab = name;
  document.querySelectorAll("#tabs .tab").forEach(b =>
    b.classList.toggle("is-on", b.dataset.tab === name));
  document.querySelectorAll(".pane").forEach(p =>
    { p.hidden = p.dataset.pane !== name; });
  // 审计页签第一次打开时才拉数据：值班的人未必关心审计，
  // 每 10 秒轮询时多打一个接口没有必要。
  if (name === "audit" && !state.auditAll.length) {
    loadAudit().catch(e =>
      setMsg(document.getElementById("board-msg"), e.message || "审计日志拉取失败", "bad"));
  }
  // 反思每次切进来都拉（不像审计那样只拉第一次）：它是有时效的待办，
  // 管理员判完一条回值班台、再切回来时看到的必须是最新那批。
}

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
  if (state.clockTimer) { clearInterval(state.clockTimer); state.clockTimer = null; }
  if (state.reflTimer) { clearTimeout(state.reflTimer); state.reflTimer = null; }
  state.tasks = [];
  state.byId.clear();
  state.cards.forEach(n => n.remove());
  state.cards.clear();
  state.pending.clear();
  state.users = [];
  state.reflCounts = {};
  state.reflDetail = null;
  state.reflOpenId = null;
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
    const got = await api("POST", "/login", {
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

// ---------------------------------------------------------------- 控件

let qTimer = null;
document.getElementById("q").oninput = e => {
  state.filter.q = e.target.value.trim();
  clearTimeout(qTimer);
  qTimer = setTimeout(renderTasks, 120);      // 连续输入时合并重排
};
document.getElementById("status-filter").onchange = e => {
  state.filter.status = e.target.value; renderTasks();
};
document.getElementById("only-active").onchange = e => {
  state.filter.activeOnly = e.target.checked; renderTasks();
};
document.getElementById("audit-task").onchange = e => {
  state.auditFilter.task = e.target.value;
  loadAudit().catch(x => setMsg(document.getElementById("board-msg"), x.message, "bad"));
};
document.getElementById("audit-kind").onchange = e => {
  state.auditFilter.kind = e.target.value; renderAudit();
};
document.getElementById("audit-q").oninput = e => {
  state.auditFilter.q = e.target.value.trim(); renderAudit();
};
document.getElementById("audit-fail").onchange = e => {
  state.auditFilter.failOnly = e.target.checked; renderAudit();
};
document.getElementById("audit-export").onclick = () => exportAuditCsv();
document.getElementById("refresh-btn").onclick = () => load().catch(e =>
  setMsg(document.getElementById("board-msg"), e.message || "刷新失败", "bad"));
document.getElementById("logout-btn").onclick = () => signedOut("已退出");
document.getElementById("tabs").onclick = e => {
  const b = e.target.closest(".tab");
  if (b) showTab(b.dataset.tab);
};

buildScopeSwitch();
/* 委托而不是逐个绑：范围控件和空视图里那个「切到…」按钮都是运行时挂上去的，
 * 委托一次就把两处都盖住了，也不会漏掉以后新增的节点。 */
document.addEventListener("click", e => {
  const b = e.target.closest("[data-scope]");
  if (b) setScope(b.dataset.scope);
});

document.getElementById("new-user-form").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = document.getElementById("nu-save");
  btn.disabled = true;
  try {
    await api("POST", "/users", {
      username: document.getElementById("nu-name").value.trim(),
      password: document.getElementById("nu-pass").value,
      role: document.getElementById("nu-role").value,
    });
    document.getElementById("new-user-form").reset();
    await loadUsers();
    setRowMsg("已创建账号", "ok");
  } catch (ex) {
    setRowMsg(ex.message || "创建失败", "bad");
  } finally {
    btn.disabled = false;
  }
});

/* Esc：逐级退出页签？值班台不需要——没有抽屉，也没有全屏态。
 * **唯一要避的是模态 dialog**：确认表开着时全局处理器一律不插手。
 * 不避让的话，确认表按 Esc 会连带把底下的界面也响应一遍，而 Promise 又
 * 没正常走到，按钮卡在 disabled 再也点不动。 */
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (document.querySelector("dialog[open]")) return;
});

/* 兜底轮询：值班台不能靠实时流单独活着（通道会断、反代会掐）。
 * 页面在后台标签时不跑，省得空转。 */
setInterval(() => {
  if (!state.me) return;          // 还没通过 /me 确认身份（含没登录）就别空转
  if (document.visibilityState !== "visible") return;
  load().catch(() => {});
  if (state.tab === "audit") loadAudit().catch(() => {});
  /* 反思这里**无条件**拉，不看当前页签：页签上的角标是「有多少条等我判」，
   * 只在打开页签时刷，那个角标会一直停在打开那一刻的值，等于没有角标。
   * 代价只是一个只读列表接口，很便宜。 */
}, 10000);

function startClock() {
  if (state.clockTimer) clearInterval(state.clockTimer);
  const tick = () => {
    const d = new Date();
    const p = n => String(n).padStart(2, "0");
    document.getElementById("clock").textContent =
      p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  };
  tick();
  state.clockTimer = setInterval(tick, 1000);
}

/* 下面这份名单是给控制台调试用的。
 *
 * **名单里写错一个名字，整份脚本就废了。** 它是顶层语句，引用到不存在的标识符
 * 会抛 ReferenceError，而那是在模块求值阶段——后面的 boot() 根本不会执行。
 * 症状极其有欺骗性：页签能切（那些监听器挂在前面，已经绑好了），但一个接口都
 * 不发、看板永远是空的，看起来像"后端没数据"。
 *
 * 曾经就有一个 `render` 留在名单里：渲染早就拆成 renderLive / renderTasks 等
 * 几个函数，render 这个名字早就不存在了。整个值班台于是从来没活过。 */
Object.assign(window, {
  api, state, el, load, reload: load, loadUsers, loadAudit,
  renderLive, renderPending, renderTasks, renderAudit, renderUsers, setScope,
  fmtTime, fmtMs, fmtSize, STATUS_ORDER, STATUS_LABEL, ROLE_LABEL,
  SOURCE_LABEL, CMD_LABEL, KIND_LABEL, ACTOR_LABEL,
  apiBase, token,
  buildRail, paintRail, railIndex, confirmAction, confirmBody,
  exportAuditCsv, doVerify, doAbort, doRetry, sendRpc, showTab,
});

/* 登录与否问服务端，不问 localStorage —— 理由同用户面板那边：令牌只说明
 * "上次点过登录"。旁路下压根没有令牌，/me 却是通的，早先那句
 * if (!token()) return showLogin() 会让面板永远停在登录框。 */


/* ---------------------------------------------------------------- 状态面板
 *
 * 管理员看得到**系统指标与全局速度**；这一段是这一层特有的，
 * 常规用户那边（web/）只渲染自己那一部分。
 *
 * 系统指标是**单独一个请求**：普通用户拿到的是 403，所以那边根本不会发。
 * 这里是管理员，看得见，所以发。
 */

function spHours(sec) {
  const n = Number(sec) || 0;
  if (n <= 0) return "—";
  if (n < 3600) return Math.round(n / 60) + " 分钟";
  return (n / 3600).toFixed(1) + " 小时";
}

function spBox(cls, label, value) {
  const b = el("div", cls);
  b.append(el("div", "sp-k", label), el("div", "sp-v", value));
  return b;
}

async function loadStatus() {
  const host = document.getElementById("sp-metrics");
  const sys = document.getElementById("sp-system");
  const note = document.getElementById("sp-note");
  if (!host) return;
  try {
    const o = await api("GET", "/status/overview");
    const t = o.tasks || {};
    const d = o.duration || {};
    host.textContent = "";
    host.append(spBox("sp-metric", "任务总数", String(t.total || 0)));
    host.append(spBox("sp-metric", "进行中", String(t.running || 0)));
    host.append(spBox("sp-metric", "已完成", String(t.done || 0)));
    host.append(spBox("sp-metric", "失败", String(t.failed || 0)));
    host.append(spBox("sp-metric", "成功率",
      Math.round((Number(o.success_rate) || 0) * 100) + "%"));
    host.append(spBox("sp-metric", "平均单时", spHours(d.avg_seconds)));
    if (note) note.textContent = o.scope === "admin" ? "全局" : "我的任务";
  } catch (e) {
    host.textContent = "";
    if (note) note.textContent = "状态暂时取不到";
    return;
  }
  try {
    const s = await api("GET", "/status/system");
    sys.textContent = "";
    sys.hidden = false;
    sys.append(el("div", "sp-sys-title", "系统"));
    const cpu = s.cpu || {};
    const mem = s.memory || {};
    const gpu = s.gpu || {};
    const disk = s.disk || {};
    sys.append(spBox("sp-sys", "CPU", (cpu.percent || 0) + "%"));
    sys.append(spBox("sp-sys", "内存", (mem.percent || 0) + "%"));
    sys.append(spBox("sp-sys", "GPU",
      gpu.available ? (gpu.util_percent || 0) + "%" : "无 GPU"));
    sys.append(spBox("sp-sys", "数据盘剩余",
      (disk.free_gb || 0) + " GB"));
  } catch (e) {
    sys.hidden = true;
  }
}

async function boot() {
  // 状态面板：先画它，别让它拖住别的。取不到也不该让面板整个空着。
  loadStatus().catch(() => {});
  try {
    state.me = await api("GET", "/me");
    document.getElementById("who").textContent =
      (state.me.username || "") + "（管理员）";
    await load();
    await loadUsers().catch(() => {});   // 用户列表失败不该挡住值班台本身
    /* 角标要在**开页时就**有，而不是等第一次 10 秒轮询。
     * 只靠轮询的话：刷新之后那十秒里「反思」页签上一个角标都没有，
     * 值班的人会以为没有待办——而实际上有十三条。 */
    startClock();
  } catch (e) {
    if (e && e.status === 401) {
      signedOut(token() ? "登录已过期，请重新登录" : "请登录");
    } else {
      setMsg(document.getElementById("board-msg"), (e && e.message) || "连不上服务", "bad");
    }
  }
}

boot();
