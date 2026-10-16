# ProtForge 计算平台 · 设计文档（v2）

> 状态：待审。本文是 brainstorming 阶段产出，未经批准不得进入实现。
> 日期：2026-10-16 ｜ v2 依据评审意见重写，v1 的「spawn 跑完即退」模型已废弃。

## 1. 问题与目标

ProtForge 现有 23 个技能全部是命令行工具（bash + `docker exec` / WSL venv），
使用门槛是「你得先跟 agent 对话、agent 才知道怎么拼参数」。两个问题：

1. **不可自助**——非本项目作者要用一次 `rfdiffusion`，得先学会这套命令行约定。
2. **不可见**——一次跑几小时的计算，中间发生了什么，命令行里只有滚动日志。

目标：把「提需求 → 计算 → 拿结果」变成普通网页操作，且计算过程每一步都可见。
**ProtForge 本身一行不改。**

非目标：计费、移动端适配、结果在线 3D 预览。

## 2. 核心职责

### 网页后端负责
任务账本、文件托管、队列调度、鉴权、对外 API。

### 网页后端**不**负责
不解析需求语义、不决定调哪个技能、不调用任何 ProtForge 脚本。
与 creative-panel 同一原则：**面板是账本不是执行器**。

### agent 负责
理解需求 → 决定技能链与参数 → 调用技能/读写文件/联网查资料 → 实时回写 → 收口。

## 3. 运行形态

两个**独立进程**，这是刻意的隔离：

| 服务 | 监听 | 默认端口 | 公网暴露 |
|---|---|---|---|
| 用户面板（主服务） | `0.0.0.0` | 8473（待定） | 是，反代转发 |
| 管理员面板 | `127.0.0.1` | 8474（待定） | **否** |
| agent | 无（子进程 stdin/stdout） | — | 否 |

管理员面板**不与主服务同进程，也不共用路由前缀**——它只绑 loopback，
反代在网络层就够不着它。v1 里「同进程 + 403 判定」的做法已被否决：
那是靠代码判断拦访问，运维层不该留下这条路径。本机已有 8471/8472 被占用。

两者共享同一个 SQLite 库（管理员写、用户读）。

## 4. agent 能力与边界

agent 是 **pi coding agent**（`@earendil-works/pi-coding-agent`），不是从零自写的循环。
凡 pi 原生有的能力一律用原生，不自己造轮子。

### 4.1 原生能力直接复用

| 能力 | pi 原生 API | 说明 |
|---|---|---|
| 技能装载 | `loadSkillsFromDir({ dir, source })` | 指向 ProtForge 的 skills 目录，23 个 SKILL.md 自动发现 |
| 技能进提示词 | `formatSkillsForPrompt(skills, 'read')` | XML 格式；带 `disable-model-invocation` 的不进提示词 |
| 文件与 shell 工具 | `createCodingTools(opts)` | read/write/edit/ls/grep/find/bash 一整套原生实现 |
| 联网搜索 | `createMcpExtension({ servers })` | 接 `uvx minimax-coding-plan-mcp` |
| 远程控制 | `runRpcMode()` + `RpcClient` | 管理员面板的控制通道，见 §8 |
| 事件流 | `AgentSessionEvent` 系列 | `turn_start` / `tool_execution_start` / `tool_result` / `message_end` |

`loadSkillsFromDir` 的发现规则：目录里有 SKILL.md 就当技能根、不再下钻；
否则收根目录下的 .md 子项；再递归找子目录里的 SKILL.md。
ProtForge 的布局是 `skills/protforge-*/SKILL.md`，正好命中。

### 4.2 「完全自主」的实现

用户要的是**完全自主**：调哪些技能、什么顺序、参数填什么，全由 agent 决定。
这里没有预设流水线、没有技能白名单、没有轮次上限。
agent 用的是 pi 的完整 bash 工具，因此它能写中间脚本、组织产物、自己调工具链——
这正是相比「网页直接调 CLI」的价值所在。

