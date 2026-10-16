# ProtForge 计算平台 · 设计文档（v4）

> 状态：待审。v4 依据用户初审 + 两轮 DeepSeek 设计评审重写。
> 日期：2026-10-16
>
> 版本轨迹：v1 spawn 即退 + 同进程 admin → v2 改用 pi 原生能力与 RPC →
> v3 补威胁模型与协议细节 → **v4 修正两个由前版引入的洞**：
> ① 内部端点的「127.0.0.1 判定」在有反代时失效（有反代时 `client.host`
> 恒为 127.0.0.1），改为**独立 loopback 端口 + token**；
> ② agent↔主服务的通道从未定义，补 `agent_token` 与内部端口。

## 0. 本版依据的实测结论

写设计前先跑了一遍验证，有三条结论改变了架构前提：

1. **ProtForge 没有统一的 runner 契约。** 23 个技能里：
   - **17 个**有 `.sh` runner（Docker 类为主），会打印 `PROTFORGE_STATUS:`
   - `alphafold-db` 是 `scripts/afdb.py`，宿主 Python 直跑
   - `opendde` / `af3-msa` / `esmfold2` 走 **`wsl.exe -d AF3Native`**，
     直接调 `/opendde_venv/bin/opendde`、`/alphafold3_venv/bin/python`，**不经过 bash runner**
   - `reglyco` 宿主进程，`overview` 是路由
2. **`PROTFORGE_STATUS:` 只在那 17 个 bash runner 里有意义**，
   不能当全局强制契约，缺失时不能直接判失败。
