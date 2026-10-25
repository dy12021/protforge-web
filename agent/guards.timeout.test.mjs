/**
 * 命令超时：每条命令都必须有上限，而且**上限是分层的**。
 *
 * 为什么要有这个（2026-10-24 实测的教训）：
 * pi 的 bash 工具的 timeout 参数**没有默认值**（工具说明原文：
 * "Timeout in seconds (optional, no default timeout)"），于是「模型忘了传」
 * 等于「这条命令可以跑到天荒地老」。真实后果：一条 `python
 * work/build_designed.py`（6126 原子 × 80 轮的 O(n²) 碰撞消解）
 * **跑了 4 小时 42 分**，把整行拖垮，还烧掉了整批的机时。
 *
 * 技能里其实早写了「单条命令不超过 10 分钟」（protforge-sandwich-antibody
 * 2026-10-22 提交），但那是**散文**——模型没遵守。**规矩要落在代码里。**
 *
 * 分层规则：
 * - 认得出是哪个工具 → 用**那个工具的额度**（大计算自然有更大的额度）；
 * - 认不出 → 通用上限（默认 1800 秒）。
 * 两者都被模型申请的数**夹住**（夹住不报错：模型已经拿到「被腰斩」的结果了）。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

// **环境变量必须在导入 guards.mjs 之前设好**：默认额度与通用上限是
// 模块加载时读一次的（const），工具预算表是第一次调用时读一次并缓存。
process.env.PROTFORGE_WEB_CMD_TIMEOUT_S = '600';
process.env.PROTFORGE_WEB_CMD_TIMEOUT_MAX_S = '1800';
process.env.PROTFORGE_WEB_TOOL_BUDGETS = JSON.stringify({
  'proteinmpnn.sh': 28800,      // 大计算：自己的额度
  'freesasa.sh': 600,          // 快工具：自己的额度
  'opendde': 7200,             // WSL 直调型：只有技能名能认出来
});

const { resolveTimeout, timeoutCapFor } = await import('./guards.mjs');

test('没申请 timeout 时给默认额度', () => {
  const t = resolveTimeout('ls -la', undefined);
  assert.equal(t.seconds, 600);
  assert.equal(t.cap, 1800);
  assert.equal(t.clamped, false);
});

test('模型申请超过通用上限 → 夹住', () => {
  const t = resolveTimeout('python work/build_designed.py', 7200);
  assert.equal(t.seconds, 1800, '认不出的自写脚本，4.5 小时那条的归宿是 30 分钟');
  assert.equal(t.clamped, true);
  assert.equal(t.requested, 7200);
});

test('认得出的工具用工具自己的额度', () => {
  const t = resolveTimeout('bash /x/skills/proteinmpnn/scripts/proteinmpnn.sh a.pdb', 7200);
  assert.equal(t.cap, 28800);
  assert.equal(t.seconds, 7200, '大计算申请 2 小时就给它 2 小时');
  assert.equal(t.clamped, false);
});

test('工具额度更小的时候夹到工具的额度', () => {
  const t = resolveTimeout('bash /x/skills/freesasa/scripts/freesasa.sh a.pdb', 7200);
  assert.equal(t.cap, 600);
  assert.equal(t.seconds, 600, '快工具不该被给 2 小时');
  assert.equal(t.clamped, true);
});

test('WSL 直调型技能按技能名认得出来', () => {
  const t = resolveTimeout('wsl.exe -d AF3Native -- bash -lc "/opendde_venv/bin/opendde x.fa"', 3600);
  assert.equal(t.cap, 7200);
  assert.equal(t.seconds, 3600);
});

test('工具名出现在单词中间不算认出来', () => {
  const t = resolveTimeout('python /x/myproteinmpnn.sh', 7200);
  assert.equal(t.cap, 1800, 'myproteinmpnn.sh 不是 proteinmpnn.sh');
  assert.equal(t.seconds, 1800);
});

test('没申请就按工具的额度给（不是一律 600）', () => {
  const t = resolveTimeout('bash /x/skills/proteinmpnn/scripts/proteinmpnn.sh a.pdb', undefined);
  assert.equal(t.seconds, 600, '默认还是 600：跑很久要显式申请');
  assert.equal(t.cap, 28800);
});

test('上限本身也是可配的（服务端注入）', () => {
  const t = resolveTimeout('ls', 99999);
  assert.ok(t.seconds <= 1800);
});

test('timeoutCapFor 认不出工具时给通用上限', () => {
  assert.equal(timeoutCapFor('echo hi'), 1800);
});