### 4.3 危险的规避靠配置层，不靠能力阉割

**不删 bash 工具**（删了 agent 就没法自己写脚本调 ProtForge，能力大减）。
改为在**操作层**注入受控实现。pi 的 `ToolsOptions` 每类工具都接受
`operations` 覆盖，我们替换掉默认实现：

```js
createCodingTools({
  bash:    { operations: guardedBashOps },   // 替换 exec 实现
  read:    { operations: guardedReadOps },
  write:   { operations: guardedWriteOps },
  edit:    { operations: guardedEditOps },
  ls:      { operations: guardedLsOps },
  powershell: { operations: guardedBashOps },
})
```

**为什么不直接用 `spawnHook`**：`BashSpawnHook` 的签名是
`(ctx) => ctx`，只能改写 command/cwd/env，**不能拒绝**。
而 `BashOperations.exec` 返回 `{ exitCode }`，可以在里面校验后
返回非零 + 说明文字。agent 看到的是「命令被拒绝：xxx」，能自己换条路，
而不是拿到一个空结果再原地打转。

校验规则：
- 命令首 token 必须命中允许清单（`bash`/`python`/`docker`/`git`/`cat`…）
- 拒绝 `rm -rf /`、`mkfs`、`dd`、`shutdown`、fork 炸弹、后台 `&` 逃逸
- 落地路径必须在 §4.4 的边界内

### 4.4 文件边界

| 区域 | 权限 |
|---|---|
| `data\work\<task_id>\` | 读写 |
| `../protforge/data\outputs\` | 只读 |
| `../protforge/data\inputs\` | 只读 |
| ProtForge 插件目录 | 只读 |

实现要点：**Windows 路径逃逸不能用 `startswith` 判**，必须先
`Path.resolve()` 再判，且只读区走同一套逻辑但只给读许可。
`startswith` 拦不住的形态：`C:foo` 驱动器相对路径（不以 `\` 开头但确实能逃）、
8.3 短名（`PROTFO~1`）、UNC 与 `\\?\` 前缀、junction/symlink 需 realpath 后判定。
大小写不敏感要一并处理。

### 4.5 超时（按技能分级）

v1 的统一 2 小时 / 8 小时被否决——freesasa 秒级，rfdiffusion 20 个设计要几小时，
一刀切要么把快工具掐死，要么把慢工具放跑。改为**按技能类别配表**，
表项可在配置里覆盖：

| 类别 | 单次超时 | 典型技能 |
|---|---|---|
| 宿主快工具 | 10 分钟 | freesasa, chemprop, discotope3, fpocket, p2rank |
| 对接 | 2 小时 | vina-gpu, gnina, autodock-gpu, diffdock |
| 结构预测 | 4 小时 | esmfold2, opendde, igfold, alphafold3 |
| 设计与 MD | 8 小时 | rfdiffusion, bindcraft, gromacs, proteinmpnn |

任务墙钟上限默认 **24 小时**。

pi 的 bash schema 自带 `timeout?: number`，所以模型自己也能调；
我们注入的 `BashOperations` 会把它与本表取**较小值**为准，防止模型自己放宽。

保留超时的依据是 `llm-quant` 的实测教训：上游挂起时是**无限等待而非报错**，
不设超时会静默烧穿整条任务。被杀的 `docker exec` 要连带清理容器内孤儿进程。

## 5. 任务状态机

固定、线性、不可改名：

```
queued ──> running ──> packaging ──> done
   │          │
   └──────────┴──> failed
