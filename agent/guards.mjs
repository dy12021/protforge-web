/**
 * 命令守卫（AST，不是正则）
 *
 * 存在的理由：计算进程由 LLM 驱动，它会拼出任意 shell 命令。
 * 一旦它（因为幻觉、被诱导、或单纯的判断失误）拼出 `rm -rf /`、
 * `docker run --privileged` 或 `bash -c "eval ..."`，破坏的是宿主机和
 * ProtForge 自己的运行环境，不是某个可以回滚的容器。
 *
 * 实现方式：用 mvdan.cc/sh 的 JS 移植（npm `mvdan-sh`，GopherJS 转译）
 * 把命令解析成真正的 shell 语法树，然后显式遍历可执行节点做判断。
 * **不用正则判断命令**——正则会把 `r""m -rf /`、换行、管道、
 * 各种引号组合简化成不可预测的模式，守卫会退化成猜谜。
 *
 * 三条设计原则：
 * 1. 失败即拒。解析器覆盖不到的语法 → 拒绝，不是放行。
 * 2. 递归到底。`bash -c` / `wsl.exe … bash -c` / `xargs` / `$(...)` /
 *    反引号 / 管道 / `;` / `&&` / `||` 都会把内层重新送回守卫。
 * 3. 不退化。正向用例必须能过，否则守卫会把 agent 卡死（见 guards.test.mjs）。
 *
 * ---------------------------------------------------------------------------
 * 已知缺口（不要误以为这里什么都没漏）
 *
 * 【缺口 1｜`python -c` 等内联解释器的内容无法检查】
 *   `python -c "import os; os.system('rm -rf /')"` 里，引号内是**任意代码**，
 *   不是 shell。守卫解析出来也只能看到一个 DblQuoted 字面量，无法判断它
 *   会做什么。因此守卫对内联解释器只做一件事：确认「允许在当前上下文运行
 *   内联解释器」，**不检查字符串内容**。这个缺口无法在 L1 关闭，只能靠
 *   「命令执行进程不以高权限运行」和「工作目录隔离」兜底。
 *   脚本文件形式（`python run.py`）不受影响：路径本身是可静态判定的。
 *
 * 【缺口 2｜变量里的命令无法静态判定】
 *   命令名或破坏性操作的目标如果来自变量（`$CMD`、`rm $TARGET`），
 *   守卫一律拒绝而不是猜。
 *
 * 【缺口 3｜mvdan-sh 是 GopherJS 转译，少数字段不可读】
 *   具体表现为 `DblQuoted.Value` 与 `Pos` 结构体读不出来。因此本文件
 *   **不使用** `syntax.Walk`（它在转译下不下钻到 `Word.Parts`），
 *   改为显式递归；引号内文本取自 `DblQuoted.Parts[i].Lit.Value`。
 *
 * 【缺口 4｜重定向运算符没有名字表】
 *   `syntax.RedirOpName` 未被转译导出，这里用的是 mvdan 0.10.1 上实测的
 *   运算符编码。**遇到表外的编码一律拒绝**，避免升级后静默放行。
 *
 * 【缺口 5｜控制流语句体读不出来 —— 影响面最大的一条】
 *   GopherJS 转译没有导出 `IfClause.Body` / `WhileClause.Cond` /
 *   `ForClause.Loop` / `ForClause.Body` / `LetClause.Assigns` 等字段，
 *   取到的是 `undefined`。也就是说守卫**看不见 if / for / while / until /
 *   let / declare 里面执行了什么**。看不见就不能放行，所以这些节点一律拒绝。
 *   代价是 agent 不能写 `for ... do ... done`，必须改写成长命令串或脚本文件。
 *   `case` 不受影响（`CaseClause.Items` 能读到）。
 *
 * 【缺口 6｜脚本文件的内容不在守卫视野内】
 *   `bash launcher.sh`、`python run.py` 只检查**路径**，不检查文件内容。
 *   兜底是写工具的路径限制：agent 自己写出来的脚本只能落在工作目录里，
 *   这和它直接用 `python -c` 写任意代码是同一档风险，不算额外放开。
 *
 * 【缺口 7｜按 PID 定向杀仍可能打到平台自己的进程】
 *   守卫拦得住「按名字杀」（`taskkill /IM python.exe`、`pkill`、`killall`、
 *   `kill -1`）：那是无差别的整机操作。2026-10-20 真机就是这么把平台三个
 *   服务连同 agent 进程一起杀掉的（工作流当场断掉，日志里连一行崩溃栈都没有）。
 *   但 `taskkill /PID <n>` / `kill <pid>` **放行**——agent 必须能掐掉自己起的
 *   失控计算进程（OpenMM 跑飞、端口被占），不挡的话它只能等超时。
 *   残留风险：agent 若先 `tasklist` 拿到平台服务的 PID 再按 PID 杀，仍打得中。
 *   兜底是让**服务用独立于 agent 计算进程的可执行名**——现在两边都叫
 *   python.exe，所以「按名字杀」根本没有「只杀我自己的」这种写法。
 */

import { createLocalBashOperations } from '@earendil-works/pi-coding-agent';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const shSyntax = require('mvdan-sh').syntax;
const { NewParser, NodeType } = shSyntax;

// ------------------------------------------------------------------ 常量

/** 递归深度上限，防止 `bash -c "bash -c \"bash -c ...\""` 把栈打爆。 */
const MAX_DEPTH = 12;

