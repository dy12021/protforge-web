/**
 * ProtForge 计算平台 · agent 端入口
 *
 * 后端用 `node agent/run.mjs` 拉起本进程，并通过环境变量告知任务身份。
 * 本进程进入 pi 的 RPC 模式：stdin 收 JSON 命令、stdout 出事件与回执，
 * 这两条管道由主服务持有（所以本进程绝不能往 stdout 打任何非 JSON 的东西，
 * 所有日志一律走 stderr）。
 *
 * 环境变量（后端 spawn 时注入，不是 argv——argv 会被 ps 看到，token 不能走那里）：
 *   PROTFORGE_WEB_INTERNAL  内部 API 基址，如 http://127.0.0.1:8475
 *   PROTFORGE_TASK_ID       任务 id，决定回传 URL 与 token 归属
 *   PROTFORGE_AGENT_TOKEN   一次性内部通道令牌，绑定 task_id
 *   PROTFORGE_WORK_DIR      本任务的工作目录，所有产物落在这里
 *
 * 可选：
 *   PROTFORGE_SKILLS_DIR    技能目录（也认 PROTFORGE_WEB_SKILLS_DIR；
 *                          默认指到旁边那个技能仓的 skills/）
 *   PROTFORGE_WEB_DATA_ROOT 平台数据根。长期记忆与工具问题文件都在它下面。
 *   MINIMAX_API_KEY / MINIMAX_API_HOST   联网检索能力；缺失时降级但仍能跑
 *   PROTORGE_AGENT_DIR     pi 的模型配置目录，由调度器在任务工作目录里物化。
 *                          不设就退回 getAgentDir()（用户全局），而本机那份是
 *                          空的 {}——症状是「agent 一直不吭声」而不是报错。
 *   PROTORGE_LLM_PROVIDER / PROTORGE_LLM_MODEL   显式指定模型。不指定就由 pi 自己
 *                          挑「第一个可用模型」，空配置下等于没有模型。
 *
 * 复核模式（PROTFORGE_WEB_REVIEW_MODE）：
 *   '1' 或 'memory' —— 整理长期记忆的**常规计算进程**。
 *   'task'          —— **任务审核进程**：开一轮全新的上下文，去核某一个
 *                      已完成任务的产物满不满足原始需求。
 *   两者都不是管理员，也不属于任何任务：没有 agent 令牌、没有任务号，
 *   因此碰不到平台的任何管理能力（管理端点、改技能仓库、删任务）。
 *   它们能做的就是文件工具、命令和联网检索。
 *   需求写在 PROTFORGE_WEB_REVIEW_BRIEF_PATH 指的那个文件里，跑完一轮就退出。
 *
 *   'task' 模式另有一个环境变量：
 *   PROTFORGE_WEB_REVIEW_READONLY_ROOTS  只读根（多个用 ';' 分隔）。
 *                      审核进程**可以读**这些目录里的东西（例如被审任务的
 *                      产物原件），但**不许写、不许改、不许删**——
 *                      机器审核要是能改交付物，被审的就不是原来那份了。
 */

import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  createAgentSessionRuntime,
  createAgentSessionServices,
  createAgentSessionFromServices,
  createCodingTools,
  createLocalPowerShellOperations,
  createMcpExtension,
  createPowerShellTool,
  getAgentDir,
  loadSkillsFromDir,
  ModelRuntime,
  SessionManager,
  runRpcMode,
} from '@earendil-works/pi-coding-agent';

import {
  createGuardedBashOperations,
  createGuardedEditOperations,
  createGuardedReadOperations,
  createGuardedWriteOperations,
} from './guards.mjs';
import { createReporter, createReporterTools } from './tools.mjs';

/** 技能目录的默认位置：旁边那个技能仓的 skills/（clone 在旁边就能用）。
 *  指到别处用 PROTFORGE_SKILLS_DIR 或 PROTFORGE_WEB_SKILLS_DIR 覆盖。 */
const DEFAULT_SKILLS_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'protforge', 'skills');
const MCP_SERVER_NAME = 'minimax-coding-plan';

// ---------------------------------------------------------------- 环境

function required(name) {
  const v = process.env[name];
  if (!v) {
    console.error(`[agent] 致命：缺少环境变量 ${name}。本进程必须由主服务 spawn，不能手工启动。`);
    process.exit(2);
  }
  return v;
}

