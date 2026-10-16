# ProtForge 计算平台 · 设计文档（v3）

> 状态：待审。v3 依据用户初审 + DeepSeek 设计评审重写。
> 日期：2026-10-16

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

三个独立进程：

| 服务 | 监听 | 默认端口 | 公网 |
|---|---|---|---|
| 用户面板（主服务） | `0.0.0.0` | 8473（待定） | 是 |
| 管理员面板 | `127.0.0.1` | 8474（待定） | **否** |
| agent | 子进程，stdin/stdout 由主服务持有 | — | 否 |

管理员面板独立进程、只绑 loopback，反代在网络层就够不着。
（v1「同进程 + 403」与 v2「同进程 + 独立路由」都已否决。）

三者共享同一个 SQLite 库。

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

| 工具 | 作用 | 后端行为 |
|---|---|---|
| `report_step(text)` | 上报当前正在做什么 | 写 `tasks.step`，**按 Unicode 码点截断到 80 字符并加省略号**（不是字节截断，否则会切坏 UTF-8） |
| `finish(summary, artifacts)` | agent 判定完成并收口 | POST 后端 → 调度器进入 `packaging` |

`report_step` 必须**便宜到可以频繁调用**，agent 在长工具运行期间也要周期性更新，
否则用户看到的步骤会停在三小时前。

### 5.2 完成判定协议

- **正常**：agent 调 `finish` → 调度器进 `packaging` → 扫产物 → 打 zip → `done`
- **异常**：agent 进程退出但**没有** `finish` 记录 → 判为异常结束，进 `failed`，
  **但仍执行 §8.1 的兜底产物扫描**
- 两者必须能区分，所以 `finish` 落库时写一条 `kind='finished'` 事件作为凭据

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

### 6.3 L1 命令守卫

不能只查首 token——`bash -c "rm -rf /"`、`python -c "..."`、
`docker run -v /:/host`、`xargs`、管道、子 shell、变量展开都能绕过。
做法：**切分出实际会执行的简单命令逐条校验**，遇到管道/子 shell/命令替换等
复杂语法时按白名单逐段检查，未知语法拒绝。`docker` 额外禁 `-v` 挂载宿主根。

### 6.4 L2 文件边界

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

### 6.5 上传文件如何进入工作区

上传先落 `data\uploads\<task_id>\`（只读）。任务启动时**复制**到
`data\work\<task_id>\`，agent 在工作区里读写副本。
原始上传保持不变，产物打包时不混入用户输入。

## 7. 状态机与实时性

```
queued ──> running ──> packaging ──> done
   │          │
   └──────────┴──> failed
