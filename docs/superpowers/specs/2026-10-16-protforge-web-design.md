# ProtForge 计算平台 · 设计文档

> 状态：待审。本文是 brainstorming 阶段的产出，未经批准不得进入实现。
> 日期：2026-10-16

## 1. 问题与目标

ProtForge 现有 23 个技能全部是命令行工具（bash 脚本 + `docker exec` / WSL venv），
使用门槛是「你得先跟 agent 对话、agent 才知道怎么拼参数」。这带来两个问题：

1. **不可自助**。非本项目作者要用一次 `rfdiffusion`，必须先学会这套命令行约定。
2. **不可见**。一次跑几小时的计算，中间发生了什么，命令行里只有滚动日志。

目标：做一个网页，把「提需求 → 计算 → 拿结果」变成普通网页操作，
并且计算过程中的每一步都能看见。**ProtForge 本身一行不改**。

非目标（YAGNI）：多租户计费、完整的用户体系、移动端适配、结果在线预览。

## 2. 核心职责

### 网页后端负责
- 任务账本（创建、状态流转、时间线）
- 文件托管（上传的输入文件、产物的 zip 包）
- 调度（队列、worker 生命周期、中止信号）
- 对外 API（用户面板）+ 对内 API（管理员面板）

### 网页后端**不**负责
- 不解析需求语义
- 不决定调哪个技能
- 不调用任何 ProtForge 脚本

这与 creative-panel 是同一个原则：**面板是账本不是执行器**。

### agent 负责
- 理解需求 → 决定技能链与参数
- 调用 ProtForge 技能、读写工作目录文件、联网查资料
- 把过程实时回写
- 收口时给出中文结论与产物清单

## 3. 运行形态

- 后端：FastAPI + SQLite，监听 `0.0.0.0:$PROTFORGE_WEB_PORT`。
  **端口尚未确认**，暂定 8473（本机已有 8471 media-center-pms、8472 creative-panel，需避开）。
- 管理员面板：同一进程内的独立路由，**仅接受来自 127.0.0.1 的请求**，其余一律 403。
  **部署建议：反代不要转发 `/api/admin/*`**。代码层的 403 是第二道防线，
  运维层压根不暴露才是第一道——公网域名下留一条管理员路径本身就是多余的暴露面，
  哪怕它拒绝远端访问。
- 计算：Node agent 进程，由后端的调度器 spawn，**不独立常驻**
- 公网：反向代理到本机。用户已有公网服务，本项目只提供应用层

## 4. agent 能力与边界

agent 是一个 pi agent（`@earendil-works/pi-agent-core`，照搬 `llm-quant\agent\research.mjs`
的形态），由后端为每个任务 spawn 一个独立进程，跑完即退。

### 4.1 工具集

| 工具 | 作用 |
|---|---|
| `list_skills()` | 列出可用 ProtForge 技能（名称 + 一句话用途） |
| `read_skill(name)` | 读该技能的 SKILL.md 全文 |
| `run_skill(name, args)` | 调用对应 runner 脚本，参数按 schema 校验 |
| `report(text)` | 向网页实时汇报一步进展 |
| `check_inbox()` | 读管理员干预留言（每轮循环调用一次） |
| `finish(summary, artifacts)` | 收口，提交中文结论与产物清单 |
| `fs_list/read/write/mkdir/delete` | 工作目录内的文件操作 |
| MCP `web_search` 等 | 由 `@modelcontextprotocol/sdk` 连 `uvx minimax-coding-plan-mcp` |

「完全自主」= **编排自主**：调哪些技能、什么顺序、参数填什么，全由 agent 决定。
不等于执行自主——agent 碰不到这三条边界以外的任何东西（见 4.3）。

### 4.2 预算

用户选择「完全自主，直到它认为完成」。因此**不设 agent 层面的轮次上限**，
但保留两个不可绕过的基础设施兜底：

- **单次工具调用超时**：`run_skill` 默认 2 小时（可配）。依据是 `llm-quant` 的实测教训——
  上游接口挂起时是**无限等待而非报错**，不设超时会静默烧穿整条任务。
  被杀掉的 `docker exec` 会连带清理容器内的孤儿进程。
- **任务墙钟上限**：默认 8 小时（可配），到期强制收口并标记 failed。

网页上提供手动中止按钮。

### 4.3 文件边界