/**
 * 复核模式：两种身份，都是**常规计算进程**。
 *
 * 'memory' —— 整理长期记忆。
 * 'task'  —— 审核某一个已完成的任务。
 *
 * 两者都不是管理员，也不属于任何任务：没有 agent 令牌、没有任务号，
 * 因此碰不到平台的任何管理能力（管理端点、改技能仓库、删任务）。
 */
const REVIEW_MODE_RAW = (process.env.PROTFORGE_WEB_REVIEW_MODE || '').trim();
const REVIEW_KIND = REVIEW_MODE_RAW === '1' ? 'memory'
  : (['memory', 'task', 'runtime'].includes(REVIEW_MODE_RAW) ? REVIEW_MODE_RAW : '');
if (REVIEW_MODE_RAW && !REVIEW_KIND) {
  console.error(`[agent] 致命：未知的复核模式「${REVIEW_MODE_RAW}」（只能是 memory / task / runtime）`);
  process.exit(2);
}
const REVIEW_MODE = !!REVIEW_KIND;

/** 只读根：能读、不能写。多个用 ';' 分隔。 */
const READONLY_ROOTS = (process.env.PROTFORGE_WEB_REVIEW_READONLY_ROOTS || '')
  .split(';')
  .map((s) => s.trim())
  .filter(Boolean);

const INTERNAL = REVIEW_MODE
  ? (process.env.PROTFORGE_WEB_INTERNAL || '')
  : required('PROTFORGE_WEB_INTERNAL');
const TASK_ID = REVIEW_MODE
  ? (process.env.PROTFORGE_TASK_ID || 'review')
  : required('PROTFORGE_TASK_ID');
const TOKEN = REVIEW_MODE
  ? (process.env.PROTFORGE_AGENT_TOKEN || '')
  : required('PROTFORGE_AGENT_TOKEN');
const WORK_DIR = path.resolve(required('PROTFORGE_WORK_DIR'));
const SKILLS_DIR = process.env.PROTFORGE_SKILLS_DIR
  || process.env.PROTFORGE_WEB_SKILLS_DIR
  || DEFAULT_SKILLS_DIR;

/**
 * 平台数据根。**从环境变量推出来，不写死本机路径**：
 * 服务端 spawn 时注入 PROTFORGE_WEB_DATA_ROOT；没注入就退回仓库旁边的
 * data/（与 server/db.py 的默认值一致）。
 */
const DATA_ROOT = path.resolve(
  process.env.PROTFORGE_WEB_DATA_ROOT
  || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'data'));

/** 长期记忆：可复用做法的落点。经验的第一手写入对象就是干活的那个进程。 */
const LONG_TERM_FILE = path.join(DATA_ROOT, 'memory', 'long-term.md');
/** 工具问题：按日期攒文件，等用户每周统一修。 */
const TOOL_ISSUES_DIR = path.join(DATA_ROOT, 'tool-issues');
/** 这两个目录是平台自己的地盘，明确列进守卫的允许根，写工具才合法。 */
const MEMORY_ROOTS = [path.join(DATA_ROOT, 'memory'), TOOL_ISSUES_DIR];

// ---------------------------------------------------------------- 日志

const log = (...a) => console.error('[agent]', ...a);

// ---------------------------------------------------------------- 平台规则

/**
 * 注入给模型的平台约束。用 before_agent_start 而不是自己拼 system prompt，
 * 这样 pi 自己生成的技能清单、工具说明都原样保留，我们只在其后追加。
 */
