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
    tagName: tag, id: id || '', children: [], value: '',
    className: '', hidden: false, disabled: false, open: false, rows: 0,
    placeholder: '', title: '', type: '', href: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    dataset: {},
    style: { setProperty() {}, removeProperty() {}, getPropertyValue() { return ''; } },
    options: [],
    append(...a) { node.children.push(...a); },
    appendChild(c) { node.children.push(c); return c; },
    remove() {}, addEventListener() {}, removeEventListener() {},
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
    querySelector: () => makeEl('div'),    querySelectorAll: () => [],
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

function stubFetch(calls) {
  return async (url) => {
    calls.push(String(url));
    const path = routeOf(url);
    if (path.startsWith('/stream/')) {
      // SSE：立刻结束，读帧循环随之收尾。不要让它挂住。
      return { ok: true, status: 200, statusText: 'OK', body: null,
               json: async () => ({}), headers: new Map() };
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

async function smoke(file) {
  const code = readFileSync(file, 'utf8');
  const errors = [];
  const calls = [];
  const byId = new Map();
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
    fetch: stubFetch(calls),
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
  } catch (e) {
    errors.push(e);
  } finally {
    process.off('unhandledRejection', onUnhandled);
    process.off('uncaughtException', onUnhandled);
  }

  return { errors, calls, byId, sandbox };
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