/**
 * 语义不可静态判定的命令，直接拒绝。
 * `eval` / `source` / `.` 是规格点名要拒的；`exec` / `trap` 同理——
 * 前者把当前 shell 换成待执行命令，后者把任意字符串留到信号触发时执行。
 */
const DENY_ALWAYS = new Set(['eval', 'source', '.', 'exec', 'trap']);

/**
 * 按**名字**杀进程的命令：作用域是整机，不受工作目录约束。
 *
 * 2026-10-20 真机事故：agent 为了清理自己的残留进程跑了
 * `taskkill //F //IM python.exe //T`，把平台自己的三个服务（都是 python.exe）
 * 连同服务派生出来的 agent 进程一起杀了——工作流当场断掉，而且日志里连一行
 * 崩溃栈都没有（被强杀，不是崩），排查时完全看不出是agent 自己动的手。
 *
 * 关键在于**平台的服务进程与 agent 的计算进程同叫 python.exe**：
 * 「按名字杀 python」根本没有「只杀我自己的」这种写法，不存在既杀掉残留计算、
 * 又放过平台的选项。所以按名字杀一律拒绝，agent 要清理残留只能用 PID。
 */
const DENY_KILL_BY_NAME = new Set(['pkill', 'killall', 'pskill']);

/** taskkill 按名字杀的选项（Windows 上大小写不敏感，MSYS 下是 //IM 这种写法）。 */
const TASKKILL_IMAGE_FLAGS = ['/im', '//im'];

/**
 * 内联解释器代码里出现即拒的词：**按名字杀进程**那一类。
 * 用来窄化堵掉【缺口 1】里最要命的一类（见 checkInterpreter 的说明）。
 */
const KILL_BY_NAME_TOKENS = ['taskkill', 'stop-process', 'pkill', 'killall', 'pskill'];

/**
 * 会改变文件系统的命令，需要对其操作数做路径判定。
 * cp/mv 只判定**目标**（最后一个操作数）——从工作目录外读文件是正常用法。
 */
const DESTRUCTIVE = new Set([
  'rm', 'rmdir', 'del', 'erase', 'shred', 'srm', 'dd', 'mkfs', 'mkfs.ext4',
  'mkfs.ext3', 'mkfs.xfs', 'diskpart', 'format', 'wipefs', 'cp', 'mv',
]);

/** 只包一层、不改变语义的命令：取剩余 argv 递归回去。 */
const WRAPPERS = new Set([
  'sudo', 'doas', 'nohup', 'env', 'nice', 'ionice', 'timeout', 'stdbuf',
  'time', 'command', 'builtin', 'setsid', 'watch', 'wsl', 'wsl.exe', 'xargs',
]);

/** 需要额外吃掉一个位置参数的 wrapper（`timeout 5 cmd` 里的 `5`）。 */
const WRAPPER_POSITIONAL = { timeout: 1 };

/**
 * 内联解释器。`-c` 之后的内容是任意代码，见文件头【缺口 1】。
 * 这里只确认「允许在当前上下文运行内联解释器」，不检查内容。
 */
const INTERPRETERS = new Set([
  'python', 'python3', 'py', 'node', 'ruby', 'perl', 'powershell', 'pwsh',
]);
const INTERPRETER_CODE_FLAGS = new Set([
  '-c', '--command', '-command', '-e', '/c', '-encodedcommand', '-encodedcommand',
]);

/** 支持 `-c <脚本>` 的 shell。 */
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'ksh', 'mksh', 'busybox', 'rbash']);

/** wsl.exe 自己会吃掉一个值的选项。 */
const WSL_VALUE_OPTS = new Set([
  '-d', '--distribution', '--distribution-name', '-u', '--user', '--cd',
  '-e', '--exec', '--root',
]);

/** xargs 自己会吃掉一个值的选项（`-I {}` 这种分离写法）。 */
const XARGS_VALUE_OPTS = new Set(['-n', '-P', '-d', '-a', '-L', '-I', '-E']);

/**
 * 重定向运算符编码，实测自 mvdan-sh 0.10.1（见【缺口 4】）。
 * 只放行写集合里的编码；表外编码一律拒绝。
 */
const REDIR_WRITE = new Set([54, 55, 57, 60, 64, 65]); // > >> <> >| &> &>>
const REDIR_READ = new Set([56, 59, 61, 63]);          // < 1>&2 << <<<

/**
 * ProtForge 自己的地盘（技能脚本、它的产物）——**从环境变量推出来，不写死盘符**。
 *
 * 服务端把 `PROTFORGE_DATA`（指向 <ProtForge 根>/data）注给 agent，
 * 这里取它的上一级当根。写死某个盘符的话，换台机器这套守卫会把技能目录
 * 整个挡在外面——而症状是「agent 什么也写不了」，很难一眼看出是路径不对。
 *
 * 取不到就退回到空（不放行任何宿主盘符）：宁可拦住，也不乱放。
 *
 * **每次调用现读**，不是模块加载时算一次：测试在运行中设这个变量，
 * 加载时算的话那些用例会看到空允许根而全红。
 */
function protforgeRoot() {
  const data = (process.env.PROTFORGE_DATA || '').trim();
  if (!data) return '';
  const i = Math.max(data.lastIndexOf('\\'), data.lastIndexOf('/'));
  return i > 0 ? data.slice(0, i) : '';
}

/**
 * 文件操作允许写入的绝对路径根。
 * 第一条是数据盘的工作目录，ProtForge 自己的地盘紧随其后。
 * 其余是 AF3Native WSL 里 ProtForge 技能真实使用的目录——
 * 这些路径出现在 SKILL.md 的官方示例里（`/esmfold2/bin/fold.py` 等），
 * 收紧它们会让 agent 无法完成任何计算，所以必须显式声明而不是一刀切。
 */