const PLATFORM_RULES = `

## 本次任务的运行环境

- 你是这个计算平台的计算进程，负责把用户的需求翻译成 ProtForge 技能链并真正跑出结果。
- 工作目录是本次任务专属的沙箱：${WORK_DIR}。所有中间文件和最终产物都写在这里。
- 可用的计算技能见下方技能清单。**先读对应技能的 SKILL.md 再动手**，那里记录了宿主机、
  WSL 发行版、venv 路径和已知坑，比你自己推断可靠。
- 宿主机的计算环境在 WSL 发行版 AF3Native 里，通过 \`wsl.exe -d AF3Native -- bash <脚本> <参数>\` 调用。
  不要把复合命令内联进 wsl.exe（引号会在 powershell → wsl.exe → bash 之间层层丢失），
  写成 .sh 脚本文件再按路径调用。
- 命令会被安全守卫校验：解析不出来的、含 eval/source/.、以及作用在工作目录与 ProtForge
  自己的地盘之外路径上的删除/写入/挂载，一律拒绝并把理由返回给你。被拒就换写法，不要反复重试同一条。
- \`report_step\` 回传的是**给用户看的一句话进度**，它显示在任务卡片最显眼的位置。
  - 写**用户视角的动宾短语**，像进度播报：正在搜寻蛋白质序列 / 正在用 RFdiffusion 生成结构 /
    正在计算表面面积 / 正在比对候选表位。
  - **绝对不要**把命令、路径、参数照抄进去：\`正在执行：cd E:/.../work/13 && python foo.py 2>&1 | tail\`
    这种是审计日志的内容，不是给人读的。原样贴命令等于没回传。
    你跑的命令原样记在 audit_log 里，那里才查得到。
  - 30 个字以内，不带句号，不加「正在执行：」这种前缀。
  - **只在换阶段时上报**（换了技能、换了个产物、拿到新结论）。没换阶段就不用反复报——
    页面上的那句话本来就会一直挂着，长命令跑着的时候也不会动。
  - 顺带一提：\`report_step\` 不接受「进度条百分比」，只接受一句话。
- 只有在计算真的跑完、产物已经落在工作目录里之后，才调用 \`finish\`。
  没跑完就调 finish 会让后端直接去打包一堆半成品。
- **\`finish\` 是一份承诺，不只是收工信号**：调完之后结果进「待验收」，
  等人看过产物和结论才认。**人可以不认**，然后带着补充要求让你接着做。
  所以调之前问自己两句：
  - 产物齐了吗？（平台会查：\`summary\` 为空、或者产物全是 0 字节的空文件，
    直接判「验收没通过」——这两种是机械的，不给人留情面。）
  - 结论写清楚了吗？（\`summary\` 是人验收时唯一能读到的「这一轮说了什么」，
    写「已完成」等于什么都没说。）
- 需求里带着 \`## 续跑（第 N 轮）\` 时，说明这是接着上一轮做：**上一轮的产物
  还在工作目录里**，别从头重算，把不对的那部分改掉再交。
- **工作流任务（第 N 行）的产物落点**：这类任务的产物同样必须落在工作目录，也就是你启动时所在的那个目录。确实需要写共享目录时，文件名**必须**以 t{你的任务号}_ 开头（例如 t17_hit.csv）。
  - 落在共享目录根部又没带这个前缀的文件会被判成「归属不明」：产物发现只能靠时间窗兜底命中它，而时间窗命中的产物**未经确认、打包时会被拒**——这一行于是算成 failed，而表里**只有一个 failed、没有任何原因**。
  - 工作流是「一行失败不阻断整批」地往下跑的，所以这不是一行的问题：批量场景下它会一行接一行地全失败。写进工作目录就没有这个放大效应。
- **单条命令不超过 10 分钟。** 要更久就拆开：先用最小规模跑通（≤2 分钟），确认结果是对的，再放大。
  - 搜索类脚本（刚体对接、采样、蒙特卡洛、网格搜索）尤其别一上来就拉满。**跑满上限通常意味着它在原地打转**：砍到 10 分钟你还能拿到中间结果，跑到 3000 秒你只等到一句「超时」。
  - \`timeout\` 超过 600 秒之前先问一句：它是在算，还是在空转？
  - 放大之前先把中间结果落盘，被杀掉也还留得下东西。

## 收工前：把可复用的东西沉淀下来

- **长期记忆**（别的任务能照抄的做法）：${LONG_TERM_FILE}
  - 这次任务如果做对了什么**有复用价值**的事（参数怎么给、哪个口径要先定标、
    某个坑在哪儿、某条命令要加什么参数才不出错），**收工前**用 \`write\` / \`edit\`
    工具直接写进这个文件。**不要等谁来批准**——你就是那个第一手写入者。
  - 一条一行，格式固定：
    \`- [技能 技能名] 具体做法，别的任务能照抄。（来源：任务 #N；时效性：稳定）\`
  - \`[技能 技能名]\`：填实际用到的 protforge-* 技能名；跨技能的做法写「通用」。
  - \`来源\`：写清来自哪个任务、哪次计算。
  - \`时效性\`：做法本身不会随时间变就写 \`稳定\`；**依赖当下的环境、版本、路径、
    外部服务**（过一阵可能就不成立了）就写 \`可能有时效性\`。写了这句的，
    平台每天会安排一次复核去查证它现在还成不成立。
  - **要具体到别的任务能照抄**：写清楚参数、命令、判据。不要写套话
    （「要注意环境配置」这种话对下一个任务一点用都没有）。
  - **不要写只跟这一次有关的东西**：这次的输入文件名、这次的中间结果、
    只有这一次成立的数字。写它们等于把长期记忆变成日志。
  - **追加，不要覆盖**：\`write\` 会把整个文件冲掉，而 \`edit\` 是「把某一段替换成
    另一段」。正确姿势：先 \`read\` 一遍拿到最后一行，再用 \`edit\` 把最后一行替换成
    「最后一行 + 换行 + 你要追加的那一条」。文件还不存在时才用 \`write\` 写整份。
- **工具问题**（工具本身不够用）：写进 ${TOOL_ISSUES_DIR} 下的 \`<YYYY-MM-DD>.md\`，
  文件名用**当天日期**。什么时候写：发现某个工具缺了个参数、某一步只能靠猜、
  某个工具的输出会让人误解成别的意思。
  - 同样一条一行：\`- [技能 技能名] 哪个工具缺了什么 / 会导致什么后果。\`
  - **只写问题，不要自己去改技能仓库**：这些条目由用户每周统一修一遍。
  - 同一天可能撞见好几个问题，**追加**到当天那个文件里（做法同上：
    先 \`read\` 再 \`edit\` 追加，别用 \`write\` 覆盖）。
- 这两件事都在**收工前**做（在 \`finish\` 之前）。没有值得沉淀的就什么都不写——
  宁可不写，也不要写一条没用的。`;
