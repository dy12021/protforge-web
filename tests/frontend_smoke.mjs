/**
 * 前端脚本的**加载冒烟**：把 app.js 放进一个极简 DOM 桩里跑一遍，
 * 顶层抛错就非零退出。
 *
 * 为什么需要它
 * ------------
 * 这个项目的前端已经被"加载时不报错、页面上却是死的"咬了四次：
 *
 *   1. apiBase() 多拼一次 /api —— 接口全 404，骨架照常渲染
 *   2. GET /api/tasks 的响应没拆信封就 .map —— 一张卡片都画不出来
 *   3. 详情信封当任务对象用 —— 抽屉一片空白
 *   4. **Object.assign(window, {…, render, …}) 里的 render 根本不存在**
 *      顶层 ReferenceError 把整个脚本掐死，boot() 从没执行过，
 *      于是值班台一个接口都不发、看板永远空着
 *
 * 第 4 条是最狠的一种：静态检查看不出（名单语法完全正确），
 * 后端测试更看不出来（它们不碰前端），只有打开页面看控制台才知道。
 * 而第 4 条的后果是**整个界面不工作**。
 *
 * 所以这里不看代码长什么样，只看**跑起来会不会炸**。
 *
 * 用法： node tests/frontend_smoke.mjs <app.js 路径> [...]
 */
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import vm from 'node:vm';

/** 一个"什么都行"的桩：任何属性都是可调用的桩，任何赋值都接受。 */
function makeStub(label = 'stub') {
  const fn = function stubbed() { return makeStub(label + '()'); };
  return new Proxy(fn, {
    get(_t, key) {
      if (key === Symbol.iterator) return function* empty() {};
      if (key === 'then') return undefined;          // 不能是 thenable，否则 await 永远挂着
      if (key === Symbol.toPrimitive) return () => 0;
      if (key === 'toString') return () => '[stub]';
      if (key === 'valueOf') return () => 0;
      return makeStub(`${label}.${String(key)}`);
    },
    set() { return true; },
    has() { return true; },
    apply() { return makeStub(`${label}()`); },
    construct() { return makeStub(`new ${label}`); },
  });
}

/**
 * 一个"够真"的元素桩：属性真的存得住、子节点真的挂得上。
 *
 * 用万能 Proxy 不行——board-msg 的错误文案是 append 进子节点的，Proxy 不存值，
 * 于是 boot() 自己 catch 住的那句错误就永远读不出来。
 */
function makeEl(tag, id) {
  let text = '';
  const node = {
    _on: {},
    tagName: tag, id: id || '', children: [], value: '',
    className: '', hidden: false, disabled: false, open: false, rows: 0,
    placeholder: '', title: '', type: '', href: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    dataset: {},
    style: { setProperty() {}, removeProperty() {}, getPropertyValue() { return ''; } },
    options: [],
    append(...a) { node.children.push(...a); },
    appendChild(c) { node.children.push(c); return c; },
    remove() {},
    // 监听器要**存住**，光 addEventListener 空实现的话，下面重放「登录」那一步
    // 就触发不了任何东西——而那一步恰恰是本文件最要紧的复现入口。
    addEventListener(type, fn) { (node._on[type] = node._on[type] || []).push(fn); },
    removeEventListener() {},
    /** 手动触发一类监听器，返回各处理器的返回值（async 的就是 promise，要等）。 */
    fire(type, ev = {}) {
      return (node._on[type] || []).map((fn) => fn({
        type, preventDefault() {}, stopPropagation() {}, ...ev,
      }));
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    // querySelector 必须给回一个元素：buildCols() 靠它拿 .col-count，
    // 给 null 的话 render() 第一行就炸——那是桩的锅，不是前端的锅。
    querySelector(sel) {
      if (!node._q) node._q = {};
      if (!node._q[sel]) node._q[sel] = makeEl('span');
      return node._q[sel];
    },
    querySelectorAll() { return []; },
    closest() { return null; },
    focus() {}, click() {}, reset() {}, scrollIntoView() {},
    // 流转轨的滑动动画量的是真实尺寸。桩里给一组零值，
    // 于是走"量不到就不滑"那条分支——动画本来就该在这种环境里跳过。
    getBoundingClientRect() {
      return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 };
    },
    showModal() { node.open = true; }, close() { node.open = false; },
    insertAdjacentHTML() {},
  };
  // 跟 DOM 一致：写 textContent 会把子节点清掉。
  Object.defineProperty(node, 'textContent', {
    get() { return text; },
    set(v) { text = String(v); if (v === '') node.children = []; },
    enumerable: true,
  });
  // 真 DOM 里 childNodes 也在。少了它，`ul.childNodes.length` 那类写法在桩里
  // 是 undefined.length —— 直接抛，于是「写进经验」那个按钮的行为根本测不了。
  Object.defineProperty(node, 'childNodes', {
    get() { return node.children; },
    enumerable: true,
  });
  return node;
}