const STATIC_FILE_ROOTS = [
  '/tmp',
  '/esmfold2',
  '/opendde_venv',
  '/alphafold3_venv',
  '/alphafold3',
  '/alphafold',
  '/db',
  '/hmmer',
  '/msashared',
];

/** 文件工具的允许根（静态的那些 + ProtForge 自己的地盘）。 */
function allowedFileRoots() {
  const root = protforgeRoot();
  return root ? [...STATIC_FILE_ROOTS, root] : [...STATIC_FILE_ROOTS];
}

/** docker `-v` 允许挂载的宿主路径（规格要求：ProtForge 根之外的盘符禁止）。 */
function dockerRoots() {
  const root = protforgeRoot();
  return root ? [root] : [];
}

// ------------------------------------------------------------------ 结果

const OK = Object.freeze({ allowed: true });
const bad = (reason) => Object.freeze({ allowed: false, reason });

// ------------------------------------------------------------------ 路径

/**
 * 消解 `.` 与 `..`，把路径压成**没有上跳**的形式。
 *
 * 这一步不是可有可无的洁癖：不做的话 `<允许根>\..\..\Users\<user>\.ssh\id_rsa`
 * 规范化后是 `/mnt/<盘>/<允许根>/../../users/...`，`startsWith('/mnt/<盘>/<允许根>/')` 为真 ->
 * 直接放行，L1 守卫形同虚设。Python 侧的 `guards.py` 用 `Path.resolve()` 做了同一件事，
 * 两边行为**必须一致**，否则同一个路径在一个进程里被拦、在另一个进程里放行。
 *
 * 用手写消解而不是 node:path.posix.normalize：这里已经统一成 `/mnt/x/...` 的
 * POSIX 形式，`path.posix.normalize` 会把 `/mnt/<盘>/<允许根>/../../x` 正确压成 `/mnt/x`，
 * 但对 `//server/share` 这类 UNC 归一后的路径会额外动刀，行为不如显式可控。
 */
function normalizeDots(p) {
  const parts = p.split('/');
  const out = [];
  for (const seg of parts) {
    if (seg === '.') continue;
    if (seg === '..') {
      // 弹不动的 ..（根之上）就丢掉：posix 里 /.. === /
      if (out.length && out[out.length - 1] !== '..') out.pop();
      continue;
    }
    out.push(seg);
  }
  let s = out.join('/');
  if (!s.startsWith('/')) s = `/${s}`;
  if (s.length > 1 && s.endsWith('/')) s = s.slice(0, -1);
  return s;
}

/**
 * 把 Windows 与 WSL 路径统一成可比较的 `/mnt/x/...` 小写形式。
 *
 * 导出仅供测试断言规范化结果本身（`guards.dotdot.test.mjs`）——
 * 只测"某条命令放不放行"的话，消解前后的差异会被允许根的字面前缀吃掉，看不出来。
 */
export function canonPath(p) {
  let s = String(p).replace(/\\/g, '/');
  const drive = /^([A-Za-z]):\//.exec(s);
  if (drive) s = `/mnt/${drive[1].toLowerCase()}/${s.slice(3)}`;
  const gitStyle = /^\/([A-Za-z])\//.exec(s);
  if (gitStyle) s = `/mnt/${gitStyle[1].toLowerCase()}/${s.slice(3)}`;
  // 消解必须在盘符映射**之后**：映射前的 `<允许根>\..\..` 消解结果和映射后不一样。
  s = normalizeDots(s);
  return s.toLowerCase();
}

const isAbsoluteLike = (p) =>
  /^([A-Za-z]:[\\/]|\\\\|\/)/.test(String(p));

/** `~` / `~user` 指向家目录，本沙箱永远不在允许集合里。 */
const isHomePath = (p) => String(p).startsWith('~');

/**
 * 空设备：写进去的内容直接丢弃，不落文件、不删东西。
 *
 * `cmd 2>/dev/null` 是最常见的 shell 写法之一。把它判成「写到允许范围之外」
 * 是纯粹的误伤——守卫拦的应该是破坏，而这里什么都没有可破坏。实测代价很实在：
 * 计算进程每次想丢弃一条命令的 stderr 都会被拒一次，然后被迫改写写法重试。
 *
 * 只认**精确**匹配的两个名字（统一小写、分隔符归一），`/dev/null/x`、`./nul`
 * 之类照常走路径判定——放行「名字里带 null 的路径」等于给前缀绕过开门。
 */
const NULL_DEVICES = new Set(['/dev/null', 'nul']);
const isNullDevice = (p) =>
  NULL_DEVICES.has(String(p).replace(/\\/g, '/').toLowerCase());

function isUnderAny(p, roots) {
  const c = canonPath(p);
  return roots.some((r) => {
    const rc = canonPath(r);
    return c === rc || c.startsWith(`${rc}/`);
  });
}

// ------------------------------------------------------------------ AST 取值

/**
 * 把一个 Word 求成字面量字符串。
 * 含变量/子命令的词返回 `dynamic: true`——调用方据此决定「递归」还是「拒绝」。
 *
 * 注意 DblQuoted 本身没有可读的 Value（【缺口 3】），内层要从 Parts 拼。
 */