3. **数据根 `../protforge`**，venv `../protforge\venvs\oih\python.exe`，
   compose `../protforge\oih-platform\`（全走 legacy 旧名，靠 `_common.sh` 的 fallback 生效）。
   已实测跑通 `protforge-freesasa.sh 1ubq.pdb _websmoke` → `PROTFORGE_STATUS: OK`。

**因此：不设计统一的 `run_skill` 封装层**——没有统一契约可封装。
agent 拿 pi 原生 bash，自己按各技能 SKILL.md 的 Command 段拼命令。
这同时印证了「用原生工具、给足自主权」是唯一可行路线。

## 1. 问题与目标

ProtForge 的技能全是命令行与 WSL 调用，用一次 `rfdiffusion` 得先跟 agent 对话、
agent 才知道怎么拼参数；一次计算几小时，中间发生了什么只有滚动日志。

目标：把「提需求 → 计算 → 拿结果」变成普通网页操作，过程每一步可见。
**ProtForge 本身一行不改。**

## 2. 职责划分

### 网页后端
任务账本、文件托管、队列调度、鉴权、对外 API。**不**解析需求语义、
**不**决定调哪个技能、**不**执行任何 ProtForge 命令。

### agent
理解需求 → 按 SKILL.md 拼命令 → 调技能/读写文件/联网 → 实时回写 → 收口。

## 3. 运行形态

四个进程 / 两个监听端口：

| 服务 | 监听 | 默认端口 | 公网 |
|---|---|---|---|
| 用户 API（主服务） | `0.0.0.0` | 8473（待定） | 是，反代转发 |
| **内部 API（同一服务，不同端口）** | **`127.0.0.1`** | **8475（待定）** | **否，反代不转发** |
| 管理员面板 | `127.0.0.1` | 8474（待定） | 否 |
| agent | 子进程，stdin/stdout 由主服务持有 | — | 否 |

### 3.1 内部 API 为什么必须是独立端口（v3 的安全洞）

v3 写「内部端点只接受来自 127.0.0.1 的请求」。**这个判定在有反代时是失效的**：
反代通常就跑在本机，公网请求经反代转发后 `request.client.host` **同样是 127.0.0.1**。
靠 IP 判定，等于把内部通道暴露给公网——正是 v1 被否决的那个问题，
v3 绕了一圈又回到了原地。

因此：主服务**同时监听两个端口**。

- `0.0.0.0:8473` —— 只挂**用户 API**。反代只转发这个端口。
- `127.0.0.1:8475` —— 只挂**内部 API**（agent 上报、管理员面板转发 RPC）。
  物理上绑定 loopback，反代不转发，公网够不着。

「物理上够不着」而不是「判定你来自本机」——这才是 v1 否决同进程 403 时想要的东西。

> Windows 上 Starlette 对 UDS 支持不佳，故用独立 loopback 端口而非 UNIX domain socket。

### 3.2 内部 API 的鉴权

即便绑了 loopback，仍做令牌校验（防本机其他进程冒用）：

| 调用方 | 令牌 | 能访问 |
|---|---|---|
| agent | **一次性 `agent_token`**，调度器 spawn 时生成、经环境变量注入 | 仅 `/api/internal/agent/{自己的 task_id}/*` |
| 管理员面板 | 管理员登录后的 Bearer token | `/api/internal/admin/*` |

`agent_token` **与 task_id 绑定**：拿 A 任务的 token 调 B 任务的端点直接 403。
这需要专门测试用例（越权访问其他 task 必须被拒）。

agent 怎么拿到后端地址：`PROTFORGE_WEB_INTERNAL` 环境变量 + `PROTFORGE_TASK_ID` +
`PROTFORGE_AGENT_TOKEN`，全部由调度器在 spawn 时注入，不硬编码、不走命令行
（argv 在任务列表页可见，且会进进程命令行历史）。

## 4. 威胁模型（v2 缺失，v3 补）

**这一步要先说清楚，否则下面所有防护都是自欺欺人。**

使用者是本公司授权人员（需登录）。**防御目标不是恶意攻击者**——
agent 需要完整的 bash 才能自己写脚本调 ProtForge，这就必然意味着
「有 bash 就没有真正的沙箱」。任何声称能挡住蓄意绕过者的 bash 白名单都是假的。

因此分三层，各自有明确边界：

| 层 | 目标 | 手段 | 能挡什么 | 挡不住什么 |
|---|---|---|---|---|
| L1 | **防误操作**（主要目标） | 命令解析后逐条校验，拦 `rm -rf`、`mkfs`、`dd`、`docker run -v /:/host`、fork 炸弹 | agent 误解 brief 导致的意外破坏 | 刻意构造的绕过 |
| L2 | **防越界** | agent **文件工具**的路径守卫 | agent 误写工作目录外的文件 | bash 子进程内的写入（见 §6） |
| L3 | **防资源滥用** | 任务并发/时长/磁盘配额 | 单个任务吃满机器 | 多用户协同绕过配额 |

**明说的局限**：
- 不防御有意的绕过。一个能提交 brief 的人，可以通过自然语言诱导 agent 执行任意 bash。
- 这是「授权人员内部工具」定位下的合理取舍，不是对外公共服务。
- 因此所有 bash 调用**全量记入审计日志**（L1 之上再加一层事后追溯），
  管理员面板可查、可导出。
- 如果将来要对外开放，这三层全部作废，必须换成真沙箱（独立低权用户 / 容器 /
  受限网络出口）。这是独立子项目，不在本 spec 范围。

## 5. agent 能力

用 **pi coding agent**（`@earendil-works/pi-coding-agent`）原生能力，不自写循环。

| 能力 | pi 原生 API |
|---|---|
| 技能装载 | `loadSkillsFromDir({ dir, source })` → 23 个 SKILL.md 自动发现 |
| 技能进提示词 | `formatSkillsForPrompt(skills, 'read')` |
| 文件与 shell 工具 | `createCodingTools(opts)` |
| 联网搜索 | `createMcpExtension({ servers })` → `uvx minimax-coding-plan-mcp` |
| 远程控制 | `runRpcMode()` + `RpcClient` |
| 事件流 | `AgentSessionEvent` 系列 |

### 5.1 自定义的两个工具

`step` 与完成判定是本项目概念，pi 没有，所以自定义：

| 工具 | 作用 | 通道与鉴权 |
|---|---|---|
| `report_step(text)` | 上报当前正在做什么 | `POST 127.0.0.1:8475/api/internal/agent/{task_id}/step`，带 `agent_token` |
| `finish(summary, artifacts)` | agent 判定完成并收口 | `POST .../finish`，同上 |

两者**只走内部端口，绝不经过公网 API**（§3.1）。

`report_step` 后端行为：写 `tasks.step`，**按 Unicode 码点截断到 80 字符并加省略号**
（不是字节截断，否则会切坏 UTF-8）。该工具必须**便宜到可以频繁调用**——
agent 在长工具运行期间也要周期性更新，否则用户看到的步骤会停在三小时前。

### 5.2 完成判定协议

- **正常**：agent 调 `finish` → 进 `packaging` → 扫产物 → 打 zip → `done`
- **异常**：agent 进程退出但**没有** `finish` 记录 → 判异常结束，进 `failed`，
  **但仍执行 §8.1 的兜底产物扫描**
- 两者靠 `kind='finished'` 事件是否存在来区分

**不需要跨进程通知**：调度器是**主服务进程内的后台任务**（不是独立进程），
而 `finish` 打到的 8475 正是主服务自己的端口——请求处理器可以直接改状态、
直接唤醒调度器。这是把内部 API 挂在主服务上的额外好处：
agent→后端与调度器→agent 两条通道共用一个进程，不必引入 IPC 或注册中心。

### 5.3 打包是子进程

zip 一个 20 GB 目录会长时间占住一个线程，`zipfile` 是同步 IO，
放主服务里会拖慢所有请求（FastAPI 的 async 也救不了）。

因此打包**必须 fork 子进程**执行，进程内按文件数比例通过 `events` 上报进度，
由 SSE 推给用户。主服务全程不持有那个文件句柄。

## 6. 执行环境与权限模型

### 6.1 混合 shell（v2 遗漏，是评审 #14）

agent 的 bash 工具需要同时覆盖两条通道：

| 技能类别 | 调用方式 | 通道 |
|---|---|---|
| Docker 类（rfdiffusion/vina/gnina/proteinmpnn/pesto/…） | Git Bash 跑 `.sh`，脚本内 `docker exec` | Git Bash |
| WSL 类（esmfold2/opendde/af3-msa） | `wsl.exe -d AF3Native` + venv 内 CLI | WSL |
| 宿主类（freesasa/afdb/reglyco/freesasa） | `python.exe` 直跑 | 宿主 |

守卫必须在**三条通道上都生效**，只挡 Git Bash 等于没挡——`wsl.exe` 能直接
访问整个 ext4 文件系统，绕过所有 Windows 路径检查。

### 6.2 文件权限与 bash 权限是**两套**，必须分开

这是评审 #3 指出的直接矛盾，v2 把 `../protforge/data\outputs\` 设为只读，
但那些技能就是要往那儿写产物。

| 通道 | 守卫 | 权限 |
|---|---|---|
| **agent 的文件工具**（read/write/edit/ls） | 替换 `ReadOperations`/`WriteOperations`/… 的 `operations` | 工作区读写；`outputs`/`inputs`/插件目录**只读** |
| **bash 子进程** | 替换 `BashOperations.exec` 的**命令**校验 | **不套用文件只读限制**——它的工作就是写 outputs |

即：`../protforge/data\outputs\` 对 **agent 的 read/write 工具**只读，
对 **ProtForge 的 bash 进程**可写。两者是不同主体，不该用同一套规则。

**为什么不直接用 `spawnHook`**：签名 `(ctx) => ctx`，只能改写 command/cwd/env，
**不能拒绝**。`BashOperations.exec` 返回 `{ exitCode }`，
被拒时返回非零 + 说明文字，agent 看得见原因并自行绕行。

### 6.3 L1 命令守卫：解析策略必须明确

不能只查首 token——`bash -c "rm -rf /"`、`python -c "..."`、
`docker run -v /:/host`、`xargs`、管道、子 shell、变量展开都能绕过。
但「切分出简单命令逐条校验」这句话如果不定死解析方式，`guards.mjs`
会退化成一堆正则、行为不可预测。所以规定：

**解析器**：用 shell 语法解析器（`mvdan.cc/sh` 的 JS 移植，或 `tree-sitter-bash`）
把命令解析成 AST，再遍历每个可执行节点。**不用正则做安全判断**。

**递归规则**（这是分层绕过的关键）：

| 形式 | 处理 |
|---|---|
| `bash -c "..."` / `sh -c "..."` | **取出内层字符串递归解析** |
| `wsl.exe -d AF3Native bash -c "..."` | 解析到 `--` 或第一个非选项 argv，内层**再递归解析** |
| `xargs <cmd>` | `<cmd>` 递归解析 |
| `` `...` `` / `$(...)` | 命令替换：内层递归解析 |
| `eval` / `source` / `. file` | **直接拒绝**（语义不可静态判定） |
| 管道 `a \| b`、`;`、`&&`、`\|\|` | 逐段解析，全部通过才放行 |
| 数组展开 `${!x}`、进程替换 `<(...)` | 解析失败 → 拒绝 |

**已知缺口（写明，不假装）**：`python -c "..."` / `node -e "..."` 的
**字符串内容无法在 L1 层检查**——那是任意代码。守卫只确认「允许在当前上下文
运行内联解释器」，不检查其内容。与 §4 的定位一致：L1 防误操作，不防蓄意绕过。

**失败即拒**：AST 解析失败、遇到未知节点，一律拒绝并给出原因，
绝不「解析不了就放行」。

`docker` 额外禁 `-v` 挂载宿主根（如 `-v /:/host`）、禁 `--privileged`、
禁 `-v` 挂载 `../protforge` 之外的任意盘符。

### 6.4 WSL 通道：明说边界

§6.1 说「三通道都覆盖」，但 WSL 有个绕不开的事实：
`wsl.exe` 在 Windows 侧看到的是一条命令，进了 WSL 是另一套文件系统。

v4 的做法：**在 Windows 侧解析并递归内层命令**（§6.3 的 `wsl.exe` 行），
能拦住的拦住。**但 WSL 内部发生的文件操作，Windows 侧的路径守卫管不到**——
`/mnt/e/...` 在 Windows 看来根本不是一个被检查过的路径。

因此明说：**WSL 内不做文件边界管控，只做命令入口管控 + 全量审计。**
这不是疏忽，是在当前架构下的诚实边界。真要隔离得靠 WSL 内的用户权限或
容器挂载，属 §17「对外开放时才做」的范畴。

### 6.5 L2 文件边界

| 区域 | 权限 |
|---|---|
| `data\work\<task_id>\` | 读写 |
| `data\uploads\<task_id>\` | 只读（原始上传不可篡改） |
| `../protforge/data\outputs\` | 只读（**仅对文件工具**） |
| `../protforge/data\inputs\`、插件目录 | 只读 |

实现：先 `Path.resolve()` 再判定，只读区走同一逻辑但只给读许可。
**明说这是尽力而为，不是强安全边界**——评审建议的「用 Windows API 取最终路径」
可以挡住更多 junction/TOCTOU，但对本项目（agent 操作自己的任务目录）而言，
`resolve()` + 驱动器相对路径（`C:foo`，`startswith` 拦不住）+ 8.3 短名
+ 大小写不敏感 已足够；按 L1 的定位，这里不需要教科书级强度。

### 6.6 上传文件如何进入工作区

上传先落 `data\uploads\<task_id>\`（只读）。任务启动时**复制**到
`data\work\<task_id>\`，agent 在工作区里读写副本。
原始上传保持不变，产物打包时不混入用户输入。

**哪些技能读工作区输入**：rfdiffusion 一类以路径为参数的技能会直接读工作区里的
PDB，所以工作区必须保留副本。实现时按 §12 的技能清单逐个核对，
把「是否读工作区」记进 `skills_index.py` 的技能元数据——
**不能默认所有技能都走 `PROTFORGE_DATA/inputs/`**（实测 freesasa 支持绝对路径，
但 WSL 类的 `/mnt/e/...` 路径传递方式不同）。

## 7. 状态机与实时性

```
queued ──> running ──> packaging ──> done
   │          │
   └──────────┴──> failed
```

- `failed` 是终点，不在主链上
- **状态推进全部由系统触发，用户端点里不存在 `/advance`**：
  | 迁移 | 触发者 |
  |---|---|
  | `queued → running` | 调度器接走任务 |
  | `running → packaging` | agent 调 `finish` |
  | `packaging → done` | 打包子进程成功退出 |
  | `* → failed` | 中止 / 超时 / 崩溃 / 配额超限 |
- 用户对任务只有**中止**权，没有推进权。用户能推 `running → packaging` 就会
  绕过 §5.2 的完成判定，让半成品被判为完成
- 管理员**可**强制改状态（这是他明确要的干预能力），记 `actor=admin`，
  且改状态不影响产物发现的兜底逻辑（§8.1 仍会跑）
- `running` 期间展示 `tasks.step`（§5.1），一行显示在卡片上，不必点开

### 7.1 实时通道

```
GET /api/tasks/{id}/stream      text/event-stream
GET /api/tasks                  列表快照（首屏数据源，不走 SSE）
```

SSE 而非 WebSocket：单向推送足够、穿透反代、不需要升级握手。
轮询接口**保留**，作 SSE 不可用时的降级与首屏数据源。

**推送机制（实现时不要各写各的）**：两层，缺一不可。

1. **内存事件总线**：本进程写入 `events` 后立即触发进程内 `asyncio.Event`，
   SSE 连接被唤醒并读增量。零延迟，覆盖 99% 的情况。
2. **低频轮询兜底**：每 **3 秒**扫一次 `events` 表的 `id > last_seen`。

第 2 层**不是可选的**：管理员服务（独立进程）写库时，主进程的内存事件总线
根本不会被触发——两个进程不共享内存。只靠事件总线，管理员改状态后用户侧
会一直看不到变化。多连接时的 SQLite 读压力：轮询只扫 `id` 游标，不做全表扫描，
且每个 SSE 连接各自维护游标。

## 8. 数据流

```
用户 ─POST /api/tasks─> 主服务 ─入队─> 调度器 ─spawn─> pi agent (RPC 常驻)
                                                    │
   ┌──── bash / wsl / 文件工具 / MCP 搜索 ──────────┤
   │                                                  │
   ├─ report_step() ──> tasks.step ──> SSE ──> 用户卡片
   ├─ finish() ──────> 调度器进 packaging
   ├─ 事件流 ─────────> SQLite events ──> 时间线
   │
   └──── RPC 命令 ◀──── 管理员面板 (127.0.0.1:8474)
                           │
                    POST /api/internal/rpc/{task_id}
                           │  ← 闭环：见下
                    主服务持有 agent 的 stdin/stdout，转发后回响应
```

### 8.1 管理员 RPC 通道闭环

agent 由主服务的调度器 spawn，**stdin/stdout 句柄存在主服务进程内**；
管理员面板是独立进程，默认够不着。闭环方式：

- 管理员面板把 `steer`/`abort`/`set_model` 转发到主服务的**内部端口**
  `127.0.0.1:8475/api/internal/admin/rpc/{task_id}`，携带管理员 Bearer token
- 主服务校验 token 后，写入对应 agent 进程的 stdin，把 RPC 响应回传
- 事件流同理经 `/api/internal/admin/stream/{task_id}` 代理

**不使用 IP 判定**——理由见 §3.1：有反代时 `client.host` 恒为 127.0.0.1，
按 IP 判定等于没判定。安全边界靠**端口只绑 loopback + token 校验**。

代价是多一跳，换来「agent 句柄只存在于一个进程」这个可推理的性质。
备选方案（独立 supervisor 管 agent）要多一个进程和一套注册表，不采用。

### 8.2 产物发现（两条路径都跑，不是二选一）

agent 完全自主，系统提示要求它给 job 加 `t{id}_` 前缀**不构成强制**。

**v3 写的「前缀快路径命中就不走时间窗」是个 `or`，不是双保险**：
多工具接力时，第一个工具守规矩写了 `t{id}_rfdiffusion`，
第二个没守规矩写了 `bindcraft_run`——前缀路径非空 → 时间窗不跑 →
**第二个工具的产物永久丢失**。这正是双保险失效的场景。

改为**两条都跑**：

| 路径 | 范围 | 产物标记 | 可下载 |
|---|---|---|---|
| 前缀 | `outputs\t{task_id}_*` | `source='prefix'` | 是 |
| 时间窗 | `outputs/` 下 mtime 落在任务起止窗口内 | `source='timewindow'`，`verified=0` | **否** |

时间窗命中项**前端可见但不可下载**，需管理员在面板里确认后才转正。
并发时时间窗可能误收他人产物，所以「先显示、再确认」比「自动采纳」安全。

两条都不命中才算真的没产物。`docker exec` 被杀后要清理容器内孤儿进程，
否则目录会残留半成品。

## 9. 数据模型

### 9.1 users / sessions

- `users`：`username` 唯一、`password_hash` + 每用户随机 `salt`、`role ∈ (user, admin)`、`disabled`
- 哈希用**标准库** `hashlib.pbkdf2_hmac('sha256', ...)`，不装 bcrypt/passlib
- 密码策略：≥ 10 位；不强制复杂度组合（会降低可记忆性且无实质增益），
  但拒绝与用户名相同、拒绝 `< 8`
- 用户由管理员创建，**不做自助注册 / 找回 / OAuth**

### 9.2 鉴权细节（评审 #13）

- token：`secrets.token_urlsafe(32)`，存 `sessions` 表（含 `created_at`、`expires_at`）
- 传输：**`Authorization: Bearer <token>` 放请求头，不放 Cookie**
  → 天然无 CSRF（浏览器不会自动附加自定义头），省掉一整类攻击面
- 过期：默认 12 小时，空闲 2 小时则失效；`sessions` 表定期清理过期行
- 登录限流：同一用户名连续 5 次失败锁 15 分钟（计数存内存即可，不入库）
- **审计日志独立建表**：`audit_log(id, task_id, kind, command, exit_code,
  elapsed_ms, actor, created_at)`。

  **不放在 `events` 表**——一次 rfdiffusion 跑几小时，agent 的 bash 调用
  （循环、轮询、grep 日志）轻易上万条。混在 `events` 里会有两个后果：
  ① 用户时间线被 bash 审计刷屏，§5.1「step 一行可读」的设计直接失效；
  ② `events` 快速膨胀，SSE 每 3 秒的游标扫描（§7.1）越来越慢。

  `events` 只留**用户可见**事件（含管理员操作——用户有权知道自己的任务
  被管理员动过）；`audit_log` 只留管理员可见的底层细节。两者都定期清理
  （`audit_log` 默认留 30 天）。

### 9.3 tasks

新增 `step`（§5.1）。`brief` 存需求**全文且不截断**（agent 看不到全文就会自己编），
但设**上限 20000 字符**，超出返回 422——「不截断」不等于「无限制」。

### 9.4 uploads / artifacts / events

- `uploads` 只存路径
- `artifacts` 收口时填入，含 `source`（prefix / timewindow）与 `verified`
- `events`：`kind ∈ (created,status,log,note,upload,package,error,edited,step,finished)`，
  `actor ∈ (human,agent,admin,system)`。**不放 bash 审计**（见 §9.2）

### 9.5 SQLite 并发

主服务、调度器、管理员服务**都写库**。配置：

- `PRAGMA journal_mode=WAL`（已支持多进程）
- **`PRAGMA busy_timeout=10000`**（每个连接都要设，否则并发下直接
  `database is locked`）——这是本项目最容易漏的一行
- `recursive_triggers=OFF` 同样是每连接设置，`updated_at` 触发器依赖它

先按 SQLite + busy_timeout 走。**触发迁移到 PostgreSQL 的信号**：
出现写锁等待日志，或任务数需要跨机扩展。

## 10. 任务权限（评审 #16）

- `GET /api/tasks/{id}`、`abort`、`package`、`advance`、`stream` **全部校验 owner**
  （`role='admin'` 放行全部）
- 越权访问返回 **404 而非 403**：不泄露「这个 id 存在」这一信息
- 列表接口只返回自己的任务（admin 除外）
- 用户的 `abort` 只能中止自己的任务

## 11. 资源配额（评审 #12）

24 小时墙钟 ≠ 资源隔离。补：

| 项 | 默认 | 说明 |
|---|---|---|
| 全局并发任务 | **= 本机 GPU 数量**（启动时 `nvidia-smi` 探测，可手动下调） | `_common.sh` 的 `protforge_exec` 会自动挑空闲 GPU，所以并发上限绑定 GPU 数才是对的。**配成独立参数会漂移**——用户加卡后忘了改配置，就等于超卖 |
| 单用户并发 | 1 | 防止一个人占满队列 |
| 上传单文件 | ≤ 200 MB | |
| 上传文件数/任务 | ≤ 50 | |
| 上传扩展名白名单 | `.pdb .cif .fasta .fa .pdbqt .sdf .mol2 .json .smi .csv` | 不在白名单直接拒 |
| 单任务磁盘 | 上传副本 + 工作区 + 产物 ≤ 20 GB，超出则 `failed` | |
| 磁盘水位 | `data\` 所在盘剩余 < 50 GB 时**拒绝新任务**并告警 | |
| 队列公平 | FIFO，`priority` 可置顶 | |

**磁盘账**（评审指出的盲点）：单任务最坏 = 上传 10 GB（50×200 MB）
+ 工作区副本 10 GB + 产物 20 GB ≈ **40 GB**。并发绑定 GPU 数后，
2 卡就是 80 GB。因此除了单任务上限，还必须有**全局水位线**——
上面的 50 GB 剩余阈值就是为此存在，否则磁盘打满会连带搞垮 Docker 与系统盘。

## 12. 超时分级（评审 #9）

ProtForge 的 SKILL.md frontmatter **没有「类别」字段**（只有 name/description），
所以分级表必须由 `skills_index.py` **硬编码映射**——这是本项目的配置，
不改 ProtForge。未列出的技能走默认 4 小时。

| 类别 | 单次超时 | 技能 |
|---|---|---|
| 宿主快工具 | 10 分钟 | freesasa, chemprop, discotope3, fpocket, p2rank |
| 对接 | 2 小时 | vina-gpu, gnina, autodock-gpu, diffdock |
| 结构预测 | 4 小时（默认档） | esmfold2, opendde, igfold, alphafold3, af3-msa, alphafold-db |
| 设计与 MD | 8 小时 | rfdiffusion, bindcraft, gromacs, proteinmpnn |
| **待实测** | 暂归默认 | `pesto`（容器内但计算量小）、`esm`（ESM-2 耗时随序列长度与 pseudo-perplexity 设置差异很大） |

**`pesto` 与 `esm` 的档位是猜的**，必须实测后修正这张表——它们当前的归属
直接决定单次超时是 10 分钟还是 8 小时，猜错两个方向都会出事：
猜短了正常的 ESM-2 运行会被腰斩，猜长了挂死的任务要等 8 小时才收口。
实现阶段先各跑一次真实任务计时。

任务墙钟 **24 小时**。模型自设 `timeout` 与本表**取较小值**，防止模型自己放宽。

## 13. REST API

### 主服务 · 公网端口（`0.0.0.0:8473`）
```
POST /api/auth/login              登录换 Bearer token
GET  /api/me                      当前用户
POST /api/tasks                   创建
GET  /api/tasks                   列表（仅自己的）
GET  /api/tasks/{id}              详情
GET  /api/tasks/{id}/stream       SSE 实时流
POST /api/tasks/{id}/abort        中止
GET  /api/tasks/{id}/package      下载 zip
```
**这里没有 `/advance`**——状态推进全部由系统触发，见 §7。

### 内部端口（`127.0.0.1:8475`）
```
POST /api/internal/agent/{task_id}/step    agent 上报当前步骤
POST /api/internal/agent/{task_id}/finish  agent 收口
POST /api/internal/agent/{task_id}/event   agent 上报事件
     ↑ 一次性 agent_token，与 task_id 绑定

POST /api/internal/admin/rpc/{task_id}     转发 RPC 命令给 agent（§8.1）
GET  /api/internal/admin/stream/{task_id}  代理 agent 事件流
     ↑ 管理员 Bearer token
```

### 管理员服务（`127.0.0.1:8474`）
```
GET   /api/admin/tasks                  全部任务 + 队列 + worker 状态
POST  /api/admin/tasks/{id}/abort       终止任务
POST  /api/admin/tasks/{id}/status      强制改状态
POST  /api/admin/tasks/{id}/retry       失败重跑
GET   /api/admin/tasks/{id}/audit       审计日志（读 audit_log 表）
POST  /api/admin/artifacts/{aid}/verify 确认时间窗产物（§8.2）
GET   /api/admin/users                  用户列表
POST  /api/admin/users                  建用户
PATCH /api/admin/users/{id}             改角色 / 停用 / 重置密码
```

**契约**：所有 `detail` 恒为中文字符串（含框架层错误），与 creative-panel 一致。
前端用 Bearer token 存 `localStorage`，因此**必须发 CSP 头** `default-src 'self'`
——否则一次 XSS 就能把 token 拿走。

## 14. 错误处理

| 场景 | 表现 |
|---|---|
| 危险命令被拦 | 非零退出 + 说明，agent 可见并自行绕行，审计记录 |
| 未知复杂 shell 语法 | 拒绝 + 说明 |
| 缺 `PROTFORGE_STATUS:` | **不直接判失败**（仅 17/23 有效），看退出码 + 输出目录是否有产物 |
| 产物目录为空 | 判失败 |
| WSL 未就绪 / Docker 未运行 | 明确文案，agent 自行换路 |
| MCP server 启动失败 | 明确报「搜索能力未就绪」，不让 agent 误以为无资料 |
| MCP 首次 `uvx` 拉包 | 启动期预热校验，失败即禁用搜索并明示 |
| agent 崩溃 / RPC 断连 | 宽限期后判异常结束 → `failed`，**仍跑兜底产物扫描** |
| 未调 `finish` 就退出 | 同上，凭 `finished` 事件缺失判定 |
| zip 失败 | 停在 `packaging` 并报错，**不写 done** |
| 配额超限 | 明确报错，任务 `failed` |

## 15. 目录结构

```
protforge-web\
├─ server\      app.py / db.py / store.py / dispatcher.py / auth.py
├─             skills_index.py / artifacts.py / rpc_proxy.py
├─ admin\       app_admin.py
├─ agent\       run.mjs / guards.mjs / tools.mjs / package.json
├─ web\         index.html / app.js / style.css
├─ admin-ui\    index.html / app.js / style.css
├─ data\        web.db / uploads\ / work\ / packages\ / logs\
└─ docs\superpowers\specs\
```

`skills_index.py` 用**真 YAML 解析器**读 frontmatter（ProtForge 仓库自带
`test_frontmatter.py`，踩过「裸标量里的 `: ` 让 PyYAML 抛 ScannerError、
解析失败的技能被加载器静默丢弃」的坑），并维护超时分级映射。

## 16. 测试策略

- 后端：pytest，全走真实 HTTP（临时端口 + `tmp_path` 临时库），不碰真实 `data\web.db`
- **L1 守卫专项**：`bash -c "rm -rf /"`、`python -c`、`docker run -v /:/host`、
  管道、子 shell、`xargs`、变量展开、命令替换 `` ` `` `$(...)`、
  `eval`/`source`（必须直接拒）——逐个断言被拒且退出码非零。
  另需一条**反向用例**：语法合法且无害的命令必须放行，
  否则守卫会变成「什么都拒」，agent 直接卡死。
- **解析器失败即拒**：故意构造语法残缺的命令，断言拒绝而非放行
- **L2 边界专项**：`C:foo`、8.3 短名、junction、`..` 穿越、UNC
- **WSL 通道专项**：`wsl.exe -d AF3Native bash -c "rm -rf /mnt/e"` 的**内层**
  必须被解析并拦截（v2 的洞）
- **权限专项**：A 用户访问 B 用户任务返回 404
- **agent_token 越权专项**：拿任务 A 的 token 调 `/api/internal/agent/{B}/step`
  必须 403——这是 token 与 task_id 绑定的唯一验证点
- **并发专项**：多进程同时写库不出现 `database is locked`
- **SSE 断线重连**：断开后重连，靠 `Last-Event-ID` 或 `id` 游标补齐
  断线期间产生的事件；不能只测「连接期间能收到」
- **SSE 跨进程专项**：由**管理员服务**改状态，用户侧 SSE 必须在
  3 秒轮询窗口内看到变化（验证 §7.1 第 2 层兜底真的存在）
- step 截断：80 码点边界 + 多字节字符不被切坏
- brief 超长：422
- **完成判定专项**：有 `finish` 记 vs 无 `finish` 记，两条路径分流正确
- **产物兜底专项**：agent 崩溃后仍能捞回前半程产物；
  且模拟「第一个工具守规矩、第二个没守规矩」，验证前缀+时间窗**两条都跑**
- **打包子进程专项**：打包期间主服务仍能正常响应其他请求
- 端到端：真跑 `freesasa`（宿主、秒级、不占 GPU），验证全链路 + SSE + step + zip
- 前端无自动化测试（零 CDN 无构建的后果），改完用真浏览器看

## 17. 不做的事（YAGNI）

自助注册 / 密码找回 / OAuth；GPU 感知调度；结果在线 3D 预览；
任务模板与预设流水线（用户明确要完全自主）；跨机断点续跑；
进程外沙箱（对外开放时才需要）。

## 18. 待确认的假设与风险

**假设**：
1. 端口 8473 / 8474 / 8475 为推断值（避开本机 8471/8472）。
2. 管理员 UI 独立前端还是复用 `web/` 构建产物，待拍板。
   **这两项不定，目录结构和反代配置就没法写**——反代规则必须精确到「只转发 8473」。

**风险**：
3. **shell 解析器的选择与覆盖度是本项目最大的不确定性**。§6.3 规定了解析策略，
   但 `mvdan.cc/sh` 与 `tree-sitter-bash` 对 Windows 路径、引号、PowerShell 风格的
   差异要实测；**失败即拒**的策略意味着解析器覆盖不全会导致合法命令被误拒，
   agent 直接卡死。必须准备一份真实 ProtForge 命令样本做回归。
4. 23 个技能全进提示词的 token 成本需实测，过长则只放常用子集。
5. MCP 首次 `uvx` 拉包慢或需网络，靠预热校验兜底。
6. 24 小时墙钟对最长工具链够不够（rfdiffusion 大批量 + ESMFold2 full），需实跑。
7. `Path.resolve()` 与 WSL 内的文件边界都是尽力而为，与 §4 威胁模型一致，
   **不宣称强隔离**。对外开放前必须换真沙箱。
8. `pesto` 与 `esm` 的超时档位是猜的（§12），两个方向猜错都会出事。

**待清理**：
9. `server\db.py` 是设计前抢跑的草稿，与本文有出入：缺 `step` 列、
   缺 `users` / `sessions` / `audit_log` 三张表、`events.actor` 的 CHECK 缺 `admin`、
   缺 `busy_timeout`。**以本文为准重写，不要把草稿当基线。**

## 19. 技术选型理由

| 选择 | 理由 |
|---|---|
| pi coding agent（非自写循环） | 原生有工具、技能装载、MCP、RPC、事件流；且 ProtForge 无统一契约，自研封装反而要硬造一层 |
| 原生 bash + operations 守卫 | 删 bash agent 就没法自己写脚本调 WSL 类技能，能力损失远大于收益 |
| `operations` 而非 `spawnHook` | spawnHook 不能拒绝；受控实现才能真正拦下危险命令 |
| RPC 常驻而非 spawn 即退 | 管理员中途干预需要活着的会话 |
| SSE 而非 WebSocket | 单向推送够用，穿透反代，无升级握手；保留轮询作降级 |
| 独立 loopback 端口而非 IP 判定 | 反代在本机时 `client.host` 恒为 127.0.0.1，按 IP 判定等于没判定（v3 的洞） |
| 调度器在主服务进程内，非独立进程 | `finish` 打到的就是本进程端口，改状态无需 IPC；少一个进程少一套注册表 |
| 打包走子进程 | 20 GB 的 zipfile 是同步 IO，放主服务会拖垮所有请求 |
| Bearer header 而非 Cookie | 天然免 CSRF，省一整类攻击面 |
| SQLite + busy_timeout | 够用；写锁等待是明确的迁移信号 |
| ProtForge 零改动 | 23 个技能各自维护、有独立发布节奏；本项目只当调用方 |