/** 元素桩要能当 DOM 节点用，但上面那个万能桩已经够了（属性都是桩）。 */
function makeDom(byId) {
  const doc = {
    visibilityState: 'visible',
    createElement: (tag) => makeEl(tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text), children: [] }),
    getElementById(id) {
      if (!byId.has(id)) byId.set(id, makeEl('div', id));
      return byId.get(id);
    },
    // **#id 选择器与 getElementById 返回同一个节点**（真 DOM 就是同一个）。
    // 原来这里每次返回一个全新的桩，于是页面写进去的内容在桩外面根本读不到，
    // 「渲染结果对不对」这类断言一条也写不出来——只能去断源码里有没有某个词。
    // 非 id 的选择器（.class 之类）按选择器缓存一个桩，但不进 byId：
    // stringifyLeaks 只扫 byId，保持它原来只看得到 getElementById 建出来的那些。
    querySelector(sel) {
      const s = String(sel);
      if (s.startsWith('#')) return doc.getElementById(s.slice(1));
      if (!doc._q) doc._q = new Map();
      if (!doc._q.has(s)) doc._q.set(s, makeEl('div', s));
      return doc._q.get(s);
    },
    querySelectorAll: () => [],
    addEventListener() {},
  };
  return doc;
}

const store = new Map();
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

/**
 * 夹具来自**真接口的返回体**（tests/_real_fixtures.json，抓自一台跑着的服务）。
 *
 * 手写形状是不够的：这个冒烟已经抓到过一个只有真实数据才会触发的错，
 * 手写的最小对象走不到那几行。
 */