console.log = console.error; // stdout 归 RPC 独占

/**
 * 复核模式的系统规则。**只讲边界，不讲任务**——
 * 具体要做什么写在 brief 里（服务端生成）。边界必须在这里说死：
 * 这个进程是常规权限，它碰到的每一条「顺手把顺手改了」的冲动都要被挡住。
 *
 * 两种身份两套规则，因为**它们被允许做的事根本不同**：
 * 记忆复核要动长期记忆文件（那本来就是它的活），任务审核**什么都不许动**。
 */

/** 长期记忆复核：整理知识，不碰交付物。 */
const REVIEW_RULES_MEMORY = `

## 你的身份与边界

- 你是「自动化计算平台」的**长期记忆复核进程**，权限是**常规权限**：
  你没有管理员令牌，碰不到管理端点，不许改技能仓库、不许删任务、不许提交 git。
- 你要做的**只有一件事**：按 brief 里写的要求整理长期记忆。
- 你可以用 read / write / edit / bash 这些工具，也可以联网检索。
- **不要往长期记忆里新增经验**：经验的第一手来源是干活的计算进程，不是你。
  你的活是整理（查证、删掉失效的、改写过时的），不是积累。
- 整理完按 brief 里说的写复核记录，然后结束。`;

/**
 * 用时复核：**在旁边当裁判**，判断一个跑很久的计算是不是还在合理推进。
 *
 * 与任务审核的根本区别：它**不判产物好不好**，只判**这个时长合不合理**。
 * 所以它连产物副本都不用看——它看的是「还在动吗」。
 */
const REVIEW_RULES_RUNTIME = `
## 你的身份与边界

- 你是「自动化计算平台」的**用时复核进程**，权限是**常规权限**：
  你没有管理员令牌，碰不到管理端点，不许改技能仓库、不许删任务、不许提交 git。
- 你是**独立的一轮**：你和当初干活的计算进程**不是同一个上下文**，
  你没看过它怎么想的，只看 brief 里给的事实。
- 你要做的**只有一件事**：按 brief 里写的要求，判断**这个计算任务的用时是否合理**。
- **只读**：brief 里指明的那个目录（被看那个任务的工作目录）**只许读，不许写、
  不许改、不许删**。你是裁判，不是施工队。
- 你**不要**去重跑那个计算、不要替它改代码、不要替它写产物。
  你的输出只有一个：结论文件。

## 判不准就不要硬判

- 拿不准就写 \`unclear\`。平台会把它当成「没裁决」，让任务继续跑。
- **误判成「不合理」的代价是白白判死一个正当的长计算**（几个小时的算力全废），
  比误判成「合理」严重得多。所以**倾向 unclear**。
- 但也别一路 unclear 放水：明显在原地打转的，就该说它不合理。

## 结论怎么写

把结论写成一个 JSON 文件（路径在 brief 里）：
\`\`\`json
{ "verdict": "reasonable | unreasonable | unclear", "reason": "一句话说清依据" }
\`\`\`
写完就结束，不要做别的事。`;