```

- `failed` 是终点，不在主链上
- 用户侧状态**只能**走 `POST /api/tasks/{id}/advance`（v2 漏了这个端点，v3 补回）
- 管理员可强制改状态，记 `actor=admin`
- `running` 期间展示 `tasks.step`（§5.1），一行显示在卡片上，不必点开

### 7.1 用户侧实时通道（评审 #4）

只有轮询不满足「每一步可见」的核心诉求。新增 SSE：

```
GET /api/tasks/{id}/stream      text/event-stream，推送状态变更 / step / 时间线
GET /api/tasks                  列表快照（首次渲染用，不走 SSE）
```

SSE 而非 WebSocket：单向推送足够，天然穿透反代，不需要额外的升级握手。
轮询接口**保留**，作为 SSE 不可用时的降级与首屏数据源。

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

### 8.1 管理员 RPC 通道闭环（评审 #2，v2 的真实漏洞）

agent 由主服务的调度器 spawn，**stdin/stdout 句柄存在主服务进程内**；
管理员是独立进程，默认够不着。闭环方式：

- 主服务注册内部端点 `POST /api/internal/rpc/{task_id}`，
  **只接受来自 127.0.0.1 且携带 admin token 的请求**，否则 403
- 管理员面板收到 `steer`/`abort`/`set_model` 后，转发给该端点
- 主服务写入对应 agent 进程的 stdin，把 RPC 响应回传给管理员面板
- agent 进程存活期间，**管理员面板持续读取事件流**（同样经主服务代理）

代价是多一跳，但换来了「agent 句柄只存在于一个进程」这个可推理的性质。
备选方案（独立 supervisor 管 agent）要多一个进程和一套注册表，v3 不采用。

### 8.2 产物发现（双保险，评审 #10 强化）

agent 完全自主，系统提示要求它给 job 加 `t{id}_` 前缀**不构成强制**。
所以两条路径都不依赖 agent 守规矩：

1. **前缀快路径**：`../protforge/data\outputs\t{task_id}_*`
2. **时间窗兜底**：记录任务开始/结束时间，扫 `outputs/` 下 mtime 落在窗口内的目录。
   并发时可能误收他人产物——因此**只在前缀快路径为空时才启用**，且结果标记
   `source='timewindow'` 供管理员辨认

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
- **审计日志**：登录成功/失败、管理员全部写操作、agent 的全部 bash 调用
  （`bash 命令原文 + 退出码 + 耗时`）都进 `events`，管理员面板可查可导出

### 9.3 tasks

新增 `step`（§5.1）。`brief` 存需求**全文且不截断**（agent 看不到全文就会自己编），
但设**上限 20000 字符**，超出返回 422——「不截断」不等于「无限制」。

### 9.4 uploads / artifacts / events

- `uploads` 只存路径
- `artifacts` 收口时填入，含 `source`（prefix / timewindow）
- `events`：`kind ∈ (created,status,log,note,upload,package,error,edited,step,finished,audit)`，
  `actor ∈ (human,agent,admin,system)`

### 9.5 SQLite 并发（评审 #7）

主服务、调度器、管理员服务**都写库**，v2 说的「管理员写、用户读」不准确。
配置：

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
| 全局并发任务 | 1（可配 2–3） | ProtForge 的 `protforge_exec` 会自动挑空闲 GPU |
| 单用户并发 | 1 | 防止一个人占满队列 |
| 上传单文件 | ≤ 200 MB | |
| 上传文件数/任务 | ≤ 50 | |
| 上传扩展名白名单 | `.pdb .cif .fasta .fa .pdbqt .sdf .mol2 .json .smi .csv` | 不在白名单直接拒 |
| 任务产物目录 | ≤ 20 GB，超出则 `failed` | 防磁盘打满 |
| 队列公平 | FIFO，`priority` 可置顶 | |

## 12. 超时分级（评审 #9）

ProtForge 的 SKILL.md frontmatter **没有「类别」字段**（只有 name/description），
所以分级表必须由 `skills_index.py` **硬编码映射**——这是本项目的配置，
不改 ProtForge。未列出的技能走默认 4 小时。

| 类别 | 单次超时 | 技能 |
|---|---|---|
| 宿主快工具 | 10 分钟 | freesasa, chemprop, discotope3, fpocket, p2rank, pesto |
| 对接 | 2 小时 | vina-gpu, gnina, autodock-gpu, diffdock |
| 结构预测 | 4 小时（默认档） | esmfold2, opendde, igfold, alphafold3, af3-msa, alphafold-db |
| 设计与 MD | 8 小时 | rfdiffusion, bindcraft, gromacs, proteinmpnn, esm |
| 其他 / 未分类 | 4 小时 | |

任务墙钟 **24 小时**。模型自设 `timeout` 与本表**取较小值**，防止模型自己放宽。

## 13. REST API

### 主服务（公网）
```
POST /api/auth/login              登录换 Bearer token
GET  /api/me                      当前用户
POST /api/tasks                   创建
GET  /api/tasks                   列表（仅自己的）
GET  /api/tasks/{id}              详情
GET  /api/tasks/{id}/stream       SSE 实时流
POST /api/tasks/{id}/advance      推进状态
POST /api/tasks/{id}/abort        中止
GET  /api/tasks/{id}/package      下载 zip
```

### 内部（仅 127.0.0.1 + admin token）
```
POST /api/internal/rpc/{task_id}          转发 RPC 命令给 agent（§8.1）
GET  /api/internal/stream/{task_id}       代理 agent 事件流
```

### 管理员服务（仅 127.0.0.1）
```
GET   /api/admin/tasks            全部任务 + 队列 + worker 状态
POST  /api/admin/tasks/{id}/abort /status /retry
GET   /api/admin/tasks/{id}/audit 审计日志
GET   /api/admin/users
POST  /api/admin/users
PATCH /api/admin/users/{id}       改角色 / 停用 / 重置密码
```

**契约**：所有 `detail` 恒为中文字符串（含框架层错误），与 creative-panel 一致。

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
  管道、子 shell、`xargs`、变量展开——逐个断言被拒且退出码非零
- **L2 边界专项**：`C:foo`、8.3 短名、junction、`..` 穿越、UNC
- **WSL 通道专项**：确认 `wsl.exe` 调用同样受限（v2 漏洞）
- **权限专项**：A 用户访问 B 用户任务返回 404
- **并发专项**：多进程同时写库不出现 `database is locked`
- step 截断：80 码点边界 + 多字节字符不被切坏
- brief 超长：422
- **完成判定专项**：有 `finish` 记 vs 无 `finish` 记，两条路径分流正确
- **产物兜底专项**：agent 崩溃后仍能捞回前半程产物
- 端到端：真跑 `freesasa`（宿主、秒级、不占 GPU），验证全链路 + SSE + step + zip
- 前端无自动化测试（零 CDN 无构建的后果），改完用真浏览器看

## 17. 不做的事（YAGNI）

自助注册 / 密码找回 / OAuth；GPU 感知调度；结果在线 3D 预览；
任务模板与预设流水线（用户明确要完全自主）；跨机断点续跑；
进程外沙箱（对外开放时才需要）。

## 18. 待确认的假设与风险

**假设**：
1. 端口 8473 / 8474 为推断值（避开本机 8471/8472）。
2. 管理员 UI 独立前端还是复用 `web/` 构建产物，待拍板。

**风险**：
3. **四通道守卫的覆盖度是本项目最大的不确定性**。Git Bash / WSL / 宿主 Python
   三条通道的语法差异必须实测（§6.1），不能假设。
4. 23 个技能全进提示词的 token 成本需实测，过长则只放常用子集。
5. MCP 首次 `uvx` 拉包慢或需网络，靠预热校验兜底。
6. 24 小时墙钟对最长工具链够不够（rfdiffusion 大批量 + ESMFold2 full），需实跑。
7. `Path.resolve()` 的守卫强度是尽力而为，与 §4 威胁模型一致，不宣称强隔离。

**待清理**：
8. `server\db.py` 是设计前抢跑的草稿：缺 `step` 列、缺 `users`/`sessions`/`sessions` 相关表、
   `events.actor` 的 CHECK 缺 `admin`、缺 `busy_timeout`。**以本文为准重写。**

## 19. 技术选型理由

| 选择 | 理由 |
|---|---|
| pi coding agent（非自写循环） | 原生有工具、技能装载、MCP、RPC、事件流；且 ProtForge 无统一契约，自研封装反而要硬造一层 |
| 原生 bash + operations 守卫 | 删 bash agent 就没法自己写脚本调 WSL 类技能，能力损失远大于收益 |
| `operations` 而非 `spawnHook` | spawnHook 不能拒绝；受控实现才能真正拦下危险命令 |
| RPC 常驻而非 spawn 即退 | 管理员中途干预需要活着的会话 |
| SSE 而非 WebSocket | 单向推送够用，穿透反代，无升级握手；保留轮询作降级 |
| Bearer header 而非 Cookie | 天然免 CSRF，省一整类攻击面 |
| SQLite + busy_timeout | 够用；写锁等待是明确的迁移信号 |
| ProtForge 零改动 | 23 个技能各自维护、有独立发布节奏；本项目只当调用方 |