function loadFixtures() {
  const p = new URL('./_real_fixtures.json', import.meta.url);
  const FIXTURES = JSON.parse(readFileSync(p, 'utf8'));
  // 用户面板的口径不同：同一个文件里的 /tasks 是管理端口的形态，
  // 用户端口返回的是信封 {task, events, artifacts}。
  FIXTURES['/api/tasks'] = {
    tasks: FIXTURES['/tasks'].tasks,
    queue: FIXTURES['/tasks'].queue,
  };
  FIXTURES['/api/tasks/1'] = {
    task: FIXTURES['/tasks'].tasks[0],
    events: [], artifacts: [],
  };

  // 工作流页的夹具。形状照 server/app.py 里的 _wf_view / 列模型 / 详情路由写，
  // 不是随手编的：冒烟抓到过的「响应没拆信封就 .map」就是形状不对。
  // 未知路径的兜底是 {}，于是 d.workflows 会是 undefined，
  // for...of 直接抛 TypeError——所以这一段缺了会让冒烟红。
  //
  // 四条从代码里核出来、照抄直觉会写错的点：
  //   1. **writeback 是返回的**（M-4 把 _wf_view 里的 pop 去掉了，规格 2 决策表
  //      「回写是可开关」）。前端 workflow.js 真的在读 wf.writeback 去勾那颗
  //      复选框，夹具里缺这个键的话勾选框恒为未勾选，看着像"开关坏了"。
  //   2. **行级统计只在列表端点挂**（app.py 的 _wf_view(stats) 注释写明）：列表
  //      的每张卡片带 failed_rows / current_row / current_status，**详情端点的
  //      workflow 没有这三个键**。夹具必须照抄这个差别，否则卡片会走缺键分支
  //      显示「失败行数没算出来」——真实接口不会那么说。
  //   3. workflow.status 的 CHECK 是 ('draft','running','paused','finished')，
  //      **终态是 finished，不是 done**（行任务才是 done/failed）。
  //   4. 详情路由还返回 skips；列表只返回 workflows 一个键。
  //
  // 键的顺序有讲究：stubFetch 用 startsWith(k + '/') 找夹具，**先插入的赢**。
  // 具体的（/preview）必须排在笼统的（/api/workflows）前面，否则预览会拿到
  // 列表的信封——而信封形状错了前端照样不报错，只是渲染出 undefined。
  const WF = {
    id: 1, owner_id: 1, name: '示例工作流', status: 'running',
    sheet: 'Sheet1', source_path: 'C:/data/workflows/wf1/source.xlsx',
    prompt_template: '请计算 {{seq}}',
    header_row: 1, key_col: 0,
    total_rows: 4, cursor: 3,
    writeback: 1,
    writeback_pending: 0, abort_requested: 0,
    result_path: 'C:/data/workflows/wf1/result.xlsx',
    created_at: '2026-10-18 10:00:00',
    updated_at: '2026-10-18 10:05:00',
  };
  FIXTURES['/api/workflows/1/preview'] = {
    row: 0,
    rendered: '请计算 SEQ-001',
    values: { Sheet1: { 0: 'SEQ-001', 1: '' } },
  };
  FIXTURES['/api/workflows/1'] = {
    workflow: WF,
    // workflow_columns 是 SELECT * ：id / workflow_id 也在里面
    columns: [
      { id: 1, workflow_id: 1, col_index: 0, header: '标识',
        role: 'key', slot: '', field: '' },
      { id: 2, workflow_id: 1, col_index: 1, header: '序列',
        role: 'input', slot: 'seq', field: '' },
      { id: 3, workflow_id: 1, col_index: 2, header: '结果',
        role: 'output', slot: '', field: 'summary' },
    ],
    // 第 3 行被用户跳过，第 4 行是真跑失败的。**跳过的行在 tasks 里 status 就是
    // failed**（补偿任务建出来就是终态，tasks.status 的 CHECK 里没有 skipped），
    // 界面必须靠 workflow_skips 把它显示成「已跳过」——否则表里写「已跳过」、
    // 卡片标红「失败 2 行」，用户分不清「我跳过的」和「跑失败的」。
    skips: [2],
    // 行就是普通计算任务（tasks 表），status 走主链那套词
    rows: [
      { id: 11, title: '行 1', status: 'done', summary: '算完了', error: '',
        workflow_id: 1, workflow_row: 0, workflow_archived: 1 },
      { id: 12, title: '行 2', status: 'running', summary: '', error: '',
        workflow_id: 1, workflow_row: 1, workflow_archived: 0 },
      { id: 13, title: '行 3 · 已跳过', status: 'failed',
        summary: '用户跳过：序列坏了', error: '用户跳过，未运行',
        workflow_id: 1, workflow_row: 2, workflow_archived: 0 },
      { id: 14, title: '行 4', status: 'failed',
        summary: '', error: '计算进程崩了',
        workflow_id: 1, workflow_row: 3, workflow_archived: 0 },
    ],
  };
  FIXTURES['/api/workflows'] = {
    // 列表比详情多三个统计键（见上面第 2 条）
    workflows: [Object.assign({}, WF,
      { failed_rows: 1, current_row: 1, current_status: 'running' })],
  };
  FIXTURES['/api/workflows/upload'] = {
    workflow: WF, headers: ['标识', '序列', '结果'], row_count: 3,
    preview: [['A1', 'SEQ-001', '']],
  };
  return FIXTURES;
}

const FIXTURES = loadFixtures();

/** 去掉 apiBase() 的前缀，只留路径部分。 */
function routeOf(url) {
  const s = String(url);
  const i = s.indexOf('/api/admin');
  if (i >= 0) return s.slice(i + '/api/admin'.length);
  return s.replace(/^https?:\/\/[^/]+/, '');
}

function stubFetch(calls, ctl = {}) {
  return async (url) => {
    calls.push(String(url));
    const path = routeOf(url);
    if (path.startsWith('/stream/')) {
      // SSE：立刻结束，读帧循环随之收尾。不要让它挂住。
      return { ok: true, status: 200, statusText: 'OK', body: null,
               json: async () => ({}), headers: new Map() };
    }
    // 「还没登录」这一态要能演出来：第一次 /api/me 必须 401，boot() 才会走
    // signedOut() 弹登录框。不这样的话，页面加载就一路走到底，
    // 而真正的复现入口（登录成功后 boot() 再跑一遍）压根不会发生。
    if (path === '/api/me' && ctl.notLoggedIn) {
      return { ok: false, status: 401, statusText: 'Unauthorized', body: null,
               json: async () => ({ detail: '登录已失效，请重新登录' }),
               headers: new Map() };
    }
    if (path === '/api/auth/login') {
      return { ok: true, status: 200, statusText: 'OK', body: null,
               json: async () => ({ token: 'stub-token',
                                    user: { username: 'stub', role: 'user' } }),
               headers: new Map() };
    }
    const key = Object.keys(FIXTURES).find(
      (k) => path === k || path.startsWith(k + '/') || path.startsWith(k + '?'));
    const body = key ? FIXTURES[key] : {};
    return { ok: true, status: 200, statusText: 'OK', body: null,
             json: async () => body, headers: new Map() };
  };
}