/** 任务审核：只读地看，独立地判。 */
const REVIEW_RULES_TASK = `

## 你的身份与边界

- 你是「自动化计算平台」的**任务审核进程**，权限是**常规权限**：
  你没有管理员令牌，碰不到管理端点，不许改技能仓库、不许删任务、不许提交 git。
- 你是**独立的一轮**：你和当初干活的计算进程**不是同一个上下文**，
  你没看过它怎么想的，只看它交出来的东西和你手里的需求。
  这正是让你来审的原因——自己审自己不算审。
- 你要做的**只有一件事**：按 brief 里写的要求，判断这一轮满不满足原始需求。
- **只读**：brief 里指明的那个目录（被审任务的产物原件）**只许读，不许写、
  不许改、不许删、不许移动**。守卫会挡；你要是在那里改了，被审的就不是原来那份了。
- **不要重算**：不许跑 ProtForge 技能链去「自己算一遍看看对不对」。
  那是干活的计算进程的活。你要看的是**它交出来的**东西。
- 你可以用 read / bash 这些工具看文件，也可以联网检索查判据。
- 写结论：按 brief 里说的路径写一个 JSON 文件，写完就结束。
- **不要猜**：看不清就判 fail 并在 reason 里说清「哪里看不清」。
  判错的代价是人再看一遍；瞎写一个 pass 的代价是坏结果被当成交付。`;

// ---------------------------------------------------------------- 组装