function wordToString(word) {
  let out = '';
  let dynamic = false;
  const parts = word && word.Parts ? word.Parts : [];
  for (const part of parts) {
    switch (NodeType(part)) {
      case 'Lit':
      case 'SglQuoted':
        out += String(part.Value);
        break;
      case 'DblQuoted':
        for (const inner of part.Parts || []) {
          const t = NodeType(inner);
          if (t === 'Lit') out += String(inner.Value);
          else dynamic = true;
        }
        break;
      default:
        dynamic = true;
        break;
    }
  }
  return { value: out, dynamic };
}

const isOptionWord = (w) => {
  const { value, dynamic } = wordToString(w);
  return !dynamic && value.startsWith('-');
};

/** 从 argv 里剥掉可执行文件名，返回基础命令名（小写、去路径、去 .exe）。 */
function baseName(words) {
  const { value, dynamic } = wordToString(words[0]);
  if (dynamic) return null;
  const tail = value.replace(/\\/g, '/').split('/').pop() || value;
  return tail.toLowerCase().replace(/\.exe$/, '');
}

// ------------------------------------------------------------------ 上下文

function makeContext(opts) {
  const fileRoots = [...allowedFileRoots(), ...(opts.fileRoots || [])];
  // 工作目录本身就是允许写入的范围，redirect/删除判定都要算上它。
  if (opts.workDir) fileRoots.push(opts.workDir);
  return { fileRoots, dockerRoots: dockerRoots(), workDir: opts.workDir };
}

// ------------------------------------------------------------------ 遍历

/**
 * 遍历语句列表。`;` 分隔的多个语句天然在这里被逐段解析。
 */
function visitStmts(stmts, ctx, depth) {
  if (!stmts || typeof stmts.length !== 'number') return OK;
  for (let i = 0; i < stmts.length; i++) {
    const r = visitStmt(stmts[i], ctx, depth);
    if (!r.allowed) return r;
  }
  return OK;
}

function visitStmt(stmt, ctx, depth) {
  if (!stmt) return bad('空语句');
  const r = visitCmd(stmt.Cmd, ctx, depth);
  if (!r.allowed) return r;
  for (const redir of stmt.Redirs || []) {
    const rr = visitRedirect(redir, ctx);
    if (!rr.allowed) return rr;
  }
  return OK;
}

/** 逐个词下钻：`$(...)` / 反引号 / 进程替换 / 双引号内嵌都在这里递归。 */
function visitWordParts(parts, ctx, depth) {
  for (const part of parts || []) {
    switch (NodeType(part)) {
      case 'Lit':
      case 'SglQuoted':
      case 'ParamExp':      // $VAR 只是个值，不会改变「跑哪个命令」
      case 'ExtGlob':
        break;
      case 'DblQuoted':
      case 'ParamExpBrace': // ${...} 的整体形式，视为值
        {
          const inner = wordToString({ Parts: [part] });
          if (!inner.dynamic) break;
          const rr = part.Parts ? visitWordParts(part.Parts, ctx, depth) : OK;
          if (!rr.allowed) return rr;
        }
        break;
      case 'CmdSubst':      // $(...) 与反引号是同一个节点
        {
          const r = visitStmts(part.Stmts, ctx, depth + 1);
          if (!r.allowed) return r;
        }
        break;
      case 'ProcSubst':      // <(...) / >(...) 在 bash 执行前就会跑起来
        {
          const r = visitStmts(part.Stmts, ctx, depth + 1);
          if (!r.allowed) return r;
        }
        break;
      case 'ArithmExp':      // $(( )) 里可以藏命令替换，见【已知缺口】下方注释
        return bad('算术展开 $(( )) 可能内嵌命令替换，守卫不解析，直接拒绝');
      case 'ArrayExpr':
        return bad('数组展开无法静态判定，直接拒绝');
      default:
        return bad(`无法识别的词成分（${NodeType(part)}），按失败即拒处理`);
    }
  }
  return OK;
}

function visitRedirect(redir, ctx) {
  const op = redir.Op;
  if (REDIR_READ.has(op)) return OK;
  if (!REDIR_WRITE.has(op)) {
    // 表外编码 = 未知运算符，放行等于把未来版本的重定向变成盲区。
    return bad(`未知的重定向运算符编码（${op}），按失败即拒处理`);
  }
  if (!redir.Word) return OK; // 数字 fd 复制（1>&2 之类）没有目标词
  const { value, dynamic } = wordToString(redir.Word);
  if (dynamic) return bad('重定向目标来自变量，无法静态判定');
  // 空设备先放行：它不是文件系统路径，落不进「允许根」判定，
  // 但丢弃输出没有任何副作用（见 isNullDevice 的说明）。
  if (isNullDevice(value)) return OK;
  if (!isAbsoluteLike(value)) return OK; // 相对路径落在 cwd（= 工作目录）内
  if (isHomePath(value)) return bad('重定向到家目录，禁止');
  if (!isUnderAny(value, ctx.fileRoots)) {
    return bad(`重定向写到允许范围之外：${value}`);
  }
  return OK;
}

/**
 * 取子语句列表。返回 null 表示**该字段在当前转译下读不出来**（见【缺口 5】），
 * 调用方必须据此拒绝，绝不能当成空列表放行。
 */
function stmtsOf(node, field) {
  const v = node ? node[field] : null;
  if (Array.isArray(v)) return v;
  if (v && typeof v.length === 'number') return v;
  return null;
}

