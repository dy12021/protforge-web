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

> 第一次用会先撞上两个已知缺口（**没有初始管理员**、**用户面板前端还没被主服务发出来**），
> 细节见下面「已验证 / 尚未验证」。这两个是当前实现的真实状态，不是你装错了。

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

**代码在 D 盘，数据在 E 盘**（用户明确要求；ProtForge 自己的产物本来也在 E 盘，跑任务不跨卷拷贝）。

| 内容 | 位置 | 保留 |
| --- | --- | --- |
| 代码 | `.\` | git 仓库 |
| 数据库 | `data\web.db` | 审计日志留 30 天 |
| 上传原件 | `data\uploads\<id>\` | 7 天（只读） |
| agent 工作区 | `data\work\<id>\` | 7 天 |
| 产物包 zip | `data\packages\` | 30 天，磁盘紧张按最旧优先提前回收 |
| 日志 | `data\logs\` | 14 天，单文件上限 64 MB |

- **ProtForge 的产物留在 `../protforge/data\outputs\`，本项目只引用不搬动。**
- 上传原件进工作区时用 **hardlink**（同一卷不占额外数据块），跨卷或不支持才退回复制。
- 单任务磁盘上限 20 GB（产物 + zip 合计），数据盘剩余不足 50 GB 时拒绝新任务。

**备份就是把整个数据盘拷走**：

```bat
xcopy data D:\backup\protforge-web-data\ /E /I
```

恢复：拷回 `data\` 覆盖，再重启 `start.bat`。
`data\` 不入版本库。

---

## 环境变量

**代码只从进程环境读变量，自己不加载 `.env`。** `start.bat` 会在拉起服务前载入项目根目录的
`.env`（没有就跳过，用代码里的默认值）。所以手动起服务时，得自己先把变量设好。
从 `.env.example` 复制改名，**任何密钥都不要提交进仓库**。

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `PROTFORGE_WEB_DATA_ROOT` | 数据盘根 | `data` |
| `PROTFORGE_WEB_DB` | 数据库路径 | `<数据盘根>\web.db` |
| `PROTFORGE_WEB_INTERNAL` | 内部 API 基址，**不含 `/api`** | `http://127.0.0.1:8475` |
| `PROTFORGE_WEB_SKILLS_DIR` | 服务端读的技能目录 | `../protforge/skills` |
| `PROTFORGE_SKILLS_DIR` | agent 读的技能目录（同一个） | `../protforge/skills` |
| `PROTFORGE_DATA` | ProtForge 只读数据区 | `../protforge/data` |
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
是**尽力而为**：它挡手滑、挡误删 `../protforge`，**挡不住刻意绕过**。

所以它是一层**防误操作**的护栏，不是安全边界。真要对外开放，必须换成真沙箱
（容器 / 独立账号 / 资源限制），而不是继续加白名单。

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
- **保留策略**：`janitor.sweep()` 按 spec §3.05.2 实现，只删本项目数据盘下、目录名匹配
  `<数字>_<slug>` 的路径，有测试覆盖。
- **agent 端**：AST 命令守卫 + pi RPC 模式 + 步骤回传可用，有单测。

### 尚未验证 / 未接线（**当前实现的真实状态**）

这几条不是「待优化」，是**还没连上**，照着做会撞上：

1. **调度器没有接进任何进程** —— `dispatcher.py` 没有 `main()`，
   `create_public_app()` / `create_internal_app()` 里也没有任何 lifespan 去调
   `Dispatcher.start()`；全仓库只有测试在实例化它。
   **后果：三个服务起来后，提交任务不会有 agent 被 spawn，任务会一直停着。**
   调度器也没有独立启动入口，所以 `start.bat` 里也没有（更没有）替它单起一个进程。
2. **清理器没有被调度** —— `janitor.sweep()` 只有测试调用，没有「每小时扫一次」的循环。
   保留策略目前是「函数写好了但没人定时调」。
3. **用户面板前端没被主服务发出来** —— public app 没有 `/` 路由也没挂静态目录
   （只有管理员面板自己发 `admin-ui/`）。**所以 `http://127.0.0.1:8473/` 现在是 404**，
   `start.bat` 自动打开浏览器会看到 404，这是预期内的当前状态。
4. **没有初始管理员** —— `db.init_db()` 不建用户，而管理员面板要登录才能建用户。
   第一个管理员需要手工建一次：
   ```bat
   cd /d .\server
   "C:\ProgramData\anaconda3\python.exe" -c "import auth; print(auth.create_user('你的用户名', '至少10位的密码', 'admin'))"
   ```
5. **模型凭据经 spawn 的传递方式待确认** —— 仓库里**没有任何代码读 LLM 相关环境变量**。
   模型与密钥走 pi 自己的 agent 目录（本机 `%USERPROFILE%\.pi\agent\` 下已有 `auth.json`
   与 `models-store.json`）。`agent/run.mjs` 里只把 `MINIMAX_API_KEY` 用于 MCP 搜索，
   **模型本身**（预期 `https://api.minimax.cn/v1` + `MiniMax-M3.1-Flash-Preview`）
   是否能被调度器 spawn 出来的 agent 用上，**尚未联调验证**。
6. **产物回传未闭环** —— agent 的 `finish` 会带 `artifacts` 字段，但服务端 `FinishIn`
   只有 `summary` / `error`，多余的 `artifacts` 会被 pydantic 静默丢弃；
   `package_path` 得走产物发现/确认那条路。产物能否真正出现并可下载，**待联调验证**。

---

## 常见排查

**浏览器打开 8473 是 404**
不是你的问题。见上面「尚未验证」第 3 条：用户面板前端还没被主服务发出来。
接口本身是活的，可以先探 `<http://127.0.0.1:8473/api/health>`，返回 `{"ok":true,...}` 就说明服务正常。

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

不过在调度器接线之前（见「尚未验证」第 1 条），**根本不会有 agent 被 spawn**，
所以这一类现象要先看第 1 条是不是还没解决。

**产物下载不了**
按这个顺序看：

1. 任务状态是不是终态——非终态下载接口会返回 409「任务还没结束」。
2. 任务记录里 `package_path` 是不是空的——空就是 409「这个任务没有产物包」。
   产物是 agent 跑完 + 发现 + 确认三步才落到 `data\packages\` 的。
3. `data\packages\` 里有没有那个 zip，以及有没有被清理器回收（保留 30 天）。
4. 路径是否越界——越界的 `package_path` 会被路径守卫拦掉，同样返回 409。

**网页上看不到实时的进度**
事件流是 SSE。反代必须关掉 `proxy_buffering`（上面的 nginx 示例已经关了）；
不改反代直连 <http://127.0.0.1:8473/> 可以排除是不是反代的问题。
即使 SSE 断了也有 3 秒轮询兜底，刷新页面总能看到最新状态。

---

## 开发

```bat
cd /d .
"C:\ProgramData\anaconda3\python.exe" -m pytest tests -q
cd agent
node guards.test.mjs
```

测试一律走真实 HTTP（临时端口 + 临时库），不会碰 `data\web.db`。
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
