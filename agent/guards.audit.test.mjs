/**
 * 命令审计接线测试（node:test）
 *
 * 背景：规格 §14 明写「所有 bash 调用全量记入审计日志，管理员面板可查、可导出」。
 * 这条承诺曾经**完全没有实现**——`createGuardedBashOperations` 判定完就执行，
 * 不留任何痕迹；agent 侧也没有任何上报；服务端连收审计的端点都没有。
 * 于是 audit_log 表恒为空、管理端审计页签恒显示"没有匹配的审计记录"，
 * 而这一切**不报任何错**，每个单模块测试都是绿的。
 *
 * 这组测试钉的就是那条缺失的接线。它断言的不是"守卫还会放行/拒绝"，
 * 那是 guards.test.mjs 的活；这里断言的是**每一次判定都留下了证据**。
 *
 * 用假的 base operations（`createGuardedBashOperations(opts, base)` 的第二个参数）
 * 而不是真起 shell：审计行为与命令本身无关，跑真命令只会让测试变慢而不变强。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { createGuardedBashOperations } from './guards.mjs';

const WORK_DIR = 'data\\work\\task-1';

/** 收集审计行的假回传器。 */
function recorder() {
  const rows = [];
  return {
    rows,
    onAudit: (row) => { rows.push(row); },
  };
}

/** 假 shell：记录跑过的命令，返回可配置的退出码。 */
function fakeShell({ exitCode = 0, throws = null } = {}) {
  const ran = [];
  return {
    ran,
    operations: {
      async exec(command) {
        ran.push(command);
        if (throws) throw throws;
        return { output: 'ok', exitCode, cancelled: false, truncated: false };
      },
    },
  };
}

// ---------------------------------------------------------------- 放行的命令要留痕

test('an allowed command produces exactly one audit row with its exit code', async () => {
  const { rows, onAudit } = recorder();
  const shell = fakeShell({ exitCode: 0 });
  const bash = createGuardedBashOperations({ workDir: WORK_DIR, onAudit }, shell.operations);

  await bash.exec('ls -la', WORK_DIR, {});

  assert.equal(shell.ran.length, 1, '命令确实执行了');
  assert.equal(rows.length, 1, `放行的命令必须留下审计行，实际 ${rows.length} 行`);
  assert.equal(rows[0].kind, 'bash');
  assert.equal(rows[0].command, 'ls -la', '命令原文必须一字不改');
  assert.equal(rows[0].exit_code, 0);
  assert.ok(Number.isInteger(rows[0].elapsed_ms) && rows[0].elapsed_ms >= 0,
    `耗时必须是毫秒整数，实际 ${rows[0].elapsed_ms}`);
});

test('a non-zero exit is recorded as-is, not normalised to zero', async () => {
  const { rows, onAudit } = recorder();
  const shell = fakeShell({ exitCode: 127 });
  const bash = createGuardedBashOperations({ workDir: WORK_DIR, onAudit }, shell.operations);

  await bash.exec('fold.py job.json', WORK_DIR, {});

  assert.equal(rows.length, 1);
  assert.equal(rows[0].exit_code, 127,
    '退出码是「非零退出」过滤器的依据，归零等于把失败记录洗成成功');
});

// ---------------------------------------------------------------- 被拦下的命令更要留痕

test('a denied command is audited even though it never ran', async () => {
  const { rows, onAudit } = recorder();
  const shell = fakeShell();
  const bash = createGuardedBashOperations({ workDir: WORK_DIR, onAudit }, shell.operations);

  await assert.rejects(() => bash.exec('rm -rf /', WORK_DIR, {}), /安全守卫拒绝/);

  assert.equal(shell.ran.length, 0, '被拒的命令绝不能真的执行');
  assert.equal(rows.length, 1,
    `被守卫拦下的命令必须留痕——它恰恰是事后追责最想看的一行，实际 ${rows.length} 行`);
  assert.equal(rows[0].kind, 'bash_denied');
  assert.equal(rows[0].exit_code, null, '从没执行过就没有退出码，不能编一个出来');
});

test('the denied row keeps the command verbatim on its first line', async () => {
  const { rows, onAudit } = recorder();
  const bash = createGuardedBashOperations({ workDir: WORK_DIR, onAudit }, fakeShell().operations);

  const CMD = 'bash -c "rm -rf /"';
  await assert.rejects(() => bash.exec(CMD, WORK_DIR, {}), /安全守卫拒绝/);

  const firstLine = rows[0].command.split('\n')[0];
  assert.equal(firstLine, CMD,
    '命令原文必须原样留在第一行：判断「里面有没有 rm」靠的就是它，改写过的原文比没有原文更糟');
  assert.ok(rows[0].command.includes('被守卫拒绝'),
    '拒绝理由也要留下，否则这行只说明「有条命令没过」，过不去问为什么');
});