async function main() {
  mkdirSync(WORK_DIR, { recursive: true });
  mkdirSync(path.join(WORK_DIR, '.pi'), { recursive: true });

  // 技能：先用 loadSkillsFromDir 做一次显式发现与体检，再把目录交给 pi 原生加载。
  // 之所以不手工 formatSkillsForPrompt 拼进 system prompt：additionalSkillPaths
  // 已经让 pi 自己生成技能清单，再拼一份会出现两套会漂移的描述。
  const { skills, diagnostics } = loadSkillsFromDir({ dir: SKILLS_DIR, source: 'protforge' });
  log(`技能目录 ${SKILLS_DIR}：加载 ${skills.length} 个`);
  if (!skills.length) {
    log(`警告：没有发现任何技能，模型将拿不到 ProtForge 能力。诊断：${JSON.stringify(diagnostics)}`);
  }
  for (const d of diagnostics || []) log(`技能诊断：${d.type} ${d.message}`);

  // 复核模式没有任务、没有令牌，也就**没有回传对象**：
  // report_step / finish 往哪儿报？无处可报。所以这条路上用一个什么都不做的
  // 回传器占位——守卫的审计接线保持同一套形状（那段接线有测试钉着，
  // 改成两行三元表达式就会让它认不出来）。
  const reporter = REVIEW_MODE
    ? {
        step: async () => ({ ok: false, error: '复核模式没有回传对象' }),
        finish: async () => ({ ok: false, error: '复核模式没有回传对象' }),
        audit: async () => ({ ok: false, error: '复核模式没有回传对象' }),
        startHeartbeat: () => {},
      }
    : createReporter({ internal: INTERNAL, taskId: TASK_ID, token: TOKEN });
  if (!REVIEW_MODE) {
    reporter.startHeartbeat();
    await reporter.step('计算进程已启动，正在准备环境');
  }

  // ---- 命令守卫 ----
  const guardOpts = {
    workDir: WORK_DIR,
    // **长期记忆与工具问题目录**：计算进程要把可复用的做法写进长期记忆、
    // 把工具问题写进当天的日期文件。这两个目录是平台自己的地盘，
    // 明确列进允许根，写工具才合法（守卫默认只放行工作目录、
    // ProtForge 数据区与几个固定路径，而平台数据根不在其中）。
    // 长期记忆与工具问题目录**只有记忆复核进程能写**。
    // 任务审核进程只许写它自己的工作目录：它的活是出结论，不是沉淀经验——
    // 往长期记忆里塞「审核经验」会把第一手来源搞脏。
    fileRoots: REVIEW_KIND === 'memory' ? MEMORY_ROOTS : [],
    // **只读根**（只在任务审核模式下有值）：能读、不能写。
    // 没有这一层，审核进程理论上能改掉被审任务的产物——
    // 那份产物就不再是被审的那份，审核结论也就没有意义了。
    readOnlyRoots: READONLY_ROOTS,
    // 每条命令（含被守卫拦下的）都上报审计，落 audit_log。
    // 这是规格 §14「所有 bash 调用全量记入审计日志」在 agent 侧的一半；
    // 另一半是 server/app.py 的 POST /api/internal/agent/{id}/audit。
    // 少了任何一边，审计表就恒为空——而管理端不会报任何错。
    onAudit: (row) => reporter.audit(row.kind, row.command, row.exit_code, row.elapsed_ms),
  };
  const guard = {
    bash: createGuardedBashOperations(guardOpts),
    // PowerShellOperations 就是 BashOperations（pi 里是同一个类型别名），
    // 所以同一份守卫直接复用。
    powershell: createGuardedBashOperations(guardOpts, createLocalPowerShellOperations()),
    read: await createGuardedReadOperations(guardOpts),
    write: await createGuardedWriteOperations(guardOpts),
    edit: await createGuardedEditOperations(guardOpts),
  };
  log('命令守卫已启用（AST 解析，失败即拒）');

  const codingTools = createCodingTools(WORK_DIR, {
    bash: { operations: guard.bash },
    read: { operations: guard.read },
    write: { operations: guard.write },
    edit: { operations: guard.edit },
  });
  // createCodingTools 只产出 read/bash/edit/write，PowerShell 要单独挂一个。
  codingTools.push(createPowerShellTool(WORK_DIR, { operations: guard.powershell }));
  // 复核模式没有任务，也就没有 report_step / finish 可报。
  const customTools = REVIEW_MODE
    ? [...codingTools]
    : [...codingTools, ...createReporterTools(reporter)];

  /**
   * 关键接线：会话只接受工具**名字**（tools: string[]），不接受工具**实例**。
   * 也就是说 createAgentSessionFromServices 会用 pi 自己的默认实现重建
   * read/bash/edit/write——那是没有守卫的版本，守卫等于白写。
   *
   * 正确做法是 noTools:'builtin' 关掉全部内置工具，再把接好守卫的这份
   * 作为 customTools 注册进去。这样每一次文件读写和每一条命令都必定
   * 过我这一层，不存在「忘了传 operations 就静默失去防护」。
   */

  // ---- 联网检索（可选）----
  const extensionFactories = [platformRulesExtension()];
  const mcpKey = process.env.MINIMAX_API_KEY;
  if (mcpKey) {
    extensionFactories.push(createMcpExtension({
      // key 只从环境变量读，且以 ${VAR} 形式交给子进程解析，
      // 因此不会出现在任何配置文件、命令行或错误信息里。
      loadConfig: () => ({
        servers: [{
          name: MCP_SERVER_NAME,
          source: 'protforge-web-agent',
          config: {
            type: 'stdio',
            command: 'uvx',
            args: ['minimax-coding-plan-mcp'],
            env: {
              MINIMAX_API_KEY: '${MINIMAX_API_KEY}',
              MINIMAX_API_HOST: process.env.MINIMAX_API_HOST || 'https://api.minimax.cn',
            },
            // 直接暴露给模型，避免再引一层 codemode/tool_search 间接层。
            exposure: 'direct',
          },
        }],
        errors: [],
      }),
      startupWaitMs: 10_000,
    }));
    log('联网检索能力：已接入 minimax-coding-plan MCP');
  } else {
    // 缺 key 不是致命错误：技能链计算不需要联网，硬失败只会让整批任务白跑。
    log('联网检索能力未就绪：缺少 MINIMAX_API_KEY，本次任务只能使用本地技能链继续运行');
  }

  // 优先用调度器物化的那份（写在任务工作目录里，跟着任务建、跟着任务删）；
  // 没有就退回用户全局目录——那份在多数机器上是空的，见下面 MODEL 的处理。
  const agentDir = process.env.PROTFORGE_AGENT_DIR || getAgentDir();
  const LLM_PROVIDER = (process.env.PROTFORGE_LLM_PROVIDER || '').trim();
  const LLM_MODEL = (process.env.PROTFORGE_LLM_MODEL || '').trim();
  const sessionManager = SessionManager.inMemory(WORK_DIR);

  const createRuntime = async ({ cwd, agentDir: dir, sessionManager: sm }) => {
    // 显式构造 ModelRuntime 并把模型挑出来。**必须挑**：不挑的话 pi 取
    // 「第一个可用模型」，而空配置下那等于没有——agent 会安静地一直不吭声。
    const extras = {};
    if (LLM_PROVIDER && LLM_MODEL) {
      const mr = await ModelRuntime.create({
        authPath: path.join(dir, 'auth.json'),
        modelsPath: path.join(dir, 'models.json'),
      });
      const found = mr.getModel(LLM_PROVIDER, LLM_MODEL);
      if (found) {
        extras.modelRuntime = mr;
        extras.model = found;
        log(`模型：${LLM_PROVIDER}/${LLM_MODEL}`);
        // 密钥是不是真的在？模型配好了但没密钥的话，请求会 401，
        // 而失败长得像成功——所以在这里就把它说清楚。
        try {
          if (typeof mr.hasConfiguredAuth === 'function'
              && !mr.hasConfiguredAuth(LLM_PROVIDER)) {
            log(`警告：${LLM_PROVIDER} 这个 provider 没有可用的凭据，`
              + `模型请求大概率 401`);
          }
        } catch { /* 查不到就算了，不影响主流程 */ }
      } else {
        const have = mr.getModels(LLM_PROVIDER).map((m) => m.id);
        log(`模型配置里没有 ${LLM_PROVIDER}/${LLM_MODEL}；`
          + `该 provider 下有：${have.length ? have.join(', ') : '(空)'}`);
      }
    } else {
      log('未指定模型（PROTFORGE_LLM_PROVIDER / PROTORGE_LLM_MODEL 没设），'
        + '退回 pi 自己的默认解析；若全局配置是空的，agent 将一直不发起请求');
    }

    const services = await createAgentSessionServices({
      cwd,
      agentDir: dir,
      ...extras,
      resourceLoaderOptions: {
        additionalSkillPaths: [SKILLS_DIR],
        extensionFactories,
      },
    });
    const { session, extensionsResult } = await createAgentSessionFromServices({
      services,
      sessionManager: sm,
      noTools: 'builtin',
      customTools,
      // **模型必须显式交给会话**。少了这一行，会话会退回「挑第一个可用的
      // 模型」——而内置的 minimax/MiniMax-M2.7 在默认表里排前面，
      // 拿到的却是给 openai-completions 端点的密钥，每次请求 401，
      // 而进程退出码还是 0：失败长得像成功。
      ...(extras.model ? { model: extras.model } : {}),
    });
    return { session, extensionsResult, services, diagnostics: services.diagnostics };
  };

  const runtime = await createAgentSessionRuntime(createRuntime, {
    cwd: WORK_DIR,
    agentDir,
    sessionManager,
  });

  for (const d of runtime.diagnostics || []) log(`会话诊断：${d.type} ${d.message}`);
  // 模型掉回默认值时 modelFallbackMessage 是唯一的线索，之前一直没打。
  if (runtime.modelFallbackMessage) log(`模型回退：${runtime.modelFallbackMessage}`);
  try {
    const m = runtime.session?.model;
    if (m) log(`当前模型：${m.provider ?? '?'}/${m.id ?? '?'}（api=${m.api ?? '?'}）`);
  } catch { /* 拿不到就跳过：诊断信息不该反过来把 agent 弄挂 */ }

  // ---- 复核模式：跑一轮就退出 ----
  //
  // 复核不是任务：没有任务号、没有令牌、没有回传对象，也没有人在等一个
  // 「完成」信号。它就是「把 brief 交给模型跑一轮，跑完把结果写进文件」。
  if (REVIEW_MODE) {
    const briefPath = process.env.PROTFORGE_WEB_REVIEW_BRIEF_PATH || '';
    let brief = '';
    try {
      brief = briefPath ? readFileSync(briefPath, 'utf-8') : '';
    } catch (e) {
      log(`读不到复核提示词 ${briefPath}：${e?.message ?? e}`);
    }
    if (!brief.trim()) {
      log('复核提示词是空的，没什么可整理，退出');
      process.exit(2);
    }
    log(`进入复核模式（${REVIEW_KIND}），提示词 ${briefPath}，工作目录 ${WORK_DIR}`);
    /* **把会话事件打出来**：复核进程是一次性的，跑完就退出——
     * 它没有页面、没有流、也没有人盯着。所以「它到底干了什么」只剩日志这一条路。
     * 不打的话，「模型请求失败」与「模型干完了」在日志里长得一模一样。 */
    const unsubscribe = runtime.session.subscribe((ev) => {
      try {
        const type = ev && ev.type ? ev.type : '?';
        // **message_update 是逐字增量**：每次模型吐字都来一次，打出来就是几百行。
        // 只记它的类型，不记内容；真正要看的是 message_end 那一坨。
        if (type === 'message_update') {
          updateCount += 1;
          if (updateCount % 50 === 0) log(`事件 message_update ×${updateCount}`);
          return;
        }
        const body = JSON.stringify(ev, (k, v) => (k === 'content' ? undefined : v));
        log(`事件 ${type}：${String(body).slice(0, 700)}`);
      } catch (e) {
        log(`事件打不出来：${e?.message ?? e}`);
      }
    });
    let updateCount = 0;
    let out;
    try {
      out = await runtime.session.prompt(brief);
    } catch (e) {
      log(`复核这一轮抛错了：${e?.message ?? e}`);
      if (e?.stack) log(String(e.stack).split('\n').slice(0, 8).join('\n'));
      throw e;
    }
    // **复核跑完到底干了什么**：只打「跑完了」的话，「它什么都没干」
    // 与「它干完了」在日志里长得一模一样——而前者是最难查的那种故障。
    try {
      const text = typeof out === 'string' ? out
        : (out && typeof out.text === 'string' ? out.text : '');
      log(`复核跑完一轮，回复 ${String(text).length} 字：`
        + String(text).slice(0, 400).replace(/\s+/g, ' '));
      if (out && Array.isArray(out.messages)) {
        log(`消息条数：${out.messages.length}`);
      }
    } catch (e) {
      log(`复核回复取不出来：${e?.message ?? e}`);
    }
    log('复核跑完一轮，退出');
    try { unsubscribe(); } catch { /* 退订失败不影响结果 */ }
    return;
  }

  log(`进入 RPC 模式，任务 ${TASK_ID}，工作目录 ${WORK_DIR}`);

  await runRpcMode(runtime);
}