```

- `failed` 是终点，不在主链上（与 creative-panel 把「卡住」做成布尔位同理）
- 用户侧状态只能走 `/advance`；**管理员可强制改状态**（明确要的能力），记 `actor=admin`
- `running` 期间额外维护 `step` 字段，见 §5.1

| 状态 | 含义 | 卡片上显示 |
|---|---|---|
| queued | 已提交，等 worker 接走 | 排队中 |
| running | agent 进程已起，正在编排与计算 | **当前步骤一句话** |
| packaging | 产物收拢、打 zip | 打包中 |
| done | 完成，可下载 | 完成 |
| failed | 中止 / 超时 / 报错 | 失败原因 |

### 5.1 当前步骤（running 期间）

v1 只写「正在编排与计算」，太粗——用户还是不知道发生什么。新增 `tasks.step`：

- **由 agent 自主写入**，经 RPC 事件上报（不额外造工具）
- **硬性上限 80 字符**，超了截断——这是为了让它能一行显示在卡片上，
  而不是塞进详情页。步骤是给人扫一眼的，不是日志
- 完整过程仍在时间线里，`step` 只是当前瞬时态
- agent 换了阶段就更新，长工具运行期间会周期性更新（心跳带一句进度）

## 6. 数据流

```
用户浏览器 ─POST /api/tasks─> 主服务 ─入队─> 调度器 ─spawn─> pi agent (runRpcMode, 常驻)
                                                                    │
        ┌──────────────────── RPC (stdin/stdout JSON) ───────────────┤
        │  prompt / steer / follow_up / abort / set_thinking_level   │
        │                                                            │
        │  agent 事件流 ──> 调度器 ──> SQLite events ──> 用户面板     │
        │                └─> tasks.step（当前步骤）                  │
        │                                                            │
        └── 管理员面板 (127.0.0.1:8474) ──────┘
                     观察 + steer + abort + set_thinking_level + set_model
