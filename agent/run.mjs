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
 *   PROTFORGE_SKILLS_DIR    技能目录，默认 ../protforge/skills
 *   MINIMAX_API_KEY / MINIMAX_API_HOST   联网检索能力；缺失时降级但仍能跑
 *   PROTORGE_AGENT_DIR     pi 的模型配置目录，由调度器在任务工作目录里物化。
 *                          不设就退回 getAgentDir()（用户全局），而本机那份是
 *                          空的 {}——症状是「agent 一直不吭声」而不是报错。
 *   PROTORGE_LLM_PROVIDER / PROTORGE_LLM_MODEL   显式指定模型。不指定就由 pi 自己
 *                          挑「第一个可用模型」，空配置下等于没有模型。
 */

import { mkdirSync } from 'node:fs';
import path from 'node:path';

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

const DEFAULT_SKILLS_DIR = '../protforge/skills';
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

const INTERNAL = required('PROTFORGE_WEB_INTERNAL');
const TASK_ID = required('PROTFORGE_TASK_ID');
const TOKEN = required('PROTFORGE_AGENT_TOKEN');
const WORK_DIR = path.resolve(required('PROTFORGE_WORK_DIR'));
const SKILLS_DIR = process.env.PROTFORGE_SKILLS_DIR || DEFAULT_SKILLS_DIR;

// ---------------------------------------------------------------- 日志

const log = (...a) => console.error('[agent]', ...a);

// spawnHook 是模块级函数，拿不到 main() 里的 reporter，用这个引用接过去。
let reporterRef = null;

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
- 命令会被安全守卫校验：解析不出来的、含 eval/source/.、以及作用在工作目录与 ../protforge
  之外路径上的删除/写入/挂载，一律拒绝并把理由返回给你。被拒就换写法，不要反复重试同一条。
- \`report_step\` 用于回传进度，**长步骤期间要周期性重复上报**，否则页面看起来像卡死。
- 只有在计算真的跑完、产物已经落在工作目录里之后，才调用 \`finish\`。
  没跑完就调 finish 会让后端直接去打包一堆半成品。`;
console.log = console.error; // stdout 归 RPC 独占

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

  const reporter = createReporter({ internal: INTERNAL, taskId: TASK_ID, token: TOKEN });
  reporterRef = reporter;
  reporter.startHeartbeat();
  await reporter.step('计算进程已启动，正在准备环境');

  // ---- 命令守卫 ----
  const guardOpts = {
    workDir: WORK_DIR,
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
    bash: { operations: guard.bash, spawnHook },
    read: { operations: guard.read },
    write: { operations: guard.write },
    edit: { operations: guard.edit },
  });
  // createCodingTools 只产出 read/bash/edit/write，PowerShell 要单独挂一个。
  codingTools.push(createPowerShellTool(WORK_DIR, { operations: guard.powershell, spawnHook }));
  const customTools = [...codingTools, ...createReporterTools(reporter)];

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
  log(`进入 RPC 模式，任务 ${TASK_ID}，工作目录 ${WORK_DIR}`);

  await runRpcMode(runtime);
}

/**
 * 每条 shell 命令执行前把它记成「当前在做什么」，心跳定时器就靠它续命。
 * 蛋白结构预测动辄几十分钟，期间模型是没有机会调用 report_step 的，
 * 所以这里必须由 agent 侧自己播报，否则网页进度会停在上一句话上。
 */
function spawnHook(context) {
  reporterRef?.setCurrent(`正在执行：${String(context.command).slice(0, 70)}`);
  return context;
}

/** 把平台规则追加到 pi 自己生成的 system prompt 之后。 */
function platformRulesExtension() {
  return (pi) => {
    pi.on('before_agent_start', async (event) => {
      const base = event?.systemPrompt ?? '';
      return { systemPrompt: `${base}${PLATFORM_RULES}` };
    });
  };
}

main().catch((e) => {
  console.error('[agent] 致命错误', e);
  process.exit(1);
});
