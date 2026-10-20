/**
 * `..` 上跳绕过专项（node:test，与 guards.test.mjs 分开跑）。
 *
 * 为什么单独一个文件
 * ------------------
 * 守卫的路径规范化原先只做分隔符替换与盘符映射，**不消解 `..`**。于是
 * `<ProtForge 根>\..\..\Users\<user>\.ssh\id_rsa` 规范化后是
 * `/mnt/e/protforge/../../users/...`，`startsWith('/mnt/e/protforge/')` 为真 -> 放行。
 *
 * Python 侧的 `guards.py` 用 `Path.resolve()` 做了同一件事，两边行为不一致——
 * 同一个路径在一个进程里被拦、在另一个里放行，比单边有洞更难查。
 *
 * 这类"规范化不彻底"的问题不会被现有测试抓到：正向用例给的是规范路径，
 * 反向用例给的是明显越界的路径（`C:\Windows`）。只有**先落在允许根里、
 * 再用 `..` 爬出去**这一种形态才是真的绕过。
 *
 * 为什么不用 `type <path>` 测
 * -------------------------
 * 守卫只对**会改变文件系统或以文件为参数**的命令判定路径，判定点只有五处：
 * 删除/移动类命令的操作数、重定向的目标、被执行脚本的路径、docker 挂载宿主
 * 路径、以及文件工具（read/write）的 confine()。
 * `type` / `cat` / `head` 这类**只读**命令的操作数一律不做路径判定——agent
 * 需要读允许根之外的输入文件，这是刻意留的口子。
 * 所以拿 `type` 写用例，测的其实是"`type` 根本不被管"，
 * 通过或失败都跟消解逻辑无关，是恒真断言。
 * 下面每条用例都落在上面五处真实判定点上。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { checkCommand, canonPath, createGuardedReadOperations } from './guards.mjs';

// 守卫的「ProtForge 自己的地盘」允许根是从 PROTFORGE_DATA 推出来的
// （服务端注给 agent 的那个，指向 <ProtForge 根>\data）。
// 这里显式给一份：用例里的路径都落在它下面。
process.env.PROTFORGE_DATA = 'E:\\protforge\\data';


const WORK_DIR = 'E:\\testbed\\data\\work\\task-1';
const OPTS = { workDir: WORK_DIR };

const deny = (cmd, why) => {
  const r = checkCommand(cmd, OPTS);
  assert.equal(r.allowed, false, `期望拒绝但被放行：${cmd}\n（${why || ''}）`);
  assert.ok(typeof r.reason === 'string' && r.reason.length > 0,
    `拒绝时必须给出理由：${cmd}`);
};

const allow = (cmd) => {
  const r = checkCommand(cmd, OPTS);
  assert.equal(r.allowed, true, `期望放行但被拒：${cmd}\n理由：${r.reason}`);
};

/** 起点落在允许根里，`..` 爬出去之后落在允许集合之外——这才是真的绕过形态。 */
const FROM_ALLOWED_ROOT = 'E:\\protforge\\..\\..\\Users\\someone\\.ssh\\id_rsa';
/** 起点是工作目录本身，三级上跳出数据根。 */
const FROM_WORKDIR = 'E:\\testbed\\data\\work\\task-1\\..\\..\\..\\web.db';

// ------------------------------------------------- 1. 删除/移动类命令的操作数

test('rm 的操作数用 .. 从只读区爬出去被拒', () => {
  deny(`rm ${FROM_ALLOWED_ROOT}`, '借允许根 E:\\protforge 起跳');
});

test('rm 的操作数用 .. 从工作目录爬出去被拒', () => {
  deny(`rm ${FROM_WORKDIR}`, '三级上跳到数据根之外');
});

test('cp 的目标用 .. 上跳同样被拒（源在允许根内也一样）', () => {
  // cp/mv 只判目标不判源，所以逃逸路径必须放在最后——这正是容易写错的位置。
  deny('cp E:/protforge/data/inputs/1ubq.pdb E:/testbed/data/work/task-1/../../../Windows/System32/x',
    '目标是上跳出去的工作区外路径');
});

test('正斜杠写法与反斜杠写法一致被拒', () => {
  deny('rm E:/protforge/../../Users/someone/.ssh/id_rsa', 'WSL 风格的正斜杠路径');
});

test('/e/... 的 git 风格写法同样被消解', () => {
  deny('rm /e/protforge/../../Users/someone/.ssh/id_rsa', '映射前的 /e/ 写法');
});

// ------------------------------------------------------- 2. 重定向的目标位置

test('重定向目标用 .. 上跳被拒', () => {
  deny('cat brief.md > E:/protforge/../../Windows/Temp/pwned', '写在允许根之外');
});