```

**agent 进程在任务期间常驻**，不跑完就退。v1 的 spawn-即退模型被否决：
管理员中途 steer / 换模型 / 调思考档位，需要一个还活着的会话。

收口：agent 判定完成 → 调度器进入 packaging → 扫产物 → 打 zip → done。
中止：管理员 abort → RPC 发 `abort` → 宽限期后强杀进程树 → failed。

### 6.1 产物发现（双保险）

agent 的 bash 调用在 Node 侧，**Python 调度器看不到返回值**。
若只依赖 agent 上报，agent 崩溃时产物路径就永久丢失——用户等几小时最后什么都拿不到。

1. **约定路径（主）**：系统提示要求所有 ProtForge job 名以 `t{task_id}_` 开头。
   task_id 经 RPC 初始 prompt + 环境变量双重告知，agent 无从误填。
   产物目录从 bash 输出里的 `PROTFORGE_OUTPUT_DIR` 实时回写，用户能看见。
2. **兜底扫描（保命）**：agent 进程退出后**无条件**扫
   `../protforge/data\outputs\t{task_id}_*`，命中的目录全部收进 `artifacts`。

多工具接力（rfdiffusion → proteinmpnn → opendde）产生的多个目录都能捞回，
**即使 agent 在第二个工具跑完后崩溃**。没走到 finish 不是丢产物的理由。

## 7. 数据模型

### 7.1 users（简单用户管理）

v1 写的「不做用户体系」已被否决。实现保持简单，不引第三方库：

- `username` 唯一，`password_hash` + `salt`，`role ∈ (user, admin)`，`disabled` 标志
- 哈希用**标准库** `hashlib.pbkdf2_hmac('sha256', ...)` + 每用户随机 salt，
  不装 bcrypt/passlib（本机 pip 源已有限，且标准库够用）
- 登录后签发 token，存 `sessions` 表；**不实现**注册自助、
  密码找回、OAuth——用户由管理员在面板里创建

### 7.2 tasks

在 v1 基础上新增 `step`（§5.1）。`brief` 存需求**全文且不截断**
（agent 看不到全文就会自己编）。其余：`uploads_dir` / `output_dir` /
`package_path` / `summary` / `error` / `session_id`。

### 7.3 uploads / artifacts / events / sessions

- `uploads` — 只存路径，文件在 `data\uploads\<task_id>\`
- `artifacts` — 收口时扫 `output_dir` 填入
- `events` — 时间线，`kind ∈ (created,status,log,note,upload,package,error,edited,step)`，
  `actor ∈ (human,agent,admin,system)`
- `sessions` — 登录态与 agent 会话标识

`updated_at` 用触发器自动维护，`recursive_triggers=OFF` 是硬前提
（creative-panel 实测：去掉 WHEN 子句会连跨秒场景都递归）。

## 8. 管理员面板：直接控制 pi 原生能力

v1 的 `check_inbox()` 留言板被否决——那是二等公民式的干预。
正确做法是**接 pi 的 RPC 协议**，管理员面板就是一个远程 TUI。

`RpcCommand` 已验证包含：

| 命令 | 用途 |
|---|---|
| `prompt` / `steer` / `follow_up` | 发指令；`steer` 不打断当前 turn 实时插话 |
| `abort` | 中止当前执行 |
| `get_state` | 读会话状态 |
| `set_thinking_level` | 调思考档位（off/low/medium/high/xhigh/max） |
| `set_model` / `cycle_model` | 换模型 |
| `clear_queue` / `new_session` | 队列与会话管理 |

加上本项目自有的：**终止任务**、**强制改状态**、**失败重跑**、**追加干预指令**。

## 9. REST API

### 主服务（公网）
```
POST   /api/auth/login            登录换 token
GET    /api/me                    当前用户
POST   /api/tasks                 创建（含 brief / title / 上传文件）
GET    /api/tasks                 列表（按状态筛选）
GET    /api/tasks/{id}            详情（时间线 / step / 上传 / 产物）
POST   /api/tasks/{id}/abort      中止自己的任务
GET    /api/tasks/{id}/package    下载 zip
```

### 管理员服务（仅 127.0.0.1）
```
GET    /api/admin/tasks                 全部任务 + 队列 + worker 状态
POST   /api/admin/tasks/{id}/abort      终止
POST   /api/admin/tasks/{id}/status     强制改状态
POST   /api/admin/tasks/{id}/retry      失败重跑
POST   /api/admin/tasks/{id}/rpc        发 RPC 命令（steer/follow_up/abort/...）
GET    /api/admin/tasks/{id}/stream     agent 原始事件流
GET    /api/admin/users                 用户列表
POST   /api/admin/users                 建用户
PATCH  /api/admin/users/{id}            改角色 / 停用 / 重置密码
```

**契约**：所有 `detail` 恒为中文字符串（框架层错误也要翻成人话），与 creative-panel 一致。
CLI 与前端都直接展示它，混形态就没法写匹配逻辑。

## 10. 错误处理

| 场景 | 表现 |
|---|---|
| Docker 未运行 | bash 调用返回明确文案，让 agent 自己换路而不是瞎重试 |
| 脚本无 `PROTFORGE_STATUS:` 行 | **判为失败**，即使 exit 0（ProtForge 明文契约） |
| 危险命令被拦 | 返回非零 + 说明，agent 可见并自行绕行 |
| 单次工具超时 | 按 §4.5 表取值；回写 error 事件 |
| agent 进程崩溃 | 调度器捕获，标 failed，**兜底扫产物**，保留已回写的时间线 |
| RPC 断连 | 宽限期后按崩溃处理 |
| MCP server 启动失败 | 明确报「搜索能力未就绪」，不让 agent 误以为没资料 |
| zip 打包失败 | 停在 packaging 并报错，**不写 done** |

## 11. 目录结构

```
protforge-web\
├─ server\      app.py / db.py / store.py / dispatcher.py / auth.py / skills_index.py
├─ admin\       独立的管理员面板服务（app_admin.py）
├─ agent\       run.mjs / guards.mjs / package.json / (node_modules)
├─ web\         index.html / app.js / style.css        用户面板
├─ admin-ui\    index.html / app.js / style.css        管理员面板
├─ data\        web.db / uploads\ / work\ / packages\ / logs\
└─ docs\superpowers\specs\
```

`skills_index.py` 启动时扫描插件目录，用**真 YAML 解析器**读 frontmatter
生成技能清单与超时分级表。protforge 仓库自己就有 `test_frontmatter.py`，
踩过「裸标量里的 `: ` 让 PyYAML 抛 ScannerError、解析失败的技能被加载器
静默丢弃」的坑——不能用肉眼看。

## 12. 测试策略

- 后端：**pytest，全部走真实 HTTP**（临时端口 + `tmp_path` 临时库），
  绝不碰真实 `data\web.db`（沿用 creative-panel 纪律）
- **路径逃逸专项**：逐个覆盖 §4.4 列出的每种 Windows 形态，
  含 `C:foo`、8.3 短名、junction
- **bash 守卫专项**：确认危险命令返回非零而非静默放行；确认合法命令不受影响
- **step 截断**：80 字符边界
- **超时取小值**：模型传大 timeout 不得越过本表
- agent 工具层单测（mock exec）
- 端到端：**真跑一次 freesasa**（宿主 Python，秒级，不占 GPU），
  验证 queued→running→packaging→done 全链路、step 回写、zip 内容
- 前端无自动化测试（零 CDN 无构建的直接后果），改完用真浏览器看一眼

## 13. 不做的事（YAGNI）

- 不做自助注册 / 密码找回 / OAuth——用户由管理员创建
- 不做 GPU 感知的调度——技能不声明显存需求
- 不做结果在线 3D 预览
- 不做任务模板与预设流水线（用户明确要完全自主）
- 不做跨机器的 agent 断点续跑

## 14. 待确认的风险与假设

**假设**（未经确认，实现前需拍板）：

1. **端口 8473 / 8474** 只是避开本机已用端口的推断值。
2. 管理员面板的浏览器 UI 走独立前端（`admin-ui/`）还是复用 `web/` 的构建产物。

**风险**：

3. **24 小时墙钟上限对最长的工具链够不够**（rfdiffusion 大批量 + ESMFold2 full
   可能超）。届时调参还是拆任务，要实际跑过才知道。
4. **MCP server 首次 `uvx` 拉包**可能很慢或需要网络。实现时加启动期预热校验，
   失败要明确报「搜索能力未就绪」。
5. **Windows 路径逃逸与 bash 守卫是本项目最容易写错的两处**，测试要写足。
6. **23 个技能全部进提示词**的 token 成本需实测。若过长，考虑只放常用子集，
   其余靠 agent 用 read 按需打开。
7. `../protforge` 的路径来自 `scripts/local.env`，本项目**只读引用**，不修改该文件。

**已知的待清理项**：

8. `server\db.py` 是本设计之前抢跑写下的草稿：缺 `step` 列、
   `events.actor` 的 CHECK 缺 `admin`、缺 `users`/`sessions` 表。
   **以本文为准重写，不要把草稿当基线。**

## 15. 技术选型理由

| 选择 | 理由 |
|---|---|
| pi coding agent（非自写循环） | 用户已要求；原生有工具、技能装载、MCP、RPC、事件流，自写必然更差 |
| FastAPI + SQLite | 与 creative-panel 同构，零外部依赖 |
| 原生 JS + 零 CDN 前端 | 同上。改完刷新即生效，不需要构建链 |
| 自定义 `operations` 而非 `spawnHook` | spawnHook 不能拒绝，只能改写；受控实现才能真正拦下危险命令 |
| RPC 常驻而非 spawn 即退 | 管理员中途干预需要活着的会话 |
| HTTP 事件回写 | 实时性是核心诉求 |
| ProtForge 零改动 | 23 个技能各自维护、有独立发布节奏；本项目只当调用方 |