/**
 * boot() 必须拉够的接口。少一个就说明它在半路上抛了——
 * 而 boot() 自己的 catch 会把错写进提示条就完事，从外面看是"页面打开了、
 * 标题栏有用户名、看板却永远是空的"。只测"有没有抛"看不见这个。
 *
 * 两个面板口径不同：用户面板没有用户列表，路径也带 /api 前缀。
 */
const REQUIRED = {
  'web\\app.js': ['/api/me', '/api/tasks'],
  'web/app.js': ['/api/me', '/api/tasks'],
  // workflow.js 的这条**必须**在：isWeb 只匹配 app.js，查不到键时 required 是
  // 空数组，于是「boot() 走完了」那条检查变成恒真——页面整个死了也是绿的。
  'web\\workflow.js': ['/api/workflows'],
  'web/workflow.js': ['/api/workflows'],
  'admin-ui\\app.js': ['/me', '/tasks', '/users'],
  'admin-ui/app.js': ['/me', '/tasks', '/users'],
};

/** 把 #board-msg 里 boot() 自己写进去的原文挖出来。 */
function boardMessage(byId) {
  const host = byId.get('board-msg');
  if (!host) return '';
  const walk = (n) => (n.textContent || '') + (n.children || []).map(walk).join('');
  return walk(host).trim();
}

/**
 * 扫出被字面渲染出来的 "[object HTML…]" 之类。
 *
 * 这一条抓的是"把元素当文本塞进去"：值班台的操作列曾经整个显示成
 * [object HTMLDivElement]，中止/改状态/重跑三个按钮一个都按不到，
 * 而 DOM 里它们确实被创建了——不报错、不抛异常，只是点不动。
 */
function stringifyLeaks(byId) {
  const hits = [];
  const seen = new Set();
  const walk = (n, path) => {
    if (!n || typeof n !== 'object' || seen.has(n)) return;
    seen.add(n);
    const t = typeof n.textContent === 'string' ? n.textContent : '';
    if (/\[object [A-Za-z]+\]/.test(t)) hits.push(`${path || n.id || n.tagName}: ${t.slice(0, 60)}`);
    for (const c of n.children || []) walk(c, path);
  };
  for (const [id, node] of byId) walk(node, '#' + id);
  return hits;
}

/** #board 里真正的列有几个。className 是 buildCols() 里 el("section", "col") 给的。 */
function boardColumns(byId) {
  const b = byId.get('board');
  if (!b) return 0;
  return (b.children || []).filter((c) => c && c.className === 'col').length;
}

/**
 * 一个节点在页面上显示出来的那句话：**要递归拼子节点**。
 *
 * 桩的 textContent 只存「直接赋值」的那一句，而 appendChild 挂上去的文本节点
 * 不在里面——真 DOM 里 td.textContent 会把子节点全算上。表格里那一格是
 * 「色点 span + 文本节点」拼出来的，只读 textContent 一律是空串，于是任何
 * 「这一格写着什么」的断言都会变成恒假的反面（读出来永远是空，比对什么都失败）。
 */
function textOf(node) {
  if (!node) return '';
  const kids = node.children || [];
  if (!kids.length) return node.textContent || '';
  return kids.map(textOf).join('');
}

/**
 * Major-1 的行为围栏：用户跳过的行，在**每一处面向用户的地方**都不是失败。
 *
 * 这里真的调页面的函数（openDetail / renderRows），断的是**渲染结果**，
 * 不是「文件里出现过某个词」。上一轮复审把 failed_rows 改成恒 0 时 143 条
 * 全绿——那就是文本围栏的代价；而这一条把数改回去它就红。
 *
 * 夹具里同时放了一行**真**失败（第 4 行）：只有「跳过不算失败」这一半的话，
 * 把整个统计改成恒 0 也能过。四处一起看：卡片式的详情失败数、筛选结果、
 * 状态文案、状态色点。
 */