/* 刻意**没有**「把命令原文播报成当前步骤」的钩子。

早先这里有个 spawnHook，每条命令执行前把命令塞进 step，心跳再重复它。于是网页上
最显眼的那一行显示的是「正在执行：cd <数据盘根>/work/13 && python
analyze_bdnf.py 2>&1 | tail」——被截断到 70 字符，经常断在半截路径上。

命令原文本来就逐条落在 audit_log 里（带退出码和耗时），要看随时查得到，不必再占着
界面的主位。step 是**给人读的进度叙事**，审计是**给查的原始记录**，两者不该抢同一行。

心跳重复的是模型最后一次 report_step 的那句话，所以长命令跑着的时候，界面上留着的
是「正在搜寻蛋白质序列」这类话，而不是一条 shell 命令。代价是：模型长时间不说话时，
界面上那句话不会变——那是模型该补 report_step 的理由，平台规则里写死了。 */

/** 把平台规则追加到 pi 自己生成的 system prompt 之后。 */
function platformRulesExtension() {
  const rules = REVIEW_KIND === 'task' ? REVIEW_RULES_TASK
    : (REVIEW_KIND === 'memory' ? REVIEW_RULES_MEMORY
    : (REVIEW_KIND === 'runtime' ? REVIEW_RULES_RUNTIME : PLATFORM_RULES));
  return (pi) => {
    pi.on('before_agent_start', async (event) => {
      const base = event?.systemPrompt ?? '';
      return { systemPrompt: `${base}${rules}` };
    });
  };
}

main().catch((e) => {
  console.error('[agent] 致命错误', e);
  process.exit(1);
});