| 区域 | 权限 | 说明 |
|---|---|---|
| `data\work\<task_id>\` | 读写 | agent 的工作区 |
| `../protforge/data\outputs\` | 只读 | 自己的计算结果 |
| `../protforge/data\inputs\` | 只读 | 现成样本（1ubq.pdb 等） |
| ProtForge 插件 skills 目录 | 只读 | SKILL.md 与脚本 |

越界拒绝。**Windows 路径逃逸要专门处理**，只用 `startswith` 判定不够：

- `..` 相对回退
- 绝对路径（`C:\...`）
- **驱动器相对路径 `C:foo`** —— 不以 `\` 开头，`startswith(工作目录)` 拦不住，但确实能逃出
- UNC 与 `\\?\` 前缀
- 8.3 短名（`PROTFO~1` 可指向 `ProtForge`）
- 大小写不敏感；符号链接/junction 需 `realpath` 后再判

判定必须在 `Path.resolve()` **之后**再做，且对只读区用同样的逻辑只做「读」许可。

## 5. 任务状态机

固定、线性、不可改名：

```
queued ──> running ──> packaging ──> done
   │          │
   └──────────┴──> failed
```

- `failed` 是终点，不在主链上，与 creative-panel 把「卡住」做成布尔位而非第六态同理
- **状态只能走 `/advance`**，PATCH 改 status 一律 422（沿用 creative-panel 的硬约束）
- 管理员**可以**强制改状态，这是他明确要的干预能力；该操作记 `actor=admin` 事件

| 状态 | 含义 |
|---|---|
| queued | 已提交，等 worker 接走 |
| running | agent 进程已起，正在编排与计算 |
| packaging | 产物收拢、打 zip |
| done | 完成，可下载 |
| failed | 中止 / 超时 / 模型或工具链报错 |

## 6. 数据流

```
用户浏览器 ──POST /api/tasks──> 后端 ──入队──> 调度器
                                              │ spawn
                                              ▼
                                        Node agent 进程
                                              │
      ┌──────────── 每一步 ────────────────────┤
      │  fs_* / web_search / run_skill          │
      │                                        │
      └──> POST /api/tasks/{id}/events ──> SQLite ──> 用户面板轮询
                            ▲
      管理员面板 ──POST /api/admin/tasks/{id}/inbox──┘
                                            （agent 每轮 check_inbox 读取）