// --------------------------------------------------- 3. 被执行脚本的文件路径

test('被执行的脚本路径用 .. 上跳被拒', () => {
  deny(`bash ${FROM_ALLOWED_ROOT}`, '脚本路径逃出允许根');
});

// --------------------------------------------------------- 4. docker 挂载宿主

test('docker -v 挂载用 .. 上跳出 E:\\protforge 被拒', () => {
  deny('docker run --rm -v E:/protforge/../../etc:/x oih/img:esmfold2',
    'dockerRoots 只允许 E:\\protforge，上跳后落在盘外');
});

// ------------------------------------------- 5. 文件工具 confine（read/write）

test('read 工具的路径用 .. 上跳被拒', async () => {
  // 传一个假的 base：confine() 必须在调用内层之前就抛出来，
  // 否则这条用例会去真的读磁盘。
  // 注意工厂函数本身是 async，而 confine 是**同步抛错**（不是 rejected promise），
  // 所以这里用 assert.throws 而不是 assert.rejects。
  const ops = await createGuardedReadOperations(OPTS, {
    readFile: async () => { throw new Error('不该走到这里'); },
    access: async () => { throw new Error('不该走到这里'); },
  });
  assert.throws(
    () => ops.readFile(FROM_ALLOWED_ROOT),
    /被安全守卫拒绝/,
    `confine 应当拦下：${FROM_ALLOWED_ROOT}`,
  );
});

test('read 工具在工作目录内的路径放行（confine 不能误杀）', async () => {
  let seen = null;
  const ops = await createGuardedReadOperations(OPTS, {
    readFile: async (p) => { seen = p; return 'ok'; },
    access: async () => true,
  });
  assert.equal(await ops.readFile(`${WORK_DIR}\\sub\\..\\brief.md`), 'ok');
  assert.equal(seen, `${WORK_DIR}\\sub\\..\\brief.md`,
    'confine 只做判定，不该顺手改写传进来的路径');
});

// ------------------------------------------------------ 不能过度拒绝（回归护栏）

test('消解后仍落在工作目录内的路径放行', () => {
  // 消解 `..` 不能把合法路径一起毙掉：task-1/sub/../brief.md 解析后就在工作目录里。
  allow('cp E:/protforge/data/inputs/1ubq.pdb E:/testbed/data/work/task-1/sub/../brief.md');
});

test('消解后仍落在只读区内的路径放行', () => {
  allow('rm E:/protforge/data/inputs/../inputs/1ubq.pdb');
});

test('消解后仍落在 dockerRoots 内的挂载放行', () => {
  allow('docker run --rm -v E:/protforge/data/../data/inputs:/in oih/img:esmfold2');
});

// ------------------------------------------------------ 规范化结果本身（根因）

test('canonPath 真的把 .. 消解掉了', () => {
  assert.equal(canonPath('E:\\protforge\\..\\..\\Users'), '/mnt/users');
  assert.equal(canonPath('E:/protforge/../../Users'), '/mnt/users');
  assert.equal(canonPath('/e/protforge/../../Users'), '/mnt/users');
  assert.equal(canonPath('E:\\testbed\\data\\work\\task-1\\..\\..\\..\\web.db'),
    '/mnt/e/testbed/web.db');
});

test('canonPath 消解后不得再含任何 .. 段', () => {
  // 回归证据：不做消解时 `/mnt/e/protforge/../../users` 仍然 startsWith('/mnt/e/protforge/')，
  // 这正是当初绕过成立的原因。任何残留的 `..` 段都意味着消解又退化了。
  for (const p of [
    'E:\\protforge\\..\\..\\Users',
    'E:/protforge/../../Users',
    '/e/protforge/../../Users',
    'E:\\protforge\\data\\..\\..\\..',
    'E:\\protforge\\.\\data\\..\\inputs',
  ]) {
    assert.ok(!canonPath(p).split('/').includes('..'), `残留 .. 段：${p}`);
  }
});

test('消解在盘符映射之后发生（顺序不能反）', () => {
  // 映射前消解：`<ProtForge 根>\..` -> `E:\` -> `/mnt/e`，看着对，但 `<ProtForge 根>\..\oih\x`
  // 这类路径会先被砍成 `/mnt/e/protforge/x` 而掩盖真实的重复段。
  // 断言的是「映射后再消解」的结果本身，别靠直觉判断。
  assert.equal(canonPath('E:\\protforge\\pf\\..\\data'), '/mnt/e/protforge/data');
  assert.equal(canonPath('E:\\protforge\\..'), '/mnt/e');
});