function visitCmd(cmd, ctx, depth) {
  if (depth > MAX_DEPTH) return bad('嵌套层数超过上限，拒绝');
  if (!cmd) return bad('空命令');

  switch (NodeType(cmd)) {
    case 'CallExpr':
      return visitCallExpr(cmd, ctx, depth);

    // BinaryCmd 的两侧是 Stmt 而不是 Cmd（`a && b` 的 X/Y 就是 Stmt）。
    case 'Stmt':
      return visitStmt(cmd, ctx, depth);

    // 管道 / && / || 的两侧都必须是独立可执行节点，逐段过。
    case 'BinaryCmd': {
      const a = visitCmd(cmd.X, ctx, depth);
      if (!a.allowed) return a;
      return visitCmd(cmd.Y, ctx, depth);
    }

    case 'Subshell':
    case 'Block':
    case 'CoprocClause': {
      const stmts = stmtsOf(cmd, 'Stmts');
      if (!stmts) return bad(`无法读取 ${NodeType(cmd)} 的语句列表，按失败即拒处理`);
      return visitStmts(stmts, ctx, depth + 1);
    }

    case 'CaseClause': {
      // cmd.Word 是「被匹配的值的词」，不是可执行节点——它是值不是命令，
      // 所以只下钻它的词成分查命令替换，不能当成命令去判定。
      const w = cmd.Word ? visitWordParts(cmd.Word.Parts, ctx, depth) : OK;
      if (!w.allowed) return w;
      // 分支列表字段名在 mvdan 里是 Cases，转译后有时暴露成 Items，两个都试。
      const items = stmtsOf(cmd, 'Items') || stmtsOf(cmd, 'Cases');
      if (!items) return bad('无法读取 case 的分支列表，按失败即拒处理');
      for (let i = 0; i < items.length; i++) {
        const body = stmtsOf(items[i], 'Stmts');
        if (!body) return bad('无法读取 case 分支的语句列表，按失败即拒处理');
        const r = visitStmts(body, ctx, depth + 1);
        if (!r.allowed) return r;
      }
      return OK;
    }

    case 'FuncDecl': {
      const body = stmtsOf(cmd.Body, 'Stmts');
      if (!body) return bad('无法读取函数体，按失败即拒处理');
      return visitStmts(body, ctx, depth + 1);
    }

    // ---- 以下节点的内部结构在当前转译下读不出来，只能拒绝（【缺口 5】）----
    case 'IfClause':
    case 'WhileClause':
    case 'UntilClause':
    case 'ForClause':
    case 'LetClause':
    case 'DeclClause':
    case 'ArithmCmd':
      return bad(
        `\`${NodeType(cmd)}\` 的语句体在当前 shell 解析器转译下无法读取，`
        + '守卫不能确认里面跑的是什么，直接拒绝；请改写成不含 if/for/while/let 的等价命令。',
      );

    case 'TestClause': {
      if (!cmd.X) return bad('无法读取 [[ ]] 表达式，按失败即拒处理');
      return visitCmd(cmd.X, ctx, depth);
    }

    case 'TimeClause':
      if (!cmd.X) return bad('无法读取 time 的命令体，按失败即拒处理');
      return visitCmd(cmd.X, ctx, depth);

    default:
      return bad(`无法识别的可执行节点（${NodeType(cmd)}），按失败即拒处理`);
  }
}

function visitAssigns(assigns, ctx, depth) {
  if (!assigns || typeof assigns.length !== 'number') return OK;
  for (let i = 0; i < assigns.length; i++) {
    const a = assigns[i];
    if (!a.Value) return bad('数组/索引赋值（a=(...)）无法静态判定，直接拒绝');
    const r = visitWordParts(a.Value.Parts, ctx, depth);
    if (!r.allowed) return r;
  }
  return OK;
}

function visitCallExpr(ce, ctx, depth) {
  const words = ce.Args || [];
  if (!words.length) return bad('没有可执行文件名的调用，无法判定');

  // 先把词里的 $(...)/反引号/进程替换全部递归掉，再看命令本身。
  const wr = visitAssigns(ce.Assigns, ctx, depth);
  if (!wr.allowed) return wr;
  for (const w of words) {
    const r = visitWordParts(w.Parts, ctx, depth);
    if (!r.allowed) return r;
  }

  const base = baseName(words);
  if (base === null) return bad('命令名来自变量或子命令，无法静态判定');
  if (!base) return bad('命令名为空');

  if (DENY_ALWAYS.has(base)) {
    return bad(`\`${base}\` 的执行语义无法静态判定，直接拒绝`);
  }

  // bash -c "<脚本>"：取出内层字符串再递归一次。
  if (SHELLS.has(base)) {
    for (let i = 1; i < words.length; i++) {
      const { value } = wordToString(words[i]);
      if (value === '-c' || value === '--command') {
        const inner = words[i + 1];
        if (!inner) return bad(`${base} -c 缺少脚本参数`);
        const s = wordToString(inner);
        if (s.dynamic) {
          return bad(`${base} -c 的脚本内容含变量或子命令，改写成脚本文件再执行`);
        }
        return checkScript(s.value, ctx, depth + 1);
      }
    }
    // 没有 -c 就是执行脚本文件（`bash fold.py job.json`），文件路径可静态判定。
    const script = words[1];
    if (script) {
      const s = wordToString(script);
      if (!s.dynamic && isAbsoluteLike(s.value) && !isUnderAny(s.value, ctx.fileRoots)) {
        return bad(`${base} 执行的工作区之外的脚本：${s.value}`);
      }
    }
    return OK;
  }

  if (base === 'docker' || base === 'podman') {
    return checkDocker(words, ctx);
  }

  if (INTERPRETERS.has(base)) {
    return checkInterpreter(base, words, ctx);
  }

  const killVerdict = checkKill(base, words);
  if (killVerdict) return killVerdict;

  if (WRAPPERS.has(base)) {
    return checkWrapper(base, words, ctx, depth);
  }

  if (DESTRUCTIVE.has(base)) {
    return checkDestructive(base, words, ctx);
  }

  return OK;
}