async function checkSkippedRows(r) {
  const detail = FIXTURES['/api/workflows/1'];
  const box = (id) => r.sandbox.document.getElementById(id);
  await r.sandbox.openDetail(1);

  // ① 详情页顶部的失败数：跳过的行不进，真失败的那行照进
  const failed = box('wf-failed');
  if (textOf(failed) !== '失败 1 行') {
    return [`详情页的失败数是「${textOf(failed)}」，应该是「失败 1 行」` +
            '（跳过的行被算成失败了）'];
  }
  if (!/is-bad/.test(failed.className)) {
    return ['真失败的那一行没标红'];
  }

  // ② 表格里那一行显示的是「已跳过」，色点也是 skipped 而不是 failed
  const body = box('wf-body');
  const rowOf = (n) => (body.children || []).find((tr) => tr.dataset.row === String(n));
  // 一行的结构是 [行号 th, ...每一列的 td]；只有结果列那一格带状态（色点 + 文案）
  const statusCell = (n) => {
    const tr = rowOf(n);
    return tr ? (tr.children || []).slice(1).find((td) => (td.children || []).length) : null;
  };
  const skippedCell = statusCell(2);
  const realCell = statusCell(3);
  const bad = [];
  if (!skippedCell) {
    bad.push('跳过的行根本没渲染出来');
  } else {
    if (textOf(skippedCell) !== '已跳过') {
      bad.push(`跳过的那一行显示「${textOf(skippedCell)}」，应该是「已跳过」`);
    }
    const dot = skippedCell.children[0];
    if (!dot || dot.dataset.status !== 'skipped') {
      bad.push(`跳过那一行的状态色点还是 ` +
               `${dot ? dot.dataset.status : '没有'}，是红的（=失败）`);
    }
  }
  if (!realCell || !realCell.children[0]
      || realCell.children[0].dataset.status !== 'failed') {
    bad.push('真失败的那一行没有被标成 failed——修过头了');
  }

  // ③「只看失败行」：只剩真失败的那一行，跳过的不许混进来
  box('wf-only-failed').checked = true;
  // 桩的 innerHTML = '' 不清 children（真 DOM 会），这里手动清一下再重绘
  body.children.length = 0;
  r.sandbox.renderRows(detail);
  const shown = (body.children || []).map((tr) => tr.dataset.row);
  if (shown.join(',') !== '3') {
    bad.push(`「只看失败行」筛出来的是 [${shown}]，应该只有 ['3']（真失败的那行）`);
  }
  if (!/显示 1 \/ 4 行/.test(box('wf-filter-note').textContent)) {
    bad.push(`筛选后的对账文案是「${box('wf-filter-note').textContent}」，` +
             '应该写明显示 1 / 4 行');
  }

  // ④ 第 3 步向导里的汇总也走同一套判据
  r.sandbox.showStep();
  if (!/失败 1 行/.test(box('wf-summary').textContent)) {
    bad.push(`汇总里写的是「${box('wf-summary').textContent}」，` +
             '应该只说真失败的 1 行');
  }

  // 还原：后面还有别的检查，别把筛选留在勾上
  box('wf-only-failed').checked = false;
  body.children.length = 0;
  r.sandbox.renderRows(detail);
  return bad;
}

/**
 * Minor-2 的跨语言一致性围栏：本地预览与服务端 workflow.render() 是**两份实现**，
 * 同一段模板 + 同一组取值，两边必须给出同一句话。
 *
 * 用例由 tests/test_workflow_preview_parity.py 用**服务端**渲染出 expected 之后
 * 写到临时 JSON，再用环境变量 PROTFORGE_WEB_PARITY 指过来。不这么做就只能去断
 * 「源码里出现过同一个正则」——把 SLOT_RE 改窄一点两边就分家了，而上一轮实测
 * 那种断言 38 条全绿。
 */
function checkParity(sandbox) {
  const path = process.env.PROTFORGE_WEB_PARITY;
  if (!path) return [];
  const cases = JSON.parse(readFileSync(path, 'utf8'));
  const bad = [];
  for (const c of cases) {
    const got = sandbox.renderSlots(c.tpl, c.slots);
    if (got !== c.expected) {
      bad.push(`${c.name}：服务端渲染成 ${JSON.stringify(c.expected)}，` +
               `本地渲染成 ${JSON.stringify(got)}——预览给用户看的和真正跑下去的` +
               '不是同一句话');
    }
  }
  return bad;
}