收口：agent ──POST /finish──> 后端扫 outputs ──> 打 zip ──> packaging ──> done
中止：管理员 ──POST /abort──> 调度器 kill agent 进程树 ──> failed
```

### 6.1 产物发现（双保险）

`run_skill` 是 agent 在 Node 侧直接调 bash 的，**Python 调度器看不到它的返回值**。
若只依赖 agent 上报，agent 中途崩溃时产物路径就永久丢失——
用户等了几小时，最后什么都拿不到。这是本设计里最容易漏的坑，靠两条互补路径解决：

1. **约定路径（主）**：系统提示要求 agent 的所有 `run_skill` job 名
   必须以 `t{task_id}_` 开头。调度器 spawn 时把 task_id 经环境变量与初始
   prompt 双重告知，agent 无从误填。agent 通过 `report()` 实时把
   `PROTFORGE_OUTPUT_DIR` 显示给用户——这是正常路径的可见性。
2. **兜底扫描（保命）**：调度器在 agent 进程退出后**无条件**扫
   `../protforge/data\outputs\t{task_id}_*`，把命中的目录全部收进 `artifacts`。

因此多工具接力（rfdiffusion → proteinmpnn → opendde）产生的多个产物目录
都能被完整捞回，**即使 agent 在第二个工具跑完后崩溃**。agent 没走到 finish
不是丢产物的理由。

**关键**：agent 不是等跑完才写状态，而是每一步都回写。这直接解决「干等几小时看不到发生什么」。

## 7. 数据模型

四张表：

- `tasks` — 任务主体。`brief` 存需求**全文且不截断**（agent 看不到全文就会自己编）；
  `uploads_dir` / `output_dir` / `package_path` / `summary` / `error` / `session_id`
- `uploads` — 上传输入文件，只存路径，文件在 `data\uploads\<task_id>\`
- `artifacts` — 产物清单，收口时扫 `output_dir` 填入
- `events` — 时间线。`kind ∈ (created,status,log,note,upload,package,error,edited)`，
  `actor ∈ (human,agent,admin,system)`

`updated_at` 用触发器自动维护，`recursive_triggers=OFF` 是硬前提（creative-panel 实测结论：
去掉 WHEN 子句会连跨秒场景都递归）。

## 8. REST API

### 用户侧（公网）
```
GET    /api/health
POST   /api/tasks                    创建（含 brief / title / 上传文件）
GET    /api/tasks                    列表（按状态筛选）
GET    /api/tasks/{id}               详情（含时间线、上传、产物）
POST   /api/tasks/{id}/abort         用户中止自己的任务
GET    /api/tasks/{id}/package       下载 zip
```

### 管理员侧（仅本地）
```
GET    /api/admin/tasks              全部任务 + 队列 + worker 状态
POST   /api/admin/tasks/{id}/abort   终止
POST   /api/admin/tasks/{id}/status  强制改状态
POST   /api/admin/tasks/{id}/inbox   追加干预指令
POST   /api/admin/tasks/{id}/retry   失败重跑
GET    /api/admin/agent/{id}/stream  agent 原始事件流
```

**契约**：所有 `detail` 恒为中文字符串（框架层错误也要翻成人话），
与 creative-panel 一致——CLI 和前端都直接打印/展示它，混形态就没法写匹配逻辑。

## 9. 错误处理

| 场景 | 表现 |
|---|---|
| Docker 未运行 | `run_skill` 返回明确文案，不让 agent 瞎重试 |
| 脚本无 `PROTFORGE_STATUS:` 行 | **判为失败**，即使 exit 0（ProtForge 明文契约） |
| 单次工具超时 | 记 error 事件，回给 agent 让它自己决定换路还是收口 |
| agent 进程崩溃 | 调度器捕获，标 failed，保留已回写的时间线 |
| agent 没调 finish 就退出 | 调度器按「异常结束」处理，不假设成功 |
| zip 打包失败 | 停在 packaging 并报错，不写 done |

## 10. 目录结构

```
protforge-web\
├─ server\     app.py / db.py / store.py / dispatcher.py / skills_index.py
├─ agent\      run.mjs / package.json / (node_modules)
├─ web\        index.html / app.js / style.css
├─ data\       web.db / uploads\ / work\ / packages\ / logs\
└─ docs\superpowers\specs\
```

`skills_index.py` 在启动时扫描插件目录，**从每个 SKILL.md 的 frontmatter 取
name 与 description**，据此生成 `run_skill` 的参数 schema——这样新增技能不需要改代码。
（frontmatter 解析必须用真 YAML 解析器，protforge 仓库里已有 `test_frontmatter.py`
踩过「解析失败被静默丢弃」的坑。）

## 11. 测试策略

- 后端：**pytest，全部走真实 HTTP**（临时端口 + `tmp_path` 临时库），
  绝不碰真实 `data\web.db`——沿用 creative-panel 的纪律
- 路径逃逸：专项用例覆盖 4.3 列出的每一种 Windows 形态，含 `C:foo` 与 8.3 短名
- agent：工具层单测（mock 掉 execFile），验证 `PROTFORGE_STATUS` 判定与超时分支
- 端到端：**真跑一次 freesasa**（host Python，秒级，不占 GPU），
  验证 queued→running→packaging→done 全链路与 zip 内容
- 前端无自动化测试（零 CDN 无构建的直接后果），改完用真浏览器看一眼

## 12. 不做的事（YAGNI）

- 不做用户账号体系——按现有公网服务的鉴权，本项目假定请求已过认证
- 不做 GPU 感知的调度——工具不声明显存需求
- 不做结果在线 3D 预览
- 不做任务模板/预设流水线（用户要的是完全自主）
- 不做断点续跑（内存里记 `--resume` 之外的东西）

## 13. 待确认的风险与假设

**假设**（未经确认，实现前需拍板）：

1. **鉴权由现有公网服务承担**，本项目不实现用户体系。这是第 12 节 YAGNI 的前提——
   若公网服务实际不做鉴权，本节全部作废，必须回头补账号体系。
2. **端口 8473** 只是避开本机已用端口的推断值。

**风险**：

3. **8 小时墙钟上限对最长的工具链够不够**。rfdiffusion 20 个设计 + ESMFold2 full
   可能超；届时是调参还是拆任务，要实际跑过才知道。
4. **MCP server 首次 `uvx` 拉包**可能很慢或需要网络，第一个任务可能因此失败。
   实现时会加一次启动期预热校验，失败要明确报「搜索能力未就绪」而不是让 agent 瞎重试。
5. **Windows 路径逃逸防护**是本项目最容易写错的地方，测试用例要写足，
   尤其 `C:foo` 与 8.3 短名这两种 `startswith` 拦不住的形态。
6. `../protforge` 的路径来自 `scripts/local.env`，本项目**只读引用**，不修改该文件。

**已知的待清理项**：

7. `server\db.py` 是本次设计之前抢跑写下的草稿，其 `events.actor` 的 CHECK 约束
   只有 `human/agent/system`，**缺 `admin`**，与本文第 7 节冲突。
   实现时以本文为准修正，不要把草稿当成既定基线。

## 14. 技术选型理由

| 选择 | 理由 |
|---|---|
| FastAPI + SQLite | 与 creative-panel 同构，用户已熟悉；零外部依赖 |
| 原生 JS + 零 CDN 前端 | 同上。本机工具改完刷新即生效，不需要构建链 |
| pi agent | 已在 `llm-quant` 验证；自带工具循环与预算控制，比自写 agent 循环可靠 |
| HTTP 回写而非文件轮询 | 实时性是本项目的核心诉求之一 |
| ProtForge 零改动 | 23 个技能各自维护，插件有独立的发布节奏；本项目只当调用方 |
