# 蛋白计算平台（protforge-web）

把 ProtForge 的蛋白计算技能开放成一个**网页**：授权人员提交需求，后台 agent 自主编排
技能链跑计算，过程实时可见，完成后下载 zip。

---

## 这是什么、给谁用

**给公司内部授权人员远程使用，不是公开服务。** 使用者需要登录，任务是算给自己看的
（分子对接、结构预测、抗体设计……），不是对外提供能力的产品。

- 需要账号：没有账号什么都干不了，连任务列表都看不到。
- 数据不出本机：任务记录、上传原件、产物包全在本机数据盘上。
- 谁能登录由管理员在管理员面板里建，平台自己不开放注册。

## 快速开始

1. 双击 **`start.bat`**
   会开出**三个**服务窗口，浏览器自动打开 <http://127.0.0.1:8473/>。
   **那三个窗口就是服务**，关掉窗口等于停掉对应服务；启动器窗口关掉不影响。

2. 需要联网搜索时，把 `MINIMAX_API_KEY` 配到 `.env`（从 `.env.example` 复制改名）。
   不配也能跑，只是 agent 不会联网。

> 第一次用要先在 `.env` 里配 `PROTFORGE_ADMIN_USER` / `PROTFORGE_ADMIN_PASSWORD`
> 建一个管理员（平台**不开放注册**，没人建管理员就谁也登不进去），细节见下面
> 「常见排查」。

### 不想开窗口：后台起 / 开机自启

`scripts\` 下有两个 PowerShell 脚本，**无窗口**，适合登录自启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <仓库>\scripts\start-services.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File <仓库>\scripts\stop-services.ps1
```

`start-services.ps1` **按监听端口幂等**：已经在听就直接跳过，不会起第二套占端口失败、
然后让旧版本继续对外服务。日志落仓库的 `logs\`（已 gitignore，不入版本库），
每次启动都追加一行到 `logs\start-services.log`。

`stop-services.ps1` 停两轮：先按**监听端口**杀（占着端口的那个才是会让旧版本
继续对外服务的进程），再按**命令行**扫一遍——扫的是那些跑着本仓服务入口、
却已经不占端口的残留（端口被别人拿走了、它没绑上、或者它是上一轮的子进程）。
这种进程留着，下一次启动会看到「端口已占用」而跳过启动，旧版本就继续在服务，
而日志上写的是「already listening」，看着一切正常。判断规则在
`scripts\select_service_pids.py`（一条能测的纯函数）：只认 python 可执行文件、
入口必须是**整个参数**且逐段对上，所以本机其它项目的 `python app.py` 不会被误杀。
每一次杀都连同完整命令行写进 `logs\stop-services.log`。

本机已注册登录自启任务 **`protforge-web-services`**（触发器 = 用户登录时，
动作 = 上面的 `start-services.ps1`）：

```powershell
Get-ScheduledTask -TaskName protforge-web-services      # 看状态
Start-ScheduledTask -TaskName protforge-web-services    # 立刻起一次
Unregister-ScheduledTask -TaskName protforge-web-services -Confirm:$false   # 取消自启
```

任务只在**登录时**触发（不是开机时）——服务要有人登录才跑得起来，这是本项目的定位。
它**不会**设置 `PROTFORGE_WEB_DEV_LOGIN`，鉴权始终开着。

---

## 架构：网页是账本，不是执行器

**网页只负责记账和展示，算力活全是后台 agent 干的。** 浏览器里点「提交」，
只是往数据库写一条任务记录；真正去调技能的是调度器 spawn 出来的 Node agent 进程，
它在 WSL 里跑 ProtForge 的脚本，把每一步回写到内部 API，网页再通过 SSE 实时显示。
**网页自己不会去调任何技能**，你在页面上看到「运行中」不代表此刻真的有什么在跑——
那是 agent 回传的状态。反过来也一样：agent 跑完了但页面没刷新，重新点开就是最新的
（事件流有 3 秒轮询兜底）。

一句话：**账本在网页上，执行的 agent 在后台，两者靠内部 API 单向通信。**

```
浏览器 ──8473──> 用户面板主服务 ──写库──> web.db
                        │
                        └──spawn──> agent (Node 子进程, RPC)
                                      │  回传 step / finish