async function smoke(file) {
  const code = readFileSync(file, 'utf8');
  const errors = [];
  const calls = [];
  const byId = new Map();
  // 用户面板的第一跑必须是"还没登录"：boot() 先建列、再问 /api/me，
  // 于是列已经建好才弹登录框。登录成功后 boot() 又跑一遍——这一步才是复现入口。
  const isWeb = /web[\\/]app\.js$/.test(file);
  const ctl = { notLoggedIn: isWeb };
  // async 函数（boot 就是）抛出的错会变成**没人处理的 rejection**：页面上什么都
  // 不显示，控制台的 uncaught 里也未必好找。必须当失败收掉，不能让它悄悄过去。
  const onUnhandled = (reason) => {
    errors.push(reason);
  };
  process.on('unhandledRejection', onUnhandled);
  process.on('uncaughtException', onUnhandled);

  const sandbox = {
    document: makeDom(byId),
    localStorage,
    location: { search: '', href: 'http://127.0.0.1/', origin: 'http://127.0.0.1' },
    navigator: { userAgent: 'node' },
    console: { log() {}, info() {}, warn() {}, error() {}, debug() {} },
    // 用**形状与真接口一致**的数据喂它：只答 401 的话 boot() 会在 load() 之前
    // 就返回，渲染路径一行都不执行——而"拿到数据之后渲染时才炸"正是最难查的那类。
    fetch: stubFetch(calls, ctl),
    setTimeout: (fn) => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
    queueMicrotask,
    URL, URLSearchParams, AbortController, TextDecoder, TextEncoder,
    Blob: class Blob {}, Event: class Event {}, CustomEvent: class CustomEvent {},
    // Option 是 DOM 全局：<select> 的构造函数。少它的话 renderStatusFilter()
    // 在 boot() 的 try **之前**就会抛，而 boot() 是 async —— 结果是一个谁也不
    // 处理的 rejection，页面上什么都不显示、什么也看不到。
    Option: class Option { constructor(text, value) { this.text = text; this.value = value; } },
    Promise, JSON, Math, Date, Object, Array, Map, Set, Number, String, Boolean,
    Error, TypeError, RangeError, Symbol, Proxy, Reflect, parseInt, parseFloat,
    isNaN, isFinite, encodeURIComponent, decodeURIComponent,
    Intl, RegExp,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  // prefers-reduced-motion 的查询：答"不偏好"，于是动效照走，代码路径最全。
  sandbox.matchMedia = () => ({
    matches: false, media: '', addEventListener() {}, removeEventListener() {},
    addListener() {}, removeListener() {},
  });
  sandbox.getComputedStyle = () => ({ getPropertyValue: () => '' });

  const context = vm.createContext(sandbox);
  try {
    vm.runInContext(code, context, { filename: file, displayErrors: true });
    // 顶层跑完了，把微任务与 promise 里的错也收一收
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));

    // 重放用户真机上点的那一步：登录框里登进去。
    // 没有这一步的话，第一跑就一路走到底，上面那个「还没登录」的态等于白设，
    // 而看板重复建列的洞恰好只在这条路径上现形。
    if (isWeb) {
      // 走 document.getElementById 而不是直接查 byId：桩是按需建节点的，
      // 只有真的调过 getElementById 的 id 才会进 Map。
      const form = sandbox.document.getElementById('login-form');
      ctl.notLoggedIn = false;              // 登录成功之后 /api/me 才该通
      sandbox.document.getElementById('l-user').value = 'stub';
      sandbox.document.getElementById('l-pass').value = 'stub';
      await Promise.all(form.fire('submit'));
      await new Promise((r) => setImmediate(r));
      await new Promise((r) => setImmediate(r));
    }
  } catch (e) {
    errors.push(e);
  } finally {
    process.off('unhandledRejection', onUnhandled);
    process.off('uncaughtException', onUnhandled);
  }

  return {
    errors, calls, byId, sandbox,
    cols: boardColumns(byId),
    expectCols: isWeb ? (sandbox.COLUMNS || []).length : null,
  };
}

/**
 * 反思那四个**危险按钮**的行为围栏：重跑 / 否决 / 写进经验 / 应用方案。
 *
 * 这条围栏是被一条真缺陷逼出来的。`confirmBody` 的第三个参数是「读表单值」
 * 的回调——onOk 里是 `read()`，**不传参**；而这四个按钮照着**用户面板**
 * `confirmAction` 的写法写成了「拿到一个回调再调它」。那个参数是 undefined，
 * 一调就抛：抛在接口调用之前，重跑按钮完全空转；抛在之后，那张确认表永远
 * 不关，Promise 悬着。
 *
 * 静态断言看不出这个：形状完全正确，正则也匹配得上，58 条静态围栏照绿。
 * 所以这里**真的点一遍**：先点按钮（这时不许有任何写接口调用），再点确认表
 * 里的确认（这时必须正好一次），最后确认表要关上。
 */
/**
 * 在确认表里找第一个 <input>（落地那一步的技能输入框）。
 *
 * 桩的 `setAttribute` 是空实现、`getAttribute` 恒回 null，所以没法按
 * `list="refl-skill-list"` 找——只能按标签名走树。
 */
function findInput(root) {
  const stack = [root];
  while (stack.length) {
    const n = stack.shift();
    if (!n || !n.children) continue;
    for (const c of n.children) {
      if (c && c.tagName === 'input') return c;
      stack.push(c);
    }
  }
  return null;
}