test('every denied command gets its own row', async () => {
  const { rows, onAudit } = recorder();
  const bash = createGuardedBashOperations({ workDir: WORK_DIR, onAudit }, fakeShell().operations);

  await assert.rejects(() => bash.exec('rm -rf /', WORK_DIR, {}), /安全守卫拒绝/);
  await assert.rejects(() => bash.exec('eval ls', WORK_DIR, {}), /安全守卫拒绝/);
  await assert.rejects(() => bash.exec('rm -rf ~', WORK_DIR, {}), /安全守卫拒绝/);

  assert.equal(rows.length, 3, '拦截了三次就得有三行，少一行就是漏了一次尝试');
  assert.ok(rows.every((r) => r.kind === 'bash_denied'));
});

// ---------------------------------------------------------------- 记账失败不许影响命令

test('an audit failure must not break an allowed command', async () => {
  // 这是最容易写反的一条：审计 POST 失败如果抛出去，模型会以为命令没跑成，
  // 于是换个写法把同一件事再跑一遍——既浪费又有风险。
  const shell = fakeShell({ exitCode: 0 });
  const bash = createGuardedBashOperations({
    workDir: WORK_DIR,
    onAudit: () => { throw new Error('审计服务 500'); },
  }, shell.operations);

  const out = await bash.exec('ls -la', WORK_DIR, {});
  assert.equal(out.exitCode, 0, '命令照常返回结果');
  assert.deepEqual(shell.ran, ['ls -la']);
});

test('an audit failure must not hide the guard rejection reason', async () => {
  const bash = createGuardedBashOperations({
    workDir: WORK_DIR,
    onAudit: () => { throw new Error('审计服务 500'); },
  }, fakeShell().operations);

  await assert.rejects(() => bash.exec('rm -rf /', WORK_DIR, {}),
    /安全守卫拒绝/,
    '守卫的拒绝理由是模型换写法的唯一依据，不能被记账失败顶掉');
});

test('an execution error is audited too', async () => {
  // 超时、被 kill、shell 起不来：命令跑了但没跑完。
  // 这种"跑了一阵然后没了"如果不留痕，事后完全查不到。
  const { rows, onAudit } = recorder();
  const shell = fakeShell({ throws: new Error('killed') });
  const bash = createGuardedBashOperations({ workDir: WORK_DIR, onAudit }, shell.operations);

  await assert.rejects(() => bash.exec('sleep 10000', WORK_DIR, {}), /killed/);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, 'bash');
  assert.equal(rows[0].exit_code, null, '没拿到退出码就报 null，不能编 0');
  assert.equal(rows[0].command, 'sleep 10000');
});

// ---------------------------------------------------------------- 没接审计也不能坏

test('the guard still works when no audit hook is configured', async () => {
  const shell = fakeShell({ exitCode: 0 });
  const bash = createGuardedBashOperations({ workDir: WORK_DIR }, shell.operations);

  const out = await bash.exec('ls -la', WORK_DIR, {});
  assert.equal(out.exitCode, 0);
  await assert.rejects(() => bash.exec('rm -rf /', WORK_DIR, {}), /安全守卫拒绝/);
});
// ---------------------------------------------------------------- 工作流行任务 · 产物落点

/* 这条规则钉的是提示词，不是代码。
 *
 * 背景很具体：`artifacts.discover()` 有三条发现路径——前缀命中、时间窗兜底、
 * 任务工作目录。其中**时间窗兜底命中的是 verified=0**，而打包器诚实地失败：
 * 产物没确认就不进结果包。落在 outputs 根又没有 t{任务号}_ 前缀的文件，
 * 正好落在这条最窄的路上，于是那一行以「没有可下载的产物」失败。
 *
 * 而工作流是**按行**跑的，一行失败不阻断整批：所以这不是一行的问题，
 * 批量场景下会一行接一行地全失败，**表里只显示 failed、不报任何错**。
 * 批量才有的放大效应——不写进提示词，模型没有任何理由知道要避开它。
 */
test("PLATFORM_RULES 明写工作流行任务的产物落点", async () => {
  const src = await readFile(new URL("./run.mjs", import.meta.url), "utf8");
  const start = src.indexOf("const PLATFORM_RULES = `");
  assert.ok(start > 0, "run.mjs 里找不到 PLATFORM_RULES");
  const rules = src.slice(start);
  const at = rules.indexOf("工作流任务");
  assert.ok(at >= 0, "提示词里没提工作流行任务");
  // 只取这一段的正文（到模板串结尾），不拿整份文件做匹配：
  // "工作目录" 这四个字本来就出现在沙箱那一条里，全篇包含等于没断言。
  const end = rules.indexOf("\\`;", at);
  const para = rules.slice(at, end > 0 ? end : rules.length);
  // 自证探针：取到的段落得真的是一段话。匹配错位置（比如切在"工作流任务"四个字
  // 的中间）时上面那些 includes 仍可能为真，这条会把那种情况挡住。
  assert.ok(para.length > 40, "取到的段落只有 " + para.length + " 字，多半匹配错位置了");
  assert.ok(para.includes("工作目录"), "没要求工作流行任务的产物落在工作目录");
  assert.ok(para.includes("t{你的任务号}_"), "没写清共享目录产物的 t{任务号}_ 前缀规则");
});
