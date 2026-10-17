/**
 * 与内部 API（8475）通信的回传层，以及注册给模型的两个自定义工具。
 *
 * 这两个工具是网页「实时可见」的全部来源：
 *   report_step —— 让用户看到「现在在干什么」
 *   finish      —— agent 认为任务完成的**唯一**信号，后端据此进入打包阶段
 *
 * 两条硬约束：
 * 1. report_step 必须**便宜到可以频繁调用**。蛋白结构预测动辄几十分钟，
 *    期间如果不再上报，网页上的进度会停在三小时前那句话上，看着像卡死。
 *    所以除了模型主动调用，还有一个心跳定时器在长工具运行期间续命。
 * 2. 回传失败**绝不能把 agent 弄崩**。回传是观测手段，不是任务的一部分；
 *    它挂了就让模型看到错误信息继续干活，而不是把整个任务判失败。
 */

import { defineTool } from '@earendil-works/pi-coding-agent';
import { Type } from 'typebox';

/** report_step 的文本上限，按**码点**算（不是 UTF-16 长度）。 */
const MAX_STEP_CODEPOINTS = 80;

/** 审计命令原文的上限，与服务端 AuditIn.command 的 max_length 对齐。 */
const MAX_COMMAND_CODEPOINTS = 8000;

/** 单次 POST 的硬超时。回传不能拖慢计算。 */
const POST_TIMEOUT_MS = 5000;

/** 心跳间隔：长工具运行期间保持「还活着」的可见性。 */
const HEARTBEAT_MS = 60_000;

function clip(text, max = MAX_STEP_CODEPOINTS) {
  const chars = Array.from(String(text ?? ''));
  if (chars.length <= max) return chars.join('');
  return `${chars.slice(0, Math.max(0, max - 1)).join('')}…`;
}

/**
 * 建一个回传器。
 * @param {{internal: string, taskId: string, token: string}} cfg
 */
export function createReporter(cfg) {
  const base = String(cfg.internal).replace(/\/+$/, '');
  const url = (kind) => `${base}/api/internal/agent/${encodeURIComponent(cfg.taskId)}/${kind}`;

  let current = null;      // 最近一次 step，心跳就是重复它
  let timer = null;
  let finished = false;

  function stopHeartbeat() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  async function post(kind, body) {
    try {
      const res = await fetch(url(kind), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agent-Token': cfg.token,
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(POST_TIMEOUT_MS),
      });
      if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e?.message ?? e).slice(0, 120) };
    }
  }

  return {
    /**
     * 上报一个步骤。连续相同的文本会被吞掉——模型在循环里很容易反复
     * 报同一句话，而每次都打一次 8475 只是白花连接。
     */
    async step(text) {
      if (finished) return { ok: false, error: '任务已结束，不再接受步骤上报' };
      const clipped = clip(text);
      if (!clipped) return { ok: false, error: '步骤文本为空' };
      current = clipped;
      return post('step', { text: clipped });
    },

    async finish(summary, artifacts) {
      finished = true;
      stopHeartbeat();
      return post('finish', {
        summary: String(summary ?? ''),
        artifacts: (artifacts ?? []).map((a) => String(a)).slice(0, 50),
      });
    },

    /**
     * 上报一条命令审计，落 audit_log（规格 §9.2 / §14 的"全量审计"）。
     *
     * 三条刻意的决定：
     *
     * 1. **不受 `finished` 闸约束。** step 必须在 finish 之后闭嘴，因为那是
     *    往时间线上加噪音；审计恰恰相反——命令是在 finish 之前跑完的，
     *    它的退出码和耗时只有在事后才有意义。在收口那一刻丢掉它们，
     *    等于把"最后跑的那几条命令"变成查无实据，正是审计要防的事。
     * 2. **失败只回 ok:false，不抛。** 与回传层同一条原则：记账失败不该让
     *    模型以为自己命令执行失败了，进而换个写法把同一件事再跑一遍。
     *    真正的风险是"审计没记上"，所以这里把失败原样交回给调用方去告警。
     * 3. **命令原文按码点截断到服务端上限**，只截一次、不改写内容——
     *    追责时要判断"里面有没有 rm"，被悄悄改过的原文比没有原文更糟。
     */
    async audit(kind, command, exitCode, elapsedMs) {
      const text = String(command ?? '');
      const chars = Array.from(text);
      return post('audit', {
        kind: String(kind ?? 'bash'),
        command: chars.length <= MAX_COMMAND_CODEPOINTS
          ? text
          : `${chars.slice(0, MAX_COMMAND_CODEPOINTS - 1).join('')}…`,
        exit_code: exitCode === undefined ? null : exitCode,
        elapsed_ms: elapsedMs === undefined ? null : elapsedMs,
      });
    },

    /**
     * 「当前在做什么」只由模型的 report_step 决定，**没有任何旁路**能改写它。
     *
     * 早先 run.mjs 挂着 spawnHook，每条命令执行前调 setCurrent 把命令原文塞进来，
     * 于是页面上那行显示的是被截到 70 字符的 shell 命令。现在那条旁路已经删掉：
     * 命令原文归 audit_log（带退出码和耗时），step 归叙事，两者不抢同一行。
     */
    startHeartbeat(intervalMs = HEARTBEAT_MS) {
      if (timer || finished) return;
      timer = setInterval(() => {
        if (finished || !current) return;
        void post('step', { text: current });
      }, intervalMs);
      // 不能让心跳把进程吊住。
      if (typeof timer.unref === 'function') timer.unref();
    },

    stop: stopHeartbeat,
  };
}