async function checkReflectionConfirms(r) {
  const errs = [];
  const sb = r.sandbox;
  const calls = [];
  const realApi = sb.api;
  sb.api = (method, path, body) => {
    calls.push([method, path, body]);
    if (method === 'GET') {
      return Promise.resolve({
        id: 7, diff: '--- a/x\n+++ b/x', proposal_target: 'skills/x/SKILL.md',
      });
    }
    if (String(path).endsWith('/land')) {
      // 落地端点会带回一份报告：写了多少条、其中多少条进了技能目录。
      return Promise.resolve({ ok: true, landed: 2, landed_into_skill: 1,
                               note: '只有 1 条进了技能目录' });
    }
    return Promise.resolve({ ok: true, message: 'stub' });
  };
  const dlg = sb.document.getElementById('confirm');
  /** 确认表里的「确认」按钮：#confirm > .confirm-inner > .confirm-actions > [算了, 确认] */
  const okButton = () => {
    const inner = dlg.children[0];
    const acts = inner.children[inner.children.length - 1];
    return acts.children[acts.children.length - 1];
  };
  /** 动作型调用：只有这几个是「点了确认才该发生」的写操作。 */
  const writes = () => calls.filter((c) => c[0] === 'POST' && String(c[1]).includes('/reflections/'));
  const cases = [
    ['retryBtn', { id: 7, error: 'boom' }, '/reflections/7/retry', '重跑'],
    ['rejectBtn', { id: 7 }, '/reflections/7/reject', '否决'],
    ['landBtn', { id: 7, skill: 'protforge-demo' }, '/reflections/7/land', '写进去'],
    ['applyBtn', { id: 7 }, '/reflections/7/apply', '应用'],
  ];
  try {
    for (const [fn, arg, path, label] of cases) {
      calls.length = 0;
      let btn;
      try {
        btn = sb[fn](arg);
        // applyBtn 的处理函数是 async 的（要先取回方案），所以要等它跑完。
        await Promise.all(btn.fire('click'));
      } catch (e) {
        errs.push(`${fn}() 点下去就抛了：${e && e.message}`);
        continue;
      }
      if (writes().length) {
        errs.push(`${fn}() 没过确认就写接口了：${JSON.stringify(writes())}`);
        continue;
      }
      let ok;
      try {
        ok = okButton();
        if (ok.textContent !== label) {
          errs.push(`${fn}() 的确认按钮写着「${ok.textContent}」，应该是「${label}」`);
          continue;
        }
        /* 落地那一条：技能是管理员在**这张表里**填的，所以得先去表里填上。
         *
         * 顺带钉住「不许预填」：预填等于把「模型的猜测」变成「管理员明确指定」，
         * 而「通用」那条经验的归属正是靠这个区别才说得清——预填过的话，
         * 模型标的「通用」会被自动塞进某个技能目录。 */
        if (path.endsWith('/land')) {
          const input = findInput(dlg);
          if (!input) {
            errs.push('landBtn() 的确认表里没有技能输入框：管理员没法指定归属');
            continue;
          }
          if (input.value) {
            errs.push(`landBtn() 把技能预填成了「${input.value}」：`
              + '模型标的「通用」会被当成管理员指定，跨技能的经验被塞进某个技能');
            continue;
          }
          input.value = 'protforge-demo';
        }
        ok.fire('click');
      } catch (e) {
        errs.push(`${fn}() 点了确认之后抛异常：${e && e.message}`);
        continue;
      }
      await new Promise((res) => setImmediate(res));
      const hit = writes().filter((c) => String(c[1]) === path);
      if (hit.length !== 1) {
        errs.push(`${fn}() 确认之后${hit.length ? '写了 ' + hit.length + ' 次' : '没有写'} `
          + `${path}：实际 ${JSON.stringify(calls)}`);
      }
      if (dlg.open) {
        errs.push(`${fn}() 确认之后确认表没有关闭（回调抛了或没返回）`);
        dlg.close();
      }
      // 落地那一条多两件事：管理员填的技能要发给服务端，落地报告要显示出来。
      // 少了第一条，模型标的技能（它写「通用」）对不上真实技能时，经验永远
      // 落不进技能目录；少了第二条，「只落了一半」管理员一个字都不会看到。
      if (path.endsWith('/land')) {
        const sent = calls.find((c) => c[0] === 'POST' && String(c[1]).endsWith('/land'));
        const skill = sent && sent[2] && sent[2].skill;
        if (skill !== 'protforge-demo') {
          errs.push(`landBtn() 没把管理员填的技能发给服务端：skill=${JSON.stringify(skill)}`);
        }
        const msg = sb.document.getElementById('board-msg');
        if (!/只有 1 条进了技能目录/.test(textOf(msg))) {
          errs.push('landBtn() 没把落地报告显示出来：只落了一半也不会有人知道');
        }
      }
    }

    /* 管理员**不填**技能 → 发过去的是空。
     *
     * 这条和上面那条不是重复：上面钉的是「填了要发出去」，这条钉的是
     * 「没填不许替模型填」。预填过的实现这里会是某个技能名——那条经验
     * 就被按**猜测**塞进技能目录了，而模型标的明明是「通用」。 */
    calls.length = 0;
    try {
      const btn2 = sb.landBtn({ id: 8, skill: 'protforge-demo' });
      await Promise.all(btn2.fire('click'));
      okButton().fire('click');
      await new Promise((res) => setImmediate(res));
      const sent = calls.find((c) => c[0] === 'POST' && String(c[1]).endsWith('/land'));
      if (!sent) {
        errs.push('landBtn() 管理员什么都没填，反而没调用落地接口');
      } else if (sent[2] && sent[2].skill) {
        errs.push(`landBtn() 管理员没填技能却发了一个技能名上去：${JSON.stringify(sent[2])}`);
      }
    } catch (e) {
      errs.push(`landBtn() 不填技能那条路径抛了：${e && e.message}`);
    }
  } finally {
    sb.api = realApi;
  }
  return errs;
}