// ------------------------------------------------------------------ 各类判定

/** `timeout 5 sh -c "…"` / `wsl.exe -d AF3Native -- bash …` / `ls | xargs rm …` */
function checkWrapper(base, words, ctx, depth) {
  const rest = [];
  const eat = base === 'xargs' ? XARGS_VALUE_OPTS : base === 'wsl' || base === 'wsl.exe' ? WSL_VALUE_OPTS : null;
  let skippedPositional = 0;
  let sawSeparator = false;

  for (let i = 1; i < words.length; i++) {
    const s = wordToString(words[i]);
    if (!sawSeparator && s.value === '--') { sawSeparator = true; continue; }
    if (s.dynamic) return bad(`${base} 的参数含变量，无法静态判定`);
    if (s.value.startsWith('-') && s.value !== '-') {
      // 分离写法的选项（`-n 5`）要连值一起吃掉，否则 `5` 会被当成命令名。
      if (eat && eat.has(s.value)) i++;
      continue;
    }
    if (base === 'env' && /^[A-Za-z_][A-Za-z0-9_]*=/.test(s.value)) {
      continue;                       // env FOO=bar cmd —— 前缀赋值不是命令，跳过
    }
    if (!sawSeparator && skippedPositional < (WRAPPER_POSITIONAL[base] || 0)) {
      skippedPositional++;
      continue;
    }
    // 第一个真正的非选项参数就是被包裹的命令，其余都是它的参数。
    for (let j = i; j < words.length; j++) rest.push(words[j]);
    break;
  }

  if (!rest.length) return OK; // 只有选项没有命令，本身不做任何事
  return visitCallExpr({ Assigns: [], Args: rest }, ctx, depth + 1);
}

/** 删除/移动类命令：操作数必须落在工作目录或声明的写入根内。 */
function checkDestructive(base, words, ctx) {
  const operands = words.slice(1).filter((w) => !isOptionWord(w));
  if (!operands.length) return OK;

  // cp/mv 只管目标——从工作目录外读源文件是 ProtForge 技能的正常做法。
  const targets = (base === 'cp' || base === 'mv') ? [operands[operands.length - 1]] : operands;

  for (const w of targets) {
    const { value, dynamic } = wordToString(w);
    if (dynamic) return bad(`${base} 的目标路径来自变量，无法静态判定`);
    // `{}` / `{,}` 是 xargs、find -exec 之类在**运行时**才替换的占位符。
    // 对 shell 来说它就是个普通字面量，但实际路径由输入流决定，
    // 所以必须按「未知路径」处理，否则 `xargs -I{} rm -rf {}` 就是一个后门。
    if (value.includes('{}') || value.includes('{:')) {
      return bad(`${base} 的目标路径含运行时占位符 ${value}，无法静态判定`);
    }
    if (isHomePath(value)) return bad(`${base} 指向家目录，禁止：${value}`);
    if (!isAbsoluteLike(value)) continue;           // 相对路径落在 cwd 内
    if (!isUnderAny(value, ctx.fileRoots)) {
      return bad(`${base} 作用于允许范围之外的路径：${value}`);
    }
  }
  return OK;
}

/**
 * 杀进程：**按名字杀一律拒绝，按 PID 杀放行**。
 *
 * 放行 PID 是因为 agent 必须能掐掉自己起的失控计算进程（OpenMM 跑飞、
 * 端口被占）——这是正常需求，不挡的话 agent 只能等超时。
 * 残留风险见文件头【缺口 7】：按 PID 定向杀仍然可能打到平台自己的进程。
 */
function checkKill(base, words) {
  if (DENY_KILL_BY_NAME.has(base)) {
    return bad(`\`${base}\` 按名字杀进程：作用域是整机，会连平台自己的服务一起杀掉，禁止`);
  }
  if (base === 'taskkill') {
    for (const w of words) {
      const { value, dynamic } = wordToString(w);
      if (dynamic) return bad('taskkill 的参数来自变量，无法静态判定');
      const v = value.toLowerCase();
      if (TASKKILL_IMAGE_FLAGS.some((f) => v === f || v.startsWith(`${f}:`))) {
        return bad(`taskkill 按名字杀进程（${value}）：会连平台自己的服务一起杀掉，禁止`);
      }
    }
    return OK;
  }
  if (base === 'kill') {
    const args = [];
    for (const w of words.slice(1)) {
      const s = wordToString(w);
      if (s.dynamic) {
        // 目标来自变量 → 无法静态判定，按【缺口 2】的规矩拒绝而不是猜。
        return bad('kill 的目标来自变量，无法静态判定');
      }
      args.push(s.value);
    }
    const negatives = args.filter((a) => /^-[0-9]+$/.test(a));
    if (negatives.length > 1) {
      // 第一个负数是信号号，其余是进程组目标（`kill -9 -1` = 杀 1 号进程组）。
      return bad('kill 的目标是负数（进程组）：会波及平台自己的服务，禁止');
    }
    if (negatives.length === 1 && args.length === 1) {
      // `kill -1` 没有目标 = 向所有进程发信号。
      return bad('kill 只有信号没有目标：等于「向所有进程发信号」，'
        + '会波及平台自己的服务，禁止');
    }
    const sep = args.indexOf('--');
    if (sep >= 0 && args.slice(sep + 1).some((a) => /^-\d+$/.test(a))) {
      // `kill -- -1`：显式写的负数目标（进程组）。
      return bad('kill 的目标是负数（进程组）：会波及平台自己的服务，禁止');
    }
    return OK;
  }
  return null;
}