function toolResult(payload) {
  return {
    content: [{ type: 'text', text: JSON.stringify(payload) }],
    details: payload,
  };
}

/** report_step / finish 两个工具，供 createAgentSession 的 customTools 使用。 */
export function createReporterTools(reporter) {
  const reportStep = defineTool({
    name: 'report_step',
    label: 'report_step',
    description:
      '向网页回传当前正在做什么——这句话会显示在任务卡片最显眼的位置，给用户看。'
      + `不超过 ${MAX_STEP_CODEPOINTS} 个字。`
      + '要求：用户视角的动宾短语（例如「正在搜寻蛋白质序列」「正在计算表面面积」）；'
      + '**绝不要**贴命令、路径、管道、重定向或参数，那些是审计日志的内容，不是进度。'
      + '只在换阶段时上报，没换阶段就不用反复报——页面上那句话本来就会一直挂着。',
    parameters: Type.Object({
      text: Type.String({
        description:
          `一句话进度（用户视角的动宾短语，不超过 ${MAX_STEP_CODEPOINTS} 个字）。`
          + '例：「正在搜寻蛋白质序列」。'
          + '反面例：「正在执行：cd E:/... && python foo.py 2>&1 | tail」——这是命令，不是进度。',
      }),
    }),
    async execute(_callId, params) {
      const r = await reporter.step(params?.text);
      // 回传失败也要让模型知道，但它不该因此中断任务。
      return toolResult(r.ok
        ? { ok: true, text: clip(params?.text) }
        : { ok: false, error: r.error, hint: '进度回传失败，继续干活即可，不影响任务。' });
    },
  });

  const finish = defineTool({
    name: 'finish',
    label: 'finish',
    description:
      '声明计算流程已经完成。调用后后端会开始打包结果下载。'
      + '**只有真的跑完、产物已经落在工作目录里时才调用**；'
      + '调用它等于告诉后端不要再等了。',
    parameters: Type.Object({
      summary: Type.String({ description: '一句话总结这次计算做了什么、结论是什么' }),
      artifacts: Type.Optional(Type.Array(
        Type.String({ description: '产物的相对路径（相对工作目录）' }),
        { maxItems: 50 },
      )),
    }),
    async execute(_callId, params) {
      const r = await reporter.finish(params?.summary, params?.artifacts ?? []);
      return toolResult(r.ok
        ? { ok: true }
        : { ok: false, error: r.error });
    },
  });

  return [reportStep, finish];
}