const files = process.argv.slice(2);
if (!files.length) {
  console.error('用法：node tests/frontend_smoke.mjs <app.js> [...]');
  process.exit(2);
}

let bad = 0;
for (const f of files) {
  const errs = [];
  let board = '';
  try {
    const r = await smoke(f);
    errs.push(...r.errors);
    const paths = r.calls.map(routeOf);
    const required = REQUIRED[f.replace(/\//g, '\\')] ?? REQUIRED[f] ?? [];
    const missing = required.filter((p) => !paths.includes(p));
    board = boardMessage(r.byId);
    if (missing.length) {
      errs.push(new Error(
        `boot() 没走到完：缺这些接口 ${JSON.stringify(missing)}；`
        + `实际只发了 ${JSON.stringify(paths.slice(0, 8))}`
        + (board ? `；提示条上写的是「${board}」` : '')));
    }
    // boot() 自己把错 catch 掉了，只剩一句文案、没有栈。这里再直接调一次 load()
    // 把它抓个现行——两个面板都把 load 挂在 window 上，正是为了这种调试。
    if (typeof r.sandbox.load === 'function') {
      try {
        await r.sandbox.load();
      } catch (e) {
        errs.push(e);
      }
    }
    // 表格、看板、抽屉都要过一遍：把元素当文本塞进去的坑不抛异常，
    // 只在渲染结果里留下 "[object HTMLDivElement]"。
    const leaks = stringifyLeaks(r.byId);
    if (leaks.length) {
      errs.push(new Error(`有元素被当成文本渲染出来了：\n        ${leaks.join('\n        ')}`));
    }
    // 看板列只能有一套。重复的那一套不会报错、不抛异常，只是第一排永远停在 0——
    // 真机上表现为「两排一模一样的列，上面一排全是 0」。
    if (r.expectCols && r.cols !== r.expectCols) {
      errs.push(new Error(
        `看板列建了 ${r.cols} 套，应该 ${r.expectCols} 套：`
        + 'buildCols() 被跑了两遍（页面加载一次、登录成功再一次）'));
    }
    // 工作流页专属的两组行为围栏：
    //   ① 跳过的行不得表现为失败（Major-1）——断的是渲染结果；
    //   ② 本地预览与服务端 render() 必须给出同一句话（Minor-2）。
    if (/web[\\/]workflow\.js$/.test(f)) {
      for (const m of await checkSkippedRows(r)) errs.push(new Error(m));
      for (const m of checkParity(r.sandbox)) errs.push(new Error(m));
    }
    // 值班台的反思按钮：确认之前不许写，确认之后必须写。
    if (/admin-ui[\\/]app\.js$/.test(f)) {
      for (const m of await checkReflectionConfirms(r)) errs.push(new Error(m));
    }
  } catch (e) {
    errs.push(e);
  }
  if (errs.length) {
    bad += 1;
    console.log(`  炸了  ${f}`);
    for (const e of errs) {
      const s = e && e.stack ? e.stack.split('\n').slice(0, 5).join('\n        ') : String(e);
      console.log(`        ${s}`);
    }
  } else {
    console.log(`  OK    ${f}`);
  }
}
process.exit(bad ? 1 : 0);