/**
 * 内联解释器：`python -c "<代码>"`。
 *
 * 见【缺口 1】：代码内容整体无法静态判定，所以这里**不检查它**。
 * 但**按名字杀进程**这一类可以在代码层面窄化堵掉——它在代码里必然出现
 * 固定的命令词（taskkill / Stop-Process / pkill / killall）。
 * 判据是「出现即拒」：宁可误伤（agent 少打这几个字），不可放过
 * （平台的服务被连带杀掉，整批工作流当场断掉）。
 *
 * `-encodedcommand`（base64）直接拒：内容不可读就等于不可判定，
 * 按文件头第一条原则「失败即拒」。
 */
function checkInterpreter(base, words, ctx) {
  for (let i = 1; i < words.length; i++) {
    const { value, dynamic } = wordToString(words[i]);
    if (dynamic) continue;
    const flag = value.toLowerCase();
    if (flag === '-encodedcommand' || flag === '--encodedcommand') {
      return bad(`${base} 的编码命令内容不可读，无法静态判定，拒绝`);
    }
    if (!INTERPRETER_CODE_FLAGS.has(flag)) continue;
    const code = words[i + 1];
    if (!code) continue;
    const s = wordToString(code);
    if (s.dynamic) continue;              // 来自变量：整体不可判定（缺口 1 / 缺口 2）
    const low = s.value.toLowerCase();
    const hit = KILL_BY_NAME_TOKENS.find((t) => low.includes(t));
    if (hit) {
      return bad(`内联解释器代码里出现按名字杀进程的命令（${hit}）：`
        + `会连平台自己的服务一起杀掉，禁止`);
    }
  }
  return OK;
}

/** docker 额外禁令：特权容器、挂宿主根、挂 ProtForge 根之外的盘。 */
function checkDocker(words, ctx) {
  for (let i = 1; i < words.length; i++) {
    const { value, dynamic } = wordToString(words[i]);
    if (dynamic) continue; // 交给后续对整条命令的静态判定，这里不猜

    if (value === '--privileged' || value.startsWith('--privileged=')) {
      return bad('docker --privileged 禁止：特权容器等同宿主机 root');
    }
    for (const flag of ['--pid=host', '--ipc=host', '--net=host', '--network=host', '--userns=host']) {
      if (value === flag) return bad(`docker ${flag} 禁止`);
    }

    let spec = null;
    if (value === '-v' || value === '--volume') {
      const next = words[i + 1];
      if (!next) continue;
      const s = wordToString(next);
      if (s.dynamic) return bad('docker 挂载参数来自变量，无法静态判定');
      spec = s.value;
      i++;
    } else if (value.startsWith('--volume=')) {
      spec = value.slice('--volume='.length);
    }
    if (spec === null) continue;

    const src = splitVolumeSpec(spec);
    if (src === '') return bad('docker 挂载了宿主根');
    const c = canonPath(src);
    if (c === '/' || c === '' || /^\/mnt\/[a-z]$/.test(c)) {
      return bad(`docker 挂载了宿主根：${src}`);
    }
    // 相对挂载由 docker 按 cwd 解析，cwd 就是工作目录。
    if (!isAbsoluteLike(src)) continue;
    if (!isUnderAny(src, ctx.dockerRoots)) {
      return bad(`docker 挂载了允许范围之外的宿主路径（仅允许 ${ctx.dockerRoots.join('、') || '（未配置）'}）：${src}`);
    }
  }
  return OK;
}

/** `<盘符>\路径:/容器路径` 的分隔符在盘符后面，不能按第一个冒号切。 */
function splitVolumeSpec(spec) {
  const start = /^[A-Za-z]:/.test(spec) ? 2 : 0;
  const idx = spec.indexOf(':', start);
  return idx === -1 ? spec : spec.slice(0, idx);
}

// ------------------------------------------------------------------ 入口

function checkScript(source, ctx, depth) {
  if (depth > MAX_DEPTH) return bad('嵌套层数超过上限，拒绝');
  if (typeof source !== 'string' || !source.trim()) return bad('空命令');

  let file;
  try {
    file = NewParser().Parse(source, 'guard.sh');
  } catch {
    // 解析失败即拒。绝不能「解析不了就放行」。
    return bad('命令无法被 shell 语法解析器解析，按失败即拒处理');
  }
  if (!file || !Array.isArray(file.Stmts) || file.Stmts.length === 0) {
    return bad('命令解析结果为空，按失败即拒处理');
  }
  return visitStmts(file.Stmts, ctx, depth);
}

/**
 * 判定一条命令能不能执行。
 * @param {string} command
 * @param {{workDir?: string, fileRoots?: string[], dockerRoots?: string[]}} [opts]
 * @returns {{allowed: boolean, reason?: string}}
 */
export function checkCommand(command, opts = {}) {
  try {
    return checkScript(command, makeContext(opts), 0);
  } catch (e) {
    // 守卫自身崩溃时也必须拒绝——这是安全边界，不是可用性边界。
    return bad(`守卫内部错误：${e && e.message ? e.message : String(e)}`);
  }
}

// ------------------------------------------------------------------ pi 接线

