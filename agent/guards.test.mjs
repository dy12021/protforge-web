/**
 * 命令守卫单测（node:test）
 *
 * 守卫的策略是「解析失败即拒」。这条策略有一个必须被测出来的副作用：
 * 如果 AST 解析器覆盖不全，合法命令会被误拒，agent 直接卡死。
 * 因此这里正向用例（必须放行）和反向用例（必须拒绝）同等重要——
 * 只写反向用例的话，一个「把所有命令都拒了」的实现也能让测试全绿。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { checkCommand } from './guards.mjs';

// 与 run.mjs 保持一致的工作目录假设（数据盘 + 其 WSL 视图）。
const WORK_DIR = 'data\\work\\task-1';
const OPTS = { workDir: WORK_DIR };

const allow = (cmd) => {
  const r = checkCommand(cmd, OPTS);
  assert.equal(r.allowed, true, `期望放行但被拒：${cmd}\n理由：${r.reason}`);
};

const deny = (cmd) => {
  const r = checkCommand(cmd, OPTS);
  assert.equal(r.allowed, false, `期望拒绝但被放行：${cmd}`);
  assert.ok(typeof r.reason === 'string' && r.reason.length > 0, `拒绝时必须给出理由：${cmd}`);
};

// ---------------------------------------------------------------- 正向用例

test('allows harmless command', () => {
  allow('ls -la');
});

test('allows harmless pipeline and chaining', () => {
  allow('mkdir -p out && cd out && ls -la');
  allow('cat job.json | python -m json.tool | head -50');
  allow('grep -n "pLDDT" report.txt || echo "not found"');
});

test('allows command substitution whose body is harmless', () => {
  allow('echo "today is $(date)"');
  allow('echo `date`');
  allow('ls $(pwd)');
  allow('diff <(cat a.txt) <(cat b.txt)');
});

test('allows bash -c whose body is harmless (recursion must not over-block)', () => {
  allow('bash -c "ls -la"');
  allow("bash -c 'echo hi'");
  allow('bash -c "mkdir -p /esmfold2/bin && cp \'/x/fold.py\' /esmfold2/bin/fold.py"');
});

test('allows wsl.exe wrapper whose inner command is harmless', () => {
  allow('wsl.exe -d AF3Native bash -c "ls /esmfold2"');
  allow('wsl.exe -d AF3Native -- bash /esmfold2/bin/fold.py job.json');
});

test('allows xargs whose command is harmless', () => {
  allow('ls *.pdb | xargs -I{} echo {}');
});

test('allows loops with variable expansion in argument position', () => {
  // 注意：for/while/if 的语句体在当前解析器转译下读不出来（见 guards.mjs【缺口 5】），
  // 所以 for 循环是被拒绝的，见下面 rejects-clause 那一组。这里只钉住
  // 「变量出现在参数位置不影响判定」这一条仍然成立。
  allow('echo $HOME && ls $PWD');
  allow('cd out && ls -la && echo done');
});

test('allows the real ProtForge skill invocation forms', () => {
  // 取自 ../protforge/skills 的实际 SKILL.md 示例
  allow('python "${PLUGIN_DIR}/skills/protforge-alphafold-db/scripts/afdb.py" P69905 --out "$PROTFORGE_DATA/outputs/P69905_afdb"');
  allow('wsl.exe -d AF3Native -- bash -c "mkdir -p /esmfold2/bin && cp \'/w/fold.py\' /esmfold2/bin/fold.py"');
  allow('wsl.exe -d AF3Native -- bash /esmfold2/bin/design_scFv.py /esmfold2/jobs/job.json');
  allow('HF_ENDPOINT=https://hf-mirror.com wsl.exe -d AF3Native -- bash /esmfold2/bin/fold.py job.json');
  allow('rm -rf ./out && rm -rf build');
  allow('mkdir -p /esmfold2/jobs');
});

test('allows writes inside the declared write set', () => {
  allow('echo hi > out.log');
  allow('cat a.txt > data\\work\\task-1\\out.txt');
  allow('cp script.py /esmfold2/bin/script.py');
});

test('allows docker mounts inside ../protforge', () => {
  allow('docker run -v ../protforge\\models:/models img');
  allow('docker run --rm -v /mnt/e/oih/models:/models -w /models img python run.py');
});

test('python -c content is a documented gap and is allowed', () => {
  // 已知缺口：守卫无法静态检查内联解释器的字符串内容。
  // 这里只确认「允许在当前上下文运行内联解释器」，不检查内容。
  allow('python -c "print(1)"');
  allow("python -c \"import os; os.system('rm -rf /')\"");
});

// ---------------------------------------------------------------- 反向用例

test('rejects rm of a root or system path', () => {
  deny('rm -rf /');
  deny('rm -rf /etc');
  deny('rm -rf ~');
  deny('rm -rf C:\\Windows\\System32');
  deny('rmdir /s /q C:\\');
});

test('rejects bash -c rm -rf (recursion required)', () => {
  deny('bash -c "rm -rf /"');
  deny("bash -c 'rm -rf /'");
  deny('sh -c "rm -rf /"');
});

test('rejects wsl ... bash -c rm -rf (recursion required)', () => {
  deny('wsl.exe -d AF3Native bash -c "rm -rf /"');
  deny('wsl.exe -d AF3Native -- bash -c "rm -rf /"');
});

test('rejects eval / source / . (static analysis impossible)', () => {
  deny('eval "rm -rf /"');
  deny('eval ls');
  deny('source ./evil.sh');
  deny('. ./evil.sh');
  deny('bash -c "source /tmp/x.sh"');
});

test('rejects nested substitution carrying a dangerous command', () => {
  deny('echo $(rm -rf /)');
  deny('echo `rm -rf /`');
  deny('cat <(rm -rf /)');
  deny('echo $(bash -c "eval ls")');
  deny('echo "$(wsl.exe -d AF3Native bash -c \'rm -rf /\')"');
});

test('rejects xargs carrying a dangerous command', () => {
  deny('ls | xargs rm -rf /');
  deny('echo x | xargs -I{} bash -c "rm -rf {}"');
});

test('rejects danger hidden behind wrapper commands', () => {
  deny('sudo rm -rf /');
  deny('nohup bash -c "rm -rf /"');
  deny('env FOO=1 bash -c "rm -rf /"');
  deny('timeout 5 sh -c "rm -rf /"');
});

test('rejects dynamic command names (cannot be decided statically)', () => {
  deny('$CMD ls');
  deny('${EVIL} --version');
});

test('rejects write redirects outside the declared write set', () => {
  deny('echo x > C:\\Windows\\evil.txt');
  deny('echo x > /usr/local/bin/evil');
});

test('rejects docker privileged and dangerous mounts', () => {
  deny('docker run --privileged img');
  deny('docker run -v /:/host img');
  deny('docker run -v C:\\Users\\<user>:/mnt img');
  deny('docker run --privileged -v /:/host alpine sh');
});

test('rejects parse failures (never fail open)', () => {
  deny('echo $(');
  deny('rm -rf / &&');
  deny('if then');
  deny('echo "unterminated');
  deny('for x in ; do');
});

test('rejects arithmetic expansion (cannot be walked safely)', () => {
  // 算术展开里可以藏命令替换，本守卫选择直接拒绝而不是猜。
  deny('echo $(( $(rm -rf /) ))');
});

test('rejects control-flow clauses whose body the parser cannot expose', () => {
  // 守卫看不见 if/for/while/let 里面跑的是什么，所以一律拒绝，而不是当成空。
  // 这是「失败即拒」的直接后果，见 guards.mjs【缺口 5】。
  for (const cmd of [
    'if true; then rm -rf /; fi',
    'for f in *; do echo $f; done',
    'while true; do echo x; done',
    'until false; do echo x; done',
    'let x=$((1+2)); echo $x',
    'declare -a xs',
  ]) {
    const r = checkCommand(cmd, OPTS);
    assert.equal(r.allowed, false, `控制流语句应当被拒绝：${cmd}`);
    assert.match(r.reason, /无法读取|无法确认/, `拒绝理由要说明原因：${cmd} -> ${r.reason}`);
  }
});

test('still analyzes case clauses (body IS readable)', () => {
  // case 的分支列表能读到，所以它必须真的被检查，而不是连坐拒绝。
  deny('case "$x" in a) rm -rf / ;; esac');
  allow('case "$x" in a) ls ;; esac');
});

test('rejects empty and whitespace-only commands', () => {
  deny('');
  deny('   ');
});

// ---------------------------------------------------------------- 不退化检查

test('guard is not a blanket rejecter', () => {
  // 反向用例全绿并不代表守卫有用——如果它把一切都拒了，正向用例会先炸。
  // 这里再显式钉死一批无害命令，确保实现保持可用。
  const harmless = [
    'ls',
    'echo hello',
    'pwd',
    'cat job.json',
    'python run.py',
    'wsl.exe -d AF3Native -- bash /esmfold2/bin/fold.py job.json',
    'docker ps',
    'docker run -v ../protforge\\models:/models img python run.py',
  ];
  const denied = harmless.filter((c) => !checkCommand(c, OPTS).allowed);
  assert.deepEqual(denied, [], `这些无害命令被误拒：${denied.join(' | ')}`);
});