管理员面板 ──8474──┐                    │
                  └──8475──> 内部 API <┘  （改状态、发 RPC 干预）
```

---

## 端口与反代

| 服务 | 监听 | 端口 | 公网暴露 |
| --- | --- | --- | --- |
| 用户面板主服务 | `0.0.0.0` | **8473** | **是，反代只转发这个** |
| 管理员面板 | `127.0.0.1` | 8474 | 否 |
| 内部 API（agent 回调 + 管理端 RPC 转发） | `127.0.0.1` | 8475 | 否 |
| agent（Node 子进程） | — | — | 否，由调度器 spawn |

> 8471 / 8472 已被本机 `media-center-pms`、`creative-panel` 占用，本项目不碰。

### 反代只转发 8473，8474 和 8475 一律不转发

```nginx
location / {
    proxy_pass http://127.0.0.1:8473;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # 实时事件流是 SSE：关掉缓冲，否则进度会攒着一起吐出来
    proxy_buffering    off;
    proxy_cache        off;
    proxy_read_timeout 3600s;
}

# 8474（管理员面板）与 8475（内部 API）**不要**在这里出现。
# 它们只绑 127.0.0.1，公网本来也够不着；一旦你把它们转发出去，
# 这两个进程的所有安全边界同时失效。
```

**为什么「不转发」是第一道防线，而不是「判定来源是 127.0.0.1」**：你的反代就跑在本机，
公网请求经它转发后 `client.host` **同样是 127.0.0.1**。靠 IP 判来源等于没判。
所以边界做在两个地方，且顺序不能反：

1. **运维层不转发**（反代配置只写 8473）——第一道防线。
2. **代码层物理隔离**——内部路由根本不挂在 8473 那个 app 上，两条命令分开跑
   （`server/app.py public` 与 `server/app.py internal`），端口和 host 写死在代码里。
   就算有人用错参数，public app 上也没有 `/api/internal/*` 这条路由，不是靠登录拦，是压根没有。

---

## 数据在哪、怎么备份

**数据默认落在仓库旁边的 `data\`**（跟着代码走，换台机器 clone 下来就能跑）。
要把它挪到别的盘（数据与代码不同盘的老规矩），用 `PROTFORGE_WEB_DATA_ROOT` 指过去——
本机的写法写在仓库根目录的 `.env` 里（**那份不入版本库**）。

| 内容 | 位置 | 保留 |
| --- | --- | --- |
| 代码 | `<仓库>\` | git 仓库 |
| 数据库 | `<数据盘根>\web.db` | 审计日志留 30 天 |
| 上传原件 | `<数据盘根>\uploads\<id>\` | 7 天（只读） |
| agent 工作区 | `<数据盘根>\work\<id>\` | 7 天 |
| 产物包 zip | `<数据盘根>\packages\` | 30 天，磁盘紧张按最旧优先提前回收 |
| 日志 | `<数据盘根>\logs\` | 14 天，单文件上限 64 MB |

- **ProtForge 的产物留在 `<ProtForge 数据区>\outputs\`，本项目只引用不搬动。**
- 上传原件进工作区时用 **hardlink**（同一卷不占额外数据块），跨卷或不支持才退回复制。
- 单任务磁盘上限 20 GB（产物 + zip 合计），数据盘剩余不足 50 GB 时拒绝新任务。

**备份就是把整个数据盘拷走**：

```bat
xcopy <数据盘根> <备份目录>\ /E /I
```

恢复：拷回 `<数据盘根>\` 覆盖，再重启 `start.bat`。
`data\` 不入版本库。

---

## 环境变量

**代码只从进程环境读变量，自己不加载 `.env`。** `start.bat` 会在拉起服务前载入项目根目录的
`.env`（没有就跳过，用代码里的默认值）。所以手动起服务时，得自己先把变量设好。
从 `.env.example` 复制改名，**任何密钥都不要提交进仓库**。

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `PROTFORGE_WEB_DATA_ROOT` | 数据盘根 | `<数据盘根>` |
| `PROTFORGE_WEB_DB` | 数据库路径 | `<数据盘根>\web.db` |
| `PROTFORGE_WEB_INTERNAL` | 内部 API 基址，**不含 `/api`** | `http://127.0.0.1:8475` |
| `PROTFORGE_WEB_SKILLS_DIR` | 服务端读的技能目录 | `<技能仓>\skills` |
| `PROTFORGE_SKILLS_DIR` | agent 读的技能目录（同一个） | `<技能仓>\skills` |
| `PROTFORGE_DATA` | ProtForge 只读数据区 | `<ProtForge 数据区>` |
| `PROTFORGE_WEB_NODE` | node 可执行文件 | `node` |
| `PROTFORGE_WEB_AGENT_SCRIPT` | agent 入口脚本 | `<代码目录>\agent\run.mjs` |
| `PROTFORGE_WEB_MAX_CONCURRENCY` | 并发上限 | 按 GPU 数推断，默认 1 |
| `MINIMAX_API_KEY` | MCP 搜索用；**缺了不报错，只是不能联网** | 空 |
| `MINIMAX_API_HOST` | 同上 | `https://api.minimax.cn` |

> 本机已经配了一个**用户级** `MINIMAX_API_KEY`（125 字符），任何由你启动的进程都继承得到，
> 所以 `.env` 里留空也能联网。写进 `.env` 只在需要换 key 时才需要。

`PROTFORGE_TASK_ID` / `PROTFORGE_AGENT_TOKEN` / `PROTFORGE_WORK_DIR` 由调度器 spawn 时注入，
**不要手工设**——手工设的 token 会被判为无效。

---

## 已知定位：这是防误操作层，不是安全沙箱

**必须先说清楚，否则下面所有防护都是自欺欺人。**

使用者是本公司授权人员（需登录），防御目标**不是蓄意攻击者**。agent 需要完整的 shell
才能自己写脚本调 ProtForge（WSL、venv、Docker 命令），这就必然意味着
**「有 shell 就没有真正的沙箱」**。后台那层命令守卫（`agent/guards.mjs` 的 AST 解析）
是**尽力而为**：它挡手滑、挡误删 `<ProtForge 根>`，**挡不住刻意绕过**。

所以它是一层**防误操作**的护栏，不是安全边界。真要对外开放，必须换成真沙箱
（容器 / 独立账号 / 资源限制），而不是继续加白名单。

守卫只在**会改变文件系统或以文件为参数**的地方判路径，一共五处：删除/移动类
命令的操作数、重定向的目标、被执行脚本的路径、docker 挂载的宿主路径、
以及 read/write 工具。`cat` / `head` / `grep` 这类**只读**命令的操作数不判——
agent 需要读允许根之外的输入文件，这是刻意留的口子。路径判定前会先消解
`.` 与 `..`（且必须在盘符映射**之后**），否则 `<ProtForge 根>\..\..\Users\x` 规范化后
仍以 `/mnt/<ProtForge 根>/` 开头，会被直接放行——这条 16 个用例在 `agent/guards.dotdot.test.mjs`。

---

## 已验证 / 尚未验证

### 已验证

- **端口与路由隔离**：public 与 internal 两个 app 各起一个 uvicorn，实测 18473 / 18475
  都返回 `200 {"ok":true,...}`；public app 上没有 `/api/internal/*` 路由
  （`tests/test_app.py`）。两个 CLI 入口 `server/app.py public|internal`、
  `admin/app_admin.py` 均可正常解析（`--help` 退出码 0）。
- **令牌与越权**：`agent_token` 与 `task_id` 绑定，拿 A 的 token 调 B 的端点返回 403。
- **spawn 注入**：`PROTFORGE_WEB_INTERNAL` / `PROTFORGE_TASK_ID` / `PROTFORGE_WORK_DIR` /
  `PROTFORGE_AGENT_TOKEN` 由调度器经环境变量注入，不走命令行。
- **`PROTFORGE_WEB_INTERNAL` 不含 `/api` 前缀**：agent 侧是 `基址 + /api/internal/agent/...`
  拼出来的（`agent/tools.mjs`），写成带 `/api` 会得到双斜杠的 404。
- **回传字段名**：agent 发 `{text}` / `{summary, artifacts}`，服务端 `StepIn.text`、
  `FinishIn.summary` / `.error` 对得上。
- **保留策略**：`janitor.sweep()` 按 spec §3.05.2 实现，所有删除动作都过三道闸
  （本项目数据盘内 / 不碰活跃任务 / 名字符合该区域的生产形状）。形状**按区域**取：
  `work` `uploads` 是 `^\d+$`，`logs` 是 `^agent-\d+(\.err)?\.log(\.\d+)?$`，
  `packages` 是 `^task_\d+\.zip$`——都必须跟**生产者**写出来的名字对齐，
  另有一条命名漂移哨兵，生产者一改名字就会在 `errors` 里报出来。
- **agent 端**：AST 命令守卫 + pi RPC 模式 + 步骤回传可用，有单测。

### 端到端实测（2026-10-16，本机，真实数据盘 `<数据盘根>`）

真实 freesasa 计算走完全程，**61 秒**：

```
提交 → queued → running → 拉起 agent → 换模型 → 读 SKILL.md
     → 定位 <ProtForge 数据区>/inputs/1ubq.pdb → 找到 venv → 跑 protforge-freesasa.sh
     → 76 残基 / 602 原子，总 SASA 4804.06 Å²（与 Result.totalArea() 交叉校验一致）
     → 按原子类型统计 → finish(summary, artifacts) → done
```

打包另用这次的真实产物验过：zip 4493 字节 / 5 个文件，且确认 `.pi-agent/`、
`models.json`、`brief.md` 一个都没混进去。

单技能实测耗时（用于定时档位）：

| 技能 | 输入 | 耗时 | 结果 |
|---|---|---|---|
| protforge-freesasa | 1ubq.pdb | 61 秒（含 agent 全流程） | OK |
| protforge-pesto | 1ubq.pdb（76 残基） | **16.6 s** | 11 hotspots |
| protforge-esm | 1ubq 序列（73 残基）embed | **36.9 s** | dim=1280 |

### 曾经「还没连上」的六条，现在全是绿的

这六条不是被优化掉的，是**接上了**。记录一下它们错在哪，因为这才是本项目
最值得记住的一课：

> 13 个 task 各自交付、各自测试全绿，接起来之后仍然什么都不发生。
> 真跑一次才连着挖出 9 处断点，**每一处都是静默的**——不报错、不退出、
> 任务看起来活着却什么都不干。

1. ~~调度器没有接进任何进程~~ → 现在随内部 app(8475) 的 lifespan 启动，
   由 `PROTFORGE_WEB_WORKERS=1` 统一控制（调度器 + 打包器 + 清理器三个一起）。
   **必须与内部 app 同进程**：agent 令牌和管理员 RPC 都在那边，跨进程共享不到那张
   内存令牌表，症状是 agent 每一步回调都 401。
2. ~~清理器没有被调度~~ → 与调度器同一个开关，后台线程每 30 分钟扫一次。
3. ~~用户面板前端没被主服务发出来~~ → `http://127.0.0.1:8473/` 返回 200，
   `/app.js` `/style.css` 同样 200。
   **不要**改成 `app.mount("/", StaticFiles(...))`：那会把整棵树接管，
   `POST /api/tasks/1/advance` 从 404 变 405、detail 不再是中文，
   「用户侧没有 /advance」这条设计被悄悄改掉。现在是三个文件的单段白名单路由。
4. ~~没有初始管理员~~ → `.env` 里设 `PROTFORGE_ADMIN_USER` / `PROTFORGE_ADMIN_PASSWORD`
   自动创建；**同名用户已存在时绝不覆盖**（覆盖等于用一次重启把现有人踢出系统）。
5. ~~模型凭据的传递方式待确认~~ → 调度器为**每个任务**物化一份
   `work/<id>/.pi-agent/models.json`，`apiKey` 写成 `$MINIMAX_API_KEY` 由 pi 运行时插值，
   **密钥不落盘**。不去改 `%USERPROFILE%\.pi\agent`——那是全机器 pi 的配置，
   平台的行为不该依赖操作者的家目录状态。
   另外：`createAgentSessionServices({model})` **不会**传到 runtime，agent 会悄悄
   用内置 catalog 的第一个可用模型（本机是 minimax/M2.7，打到 `api.minimaxi.com`
   于是 401）。所以派活时先发一条 RPC `set_model`，它带回应、成功与否能验。
6. ~~产物回传未闭环~~ → `FinishIn` 现在收下 `artifacts`（pydantic 对未声明的键是
   **静默丢弃**的，agent 以为报了、后端当没收到）。产物发现扫**两处**：
   ProtForge outputs 根（前缀/时间窗，verified=0 待管理员确认）**与任务工作目录**
   （任务独占，一律 verified=1）。只扫前者是不够的——实测 agent 把结果全写在
   工作目录里，outputs 根是空的，于是任务 done 却没有 zip。

### 仍然未验证 / 未接线

1. **超时档位只实测了 pesto / esm 两个** —— 其余技能仍按「秒级 / 对接类 / 生成类」
   三档的归类给值。生成类那档（`TIMEOUT_SLOW_S` 8h）是给 rfdiffusion / bindcraft /
   gromacs / proteinmpnn 的，**未实测**。pesto / esm 那两次也是**热容器 + 小输入**，
   冷启动没有单独计时。
2. ~~**调度器重启恢复只在测试里验过**~~ → 已在真机上验过（见下）。
   期间还因此发现并修掉了一个真机才会暴露的缺陷：补扫收尾后没把认领锁还回去，
   导致同一个任务**永远**进不了第二次补扫。
3. ~~**管理员面板的干预动作没做过真机联调**~~ → 已在真机上按过（2026-10-17）。
   插话 / 追加要求 / 切换档位三项都打通，审计记为 `admin_rpc` actor=admin；
   「中止 / 重跑 / 改状态」三个按钮也都验过（中止是两步确认后杀进程树，
   与透传给 agent 是两回事，这点与原先的判断一致）。

### 真机验证记录（2026-10-16）

对着真实数据盘 `<数据盘根>` 起内部 app，验证重启恢复：

- 库里有 4 条卡了一整天的 `running`（本该被上一版代码根治却没根治的），
  启动后全部被收尾成 `failed`，reason 如实写着「服务重启前未留下运行记录，
  无法确认计算进程是否还在，已按中断处理」——**没有假装杀掉了进程**。
- 新建一条任务置 `running`、不落运行态记录，再起一次服务：被收尾，且
  `run/` 下**没有**留下认领锁。
- 由此发现上面那个 bug：第一次跑（修复前）在 `run/` 留下了 4 个
  `*-no-record.claim` 没人删。补扫这一路的 run_id 是固定哨兵，残锁意味着
  **那个 task_id 永远进不了补扫**——同一个任务第二次以同样方式卡住时就再也
  扫不掉了。记录那条路（`_recover_one`）本来就有 `finally` 释放，只有新加的
  补扫漏了。已修（`e12ffa4`），并加了「锁被还回去」「同一条任务连扫两次都
  能收尾」两条用例；围栏验证：摘掉释放那行，13 条挂 2。
- 那 4 个旧残锁已在 2026-10-17 清掉（对应任务都是终态，纯惰性垃圾）。

### 全分支 review 修过一轮（2026-10-16）

一次全分支审查找出 11 个缺陷 / 3 个高危，逐条修完并给每条配了围栏。
两件事值得单独记下来，因为它们都不是「代码写错了」，而是**测试在替生产撒谎**：

1. **清理器的形态闸与生产命名对不上** —— 闸门要求目录名 `^\d+$`，可生产写的是
   `agent-N.log` / `task_N.zip`。结果是 `logs/` 与 `packages/` 的保留策略**一次都没执行过**，
   而测试用的是纯数字名，断言恒真。现在形态按区域拆开，并加了命名漂移哨兵。
2. **下载端点对每一个真实任务恒 409** —— 产物写到 `data\packages\`，而路径守卫的
   区域表里**没有 packages**，于是真实产物一律被判越界，用户永远拿不到结果包。
   391 个测试全绿，因为夹具把假 zip 写进了 `work\`——一个生产中不存在的布局。

教训：**端到端路径的测试必须由生产的同一个函数/常量生成路径**。测试里手拼目录名，
就是下一个"全绿但全废"的入口。详见 `.superpowers/sdd/2026-10-16-protforge-web/review-round-1.md`。

下载端点现在走 `guards.resolve_package()`，只认一个文件：
`<数据盘>\packages\task_<本任务号>.zip`。work/uploads/outputs/inputs 四个区里的文件、
别的任务的包、`packages` 下的其它文件、子目录里的同名文件一律拒；越界仍是 409 且
不回吐真实路径。

---

## 常见排查

**浏览器打开 8473 是 404**
现在的实现里用户面板前端是**已经接上**的：`/`、`/app.js`、`/style.css` 都返回 200。
如果 404，先探 `<http://127.0.0.1:8473/api/health>`——它返回 `{"ok":true,...}` 说明
后端活着，问题在前端那三个文件的单段白名单路由（`server/app.py` 末尾）上。
404 也可能是端口被别的程序占了，见下一条。

**提示端口被占 / 起来就退**
先分清是谁占的：

```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 8473,8474,8475 }
```

如果 8473 上跑的是本平台，`start.bat` 会自己探活并直接开浏览器，不会重复起；
若返回 200 但不是本平台，说明端口被别的程序占了，先关掉那个程序。

**agent 进程起不来**
先看**内部 API 那个窗口（8475）**的输出——spawn 失败只会在那里报，网页上看不到。
常见三种：

- `node` 不在 PATH 里 → 配 `PROTFORGE_WEB_NODE` 指向真实路径（本机在
  `C:\Program Files\nodejs\node.exe`）。
- 缺 `MINIMAX_API_KEY` → 只影响联网搜索，agent 仍会起来。
- 技能目录读不到 → 看窗口里 `loadSkillsFromDir` 的诊断输出，用
  `PROTFORGE_WEB_SKILLS_DIR` / `PROTFORGE_SKILLS_DIR` 指到真实目录。

调度器已经接在内部 app 的 lifespan 上了（见上面「曾经还没连上的六条」第 1 条），
所以 agent 起不来就是上面三种原因之一，不用再去别处找。

**产物下载不了**
按这个顺序看：

1. 任务状态是不是终态——非终态下载接口会返回 409「任务还没结束」。
2. 任务记录里 `package_path` 是不是空的——空就是 409「这个任务没有产物包」。
   产物是 agent 跑完 + 发现 + 确认三步才落到 `data\packages\` 的。
3. `<数据盘根>\packages\` 里有没有那个 zip，以及有没有被清理器回收（保留 30 天）。
4. 路径是否越界——`package_path` 必须是 `<数据盘>\packages\task_<本任务号>.zip` 这一个文件，
   任何别的位置（work/uploads/outputs/inputs 区的文件、别的任务的包、子目录里的同名文件）
   都会被守卫挡掉，同样返回 409。

**网页上看不到实时的进度**
事件流是 SSE。反代必须关掉 `proxy_buffering`（上面的 nginx 示例已经关了）；
不改反代直连 <http://127.0.0.1:8473/> 可以排除是不是反代的问题。
即使 SSE 断了也有 3 秒轮询兜底，刷新页面总能看到最新状态。

---

## 开发

```bat
cd /d <仓库>
"<你的 Python>" -m pytest tests -q
cd agent
node guards.test.mjs
```

测试一律走真实 HTTP（临时端口 + 临时库），不会碰 `<数据盘根>\web.db`。
前端三个文件零 CDN、零构建，改完只能用真浏览器看一眼，没有自动化测试。

| 是什么 | 在哪 |
| --- | --- |
| 主服务（public / internal 两个 app） | `server/app.py` |
| 管理员面板后端 | `admin/app_admin.py` |
| 用户面板前端 | `web\index.html` `app.js` `style.css` |
| 管理员前端 | `admin-ui\index.html` `app.js` `style.css` |
| agent 端 | `agent\run.mjs` `tools.mjs` `guards.mjs` |
| 调度器 / 清理器 / 产物 / 守卫 | `server\dispatcher.py` `janitor.py` `artifacts.py` `guards.py` |

设计规格在 `docs/superpowers/specs/2026-10-16-protforge-web-design.md`，
实施计划在 `docs/superpowers/plans/2026-10-16-protforge-web.md`，代码注释大量引用它们的章节号。

## 许可证

平台本身的代码以 **MIT** 发布（见 [`LICENSE`](LICENSE)）。

它**不**替第三方工具授权：平台调用的是
[dy12021/protforge](https://github.com/dy12021/protforge) 里的技能脚本，
那些脚本包装的第三方工具各自遵循其原始许可（ESMFold2 / ESMC 为 MIT、
OpenDDE 为 Apache-2.0 等），逐项清单见该仓的「许可与出处」一节与
`skills/protforge-sandwich-antibody/references/license-registry.md`。