/**
 * 记一条审计。**记账失败绝不抛。**
 *
 * 审计是证据，不是任务的一部分。让它有能力把一条本来跑成功的命令变成
 * "执行失败"，模型会换个写法把同一件事再跑一遍——那既浪费又危险。
 * 失败只留一行 stderr。
 */
async function reportAudit(opts, row) {
  const fn = opts && opts.onAudit;
  if (typeof fn !== 'function') return;
  try {
    await fn(row);
  } catch (e) {
    console.error('[audit] 上报失败（不影响命令本身）:', e && e.message ? e.message : e);
  }
}

/**
 * 把守卫接到 pi 的 BashOperations.exec 上。
 * pi 文档里的推荐姿势是包住 operations：本地执行保持不变，
 * 只有在命令真正跑起来之前插入校验。
 *
 * `opts.onAudit` 存在时，这里就是规格 §14「所有 bash 调用全量记入审计日志」
 * 的实现点。**放行与拦截都要记**：被拦下的那条恰恰是事后追责最想看的一行，
 * 只记成功的话，"它曾经想干什么、为什么被挡下"就永远查不到。
 */
export function createGuardedBashOperations(opts, base = null) {
  const local = base || createLocalBashOperations();
  return {
    async exec(command, cwd, options) {
      const verdict = checkCommand(command, opts);
      if (!verdict.allowed) {
        // 命令原文一行不改地留在第一行，拒绝理由追加在下一行。
        // audit_log 的列是规格 §9.2 定死的（没有 reason 列），而"为什么被拦"
        // 是这段证据的一半；改写原文又会毁掉"里面有没有 rm"的判断，
        // 所以按行拼接——原文仍是原文，只是后面跟了一行说明。
        await reportAudit(opts, {
          kind: 'bash_denied',
          command: `${command}\n# 被守卫拒绝：${verdict.reason}`,
          exit_code: null,              // 从没执行过，没有退出码可报
          elapsed_ms: 0,
        });
        // 抛错而不是静默失败：让模型看到拒绝理由并换一条写法，
        // 这是守卫唯一能帮它继续工作的方式。
        throw new Error(`命令被安全守卫拒绝：${verdict.reason}\n请改用等效的安全写法后重试。`);
      }
      const started = Date.now();
      try {
        const result = await local.exec(command, cwd, options);
        await reportAudit(opts, {
          kind: 'bash',
          command,
          // pi 的 BashResult.exitCode 在被 kill / 取消时是 undefined，
          // 原样传 null 而不是编一个 0 出来——"没退出码"和"退出码是 0"
          // 在追责时是两回事。
          exit_code: result && result.exitCode !== undefined ? result.exitCode : null,
          elapsed_ms: Date.now() - started,
        });
        return result;
      } catch (e) {
        // 执行本身抛了（超时、被杀、shell 起不来）同样要留痕，
        // 否则"命令跑了很久然后没了"在审计里查不到任何东西。
        await reportAudit(opts, {
          kind: 'bash',
          command,
          exit_code: null,
          elapsed_ms: Date.now() - started,
        });
        throw e;
      }
    },
  };
}

/** 文件工具的路径限制：只允许读工作目录与声明的根。 */
function confine(absolutePath, opts, what) {
  const allowed = allowedFileRoots();
  if (opts.workDir) allowed.push(opts.workDir);
  if (!isUnderAny(absolutePath, [...allowed, ...(opts.fileRoots || [])])) {
    throw new Error(`${what} 被安全守卫拒绝：${absolutePath} 不在工作目录或允许的根内`);
  }
  return absolutePath;
}

/** 给 pi 的 ReadOperations 套上路径限制。 */
export async function createGuardedReadOperations(opts, base) {
  const inner = base || (await defaultReadOperations());
  return {
    readFile: (p) => inner.readFile(confine(p, opts, '读取')),
    access: (p) => inner.access(confine(p, opts, '读取')),
    detectImageMimeType: inner.detectImageMimeType
      ? (p) => inner.detectImageMimeType(confine(p, opts, '读取'))
      : undefined,
  };
}

/** 给 pi 的 WriteOperations 套上路径限制。 */
export async function createGuardedWriteOperations(opts, base) {
  const inner = base || (await defaultWriteOperations());
  return {
    writeFile: (p, c) => inner.writeFile(confine(p, opts, '写入'), c),
    mkdir: (d) => inner.mkdir(confine(d, opts, '创建目录')),
  };
}

/** 给 pi 的 EditOperations 套上路径限制（读+写都要限）。 */
export async function createGuardedEditOperations(opts, base) {
  const inner = base || (await defaultEditOperations());
  return {
    readFile: (p) => inner.readFile(confine(p, opts, '编辑读取')),
    writeFile: (p, c) => inner.writeFile(confine(p, opts, '编辑写入'), c),
    access: (p) => inner.access(confine(p, opts, '编辑')),
  };
}

async function defaultEditOperations() {
  const fs = await import('node:fs/promises');
  return {
    readFile: (p) => fs.readFile(p),
    writeFile: (p, c) => fs.writeFile(p, c),
    access: (p) => fs.access(p, fs.constants.W_OK),
  };
}

async function defaultReadOperations() {
  const fs = await import('node:fs/promises');
  return {
    readFile: (p) => fs.readFile(p),
    access: (p) => fs.access(p),
    detectImageMimeType: async () => null,
  };
}

async function defaultWriteOperations() {
  const fs = await import('node:fs/promises');
  return { writeFile: (p, c) => fs.writeFile(p, c), mkdir: (d) => fs.mkdir(d, { recursive: true }) };
}
