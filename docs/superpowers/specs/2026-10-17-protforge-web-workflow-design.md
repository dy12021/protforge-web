# 工作流（Excel 批量计算）设计

- 状态：**草稿，待用户确认**（已过一轮独立审查并逐条修订）
- 日期：2026-10-17
- 关联：`docs/superpowers/specs/2026-10-16-protforge-web-design.md`（平台本体）

---

## 1. 要解决的问题

用户手里有一张 Excel（比如 500 行候选抗体，每行一个序列），希望：

1. 上传这张表并预览
2. 选其中几列作为「工作流的目标区域」
3. 配一组固定的提示词，提示词里含若干可变参数（`{{变量}}`）
4. 把表里的列映射到这些变量上
5. **每一行生成一个计算任务**，任务之间上下文完全独立
6. 工作流任务排在该用户所有常规任务之后
7. 一个任务跑完 → 自动把结果归档回工作流表的指定区域 → 再跑下一个
8. 界面要为「批量」这个场景优化，不能把几百个任务糊在常规看板上

## 2. 已确认的决策

| 决策点 | 结论 |
| --- | --- |
| 「目标区域」含义 | 每一行一个任务；选中列作为输入，其余列可作固定上下文映射进提示词变量 |
| 结果去处 | 平台内 + 回写 Excel，**回写是可开关** |
| 单行失败 | 记录原因、归档该行、**继续下一行** |
| 行任务怎么排队 | 游标式：工作流记录 + 按需生成任务（方案 A） |
| 表头行 | 默认第 1 行，可指定行号 |
| 删除工作流 | 连带删除其行任务记录；删除前先中止在跑的行任务 |
| 上传上限 | 20 MB；单元格文本截断到 8000 字符 |

## 3. 现状勘察结论（决定了方案）

这些不是推测，是对着当前代码查出来的：

- `server/app.py` 的 `create_public_app()` **没有任何上传端点**。`uploads` 表、`uploads/<task_id>/` 目录、janitor 的 7 天保留策略都已存在，但**接线没做**。上传 Excel 是从零新建。
- `tasks.priority` 字段存在，但**没有任何代码写过它**，默认全 0，所以队列实际是纯 FIFO。排序语句在 `dispatcher._queued_tasks()` 里（`dispatcher.py:1143`）：`rows.sort(key=lambda r: (-priority, id))`。
- `_queued_tasks()` 拉的是 `limit=200`，且**不分用户**，是全局队列。
- `POLL_INTERVAL = 1.0`，`tick()` 每秒一次；`self._wake.set()` 只在 `stop()` 里调用，**没有事件驱动的唤醒**。所以上一行归档后放行下一行的延迟 ≤ 1 秒。
- `janitor` 只扫 `work` / `uploads` / `logs` / `packages`，新增的 `workflows/` 目录不会被误清。
- `python-multipart 0.0.32`、`openpyxl 3.1.5` 均已安装。

**本文所有 `文件:行号` 锚点已逐条回核过当前代码**（2026-10-17，HEAD `1dccd56`）。回核改了 6 处漂移的行号、纠正了 1 处写反的事实断言（见 8.5）。写计划时**仍以函数名为准**：本项目文件还在动，行号第二次就会过期。

## 4. 数据模型

### 4.1 三张新表

`SCHEMA` 里 **workflows / workflow_columns / workflow_skips 必须排在 `tasks` 之前**——`tasks.workflow_id` 带外键指向 `workflows`，SQLite 不在建表时校验目标是否存在，但把依赖表排在前面能免掉一整类「建库顺序」问题。

```sql
CREATE TABLE workflows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sheet TEXT NOT NULL DEFAULT '',
  source_path TEXT NOT NULL DEFAULT '',
  prompt_template TEXT NOT NULL,
  header_row INTEGER NOT NULL DEFAULT 1,
  key_col INTEGER NOT NULL DEFAULT 0,
  total_rows INTEGER NOT NULL DEFAULT 0,
  cursor INTEGER NOT NULL DEFAULT 0,
  writeback INTEGER NOT NULL DEFAULT 0,
  writeback_pending INTEGER NOT NULL DEFAULT 0,
  abort_requested INTEGER NOT NULL DEFAULT 0,
  result_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK (status IN ('draft','running','paused','finished'))
);
CREATE INDEX idx_workflows_owner ON workflows(owner_id, status);

CREATE TABLE workflow_columns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  col_index INTEGER NOT NULL,
  header TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'ignore',
  slot TEXT NOT NULL DEFAULT '',
  field TEXT NOT NULL DEFAULT '',
  UNIQUE(workflow_id, col_index),
  CHECK (role IN ('key','input','context','output','ignore'))
);

CREATE TABLE workflow_skips (
  workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  row_index INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (workflow_id, row_index)
);
```

`workflow_skips` 是**删除守卫的必要配套**（理由见 6.5），不是可选功能——没有它，用户想跳过一行就只能去删任务，而删任务会让整批的账对不上（6.5 洞三）。

`writeback_pending` / `abort_requested` 两个布尔列是刻意用列而不是状态值实现的：`workflows.status` 的 `CHECK` 约束改动属于 schema 变更，而 4.3 已经确认过这条路的迁移代价（见 6.6 末尾）。用两个 0/1 列把「暂停但还要杀掉当前行」「归档成功但 xlsx 被占用待补写」这两种**附加语义**从状态机里剥出来，`status` 就永远只有四态。

### 4.2 `tasks` 加三列

```sql
ALTER TABLE tasks ADD COLUMN workflow_id INTEGER REFERENCES workflows(id) ON DELETE CASCADE;
ALTER TABLE tasks ADD COLUMN workflow_row INTEGER;
ALTER TABLE tasks ADD COLUMN workflow_archived INTEGER NOT NULL DEFAULT 0;
```

前两列为空的仍是普通任务。**这正是「工作流行就是带三个字段的普通任务」的全部改动**——中止、重跑、产物包、审计、详情页、下载、SSE 实时流全部原样复用，不造第二套任务生命周期。

第三列 `workflow_archived` 是**闸门②唯一的判据来源**，没有它整批会锁死（推演见 6.3）：闸门要问「这一行归档了吗」，而归档这件事本身在库里不留痕——xlsx 不查询，平台内的展示字段也不区分「刚跑完」和「已写回表里」。

**它必须挂在行任务上、不能是工作流上的一个游标**：管理员可以重跑**任意一行**（6.5 的洞一）。若用 `archived_cursor` 表达「已归档到第几行」，重跑第 3 行时它的 `workflow_row=3 < archived_cursor`，归档扫描会永远跳过它，重跑的结果写不回表里——一次重跑悄悄丢失。挂在行上，重跑只需把该行的 `workflow_archived` 清回 0，重跑完自然会再被归档一次。

### 4.3 ⚠ schema 迁移：必须显式写，不能靠 `init_db()`

`server/db.py:136` 的 `init_db()` 注释明写「**不做版本化迁移**」，实现全是 `CREATE TABLE IF NOT EXISTS`。**这对已存在的表是空操作**——线上库 `data\web.db` 早已建好，直接改 `SCHEMA` 常量里的 `tasks` 定义，新列**永远不会出现**，而所有代码都照常跑，只是那一列永远是 NULL。这正是这个项目历史上「测试全绿、线上静默失效」的典型形状。

所以 `init_db()` 里必须加一段幂等的列补齐：

```python
_MISSING_TASK_COLUMNS = (
    "workflow_id INTEGER REFERENCES workflows(id) ON DELETE CASCADE",
    "workflow_row INTEGER",
    "workflow_archived INTEGER NOT NULL DEFAULT 0",
)

def _add_missing_columns(c):
    have = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    for spec in _MISSING_TASK_COLUMNS:
        name = spec.split()[0]
        if name not in have:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {spec}")
```

新库走 `SCHEMA` 里加好的完整定义，旧库走这段补齐。**围栏必须拿一个「已存在的旧库」跑一遍**，只测新库等于没测。

**两条路得到的不是「同一个结果」，只是「同一组列名」。** 这一点是实现期实测出来的，我原来那句话写错了：`ALTER TABLE ADD COLUMN` **只能追加**，所以迁移库的 `tasks` 列顺序必然是新库那种顺序的另一种排列：

```
新库    [..., session_id, workflow_id, workflow_row, workflow_archived, created_at, updated_at, done_at]
迁移库  [..., session_id, created_at, updated_at, done_at, workflow_id, workflow_row, workflow_archived]
```

当前没有功能影响（`store` 里全部走 `dict(row)` 按名取键，没有任何位置解包），但**写代码必须按名取**。所以围栏钉的是**列名集合相等**而不是顺序相等，并且顺带把顺序确实不同这件事显式断言出来——不然将来有人写位置解包，任何测试都拦不住。

**同一类陷阱还有一个更狠的版本**：建在新补齐列上的索引**不能写进 `SCHEMA`**。`init_db()` 里 `executescript(SCHEMA)` 跑在补列**之前**，旧库那时还没有 `workflow_id`，一句 `CREATE INDEX ... ON tasks(workflow_id)` 会让 `init_db()` 直接抛 `no such column: workflow_id`——**三列永远补不上**，而且 `executescript` 中途中断会让排在后面的 `uploads`/`artifacts`/`events` 建表一起落空。索引不需要列补齐那套读-判断-写（`CREATE INDEX IF NOT EXISTS` 对已存在的库本来就会补建），但**不能早于补列执行**：落法是补列之后再单独跑一次这段 DDL。

**但「幂等」在本机不是自动成立的——平台有三个进程同时开着这一个库**（public 8473 / admin 8474 / internal 8475），它们都在启动时调 `init_db()`。上例那段代码是**读-判断-写**三步，两个进程同时进来时都可能看到「这一列还没有」，然后各自执行一次 `ALTER TABLE`，后到的那个拿到 `sqlite3.OperationalError: duplicate column name`。

所以补齐必须包在事务里，并容忍「重复列」这一种错误：

```python
def _add_missing_columns(c):
    have = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    for spec in _MISSING_TASK_COLUMNS:
        name = spec.split()[0]
        if name in have:
            continue
        try:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {spec}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise          # 别的错误不能吞：吞了就是静默半迁移
```

`if "duplicate column" not in ... → raise` 这半句是重点：宽泛的 `except` 会把磁盘满、库被锁、语法错一起吞掉，迁移失败却报告成功。围栏要造一次并发启动（三个进程同时起），断言全部正常退出且三列都在。

### 4.4 磁盘

新增 `data\workflows\<workflow_id>\`：

- `source.xlsx` —— 上传的原始表，**永不修改**
- `result.xlsx` —— 回写开关打开时的结果副本，用户下载的就是它

**刻意放在 `guards.regions()` 的四个区域之外**。agent 不该有权限碰用户的表格，回写一律由平台做。

### 4.5 上传落盘：三个坑，都是实测出来的

`UploadFile` / `File(...)` 在当前 FastAPI 0.136 + Starlette 1.3.1 上**可用，不用改依赖**（`workspace/probe_upload.py` 实测）。但框架对上传内容**零校验**，三个坑要自己堵：

**① `file.filename` 是用户可控的。** 直接拿它拼落盘路径就是目录穿越（传 `../../evil.xlsx`）。落盘时**只用固定名 `source.xlsx`**，原文件名只作为展示字段存库、且要过一道白名单（去路径分隔符、限长度）。

**② 空文件和伪造类型框架都不拦。** `empty.xlsx` 和 `evil.sh` 实测都是 200 照收。必须自己判：扩展名 + 魔数（xlsx 是 zip，头是 `PK\x03\x04`）+ 大小上限 + 空文件。早知道这条能省掉一次「用户传了空表，平台建出一个 0 行工作流然后永远不跑」的排障。

**③ 落盘顺序：先 staging，解析成功再建库记录。**

```
上传 → 落 data\workflows\staging\<uuid>.xlsx（uuid 自己生成，不含用户输入）
     → 校验 + openpyxl 解析（表名/表头/行数/列数）
     → 解析失败：删 staging，**库里不留任何痕**
     → 解析成功：建 workflows 行拿 id → 把 staging 搬成 workflows\<id>\source.xlsx
```

反过来（先建库记录再解析）的话，解析失败要回滚一行记录加一个目录，两处都得清，漏一处就留下垃圾。

## 5. 列角色模型

预览时用户给每列打一个角色：

| 角色 | 数量约束 | 取值方式 | 作用 |
| --- | --- | --- | --- |
| `key` | 恰好 1 列（默认第 1 列） | 本行 | 生成任务标题，让你认得出是哪一行 |
| `input` | 0..n | **本行** | 绑一个 `{{变量名}}`，逐行变化——序列、编号、浓度 |
| `context` | 0..n | **整表一个值** | 绑一个 `{{变量名}}`，所有行共用——项目代号、实验条件、参考序列 |
| `output` | 0..n | 写回 | 每列选一个语义字段，见第 7 节 |
| `ignore` | 其余 | — | 真的不参与 |

**`context` 这一格是用户明确要的**：「选取某些列作为目标区域……把表格中的**固定列**映射到这些可变参数里面」。整列取值恒定的那些列就是「固定列」，它们不进工作流的目标区域，但要进每一条任务的提示词当上下文。

`input` 和 `context` 在数据模型上只差 `role` 一个字段，但**校验和提示都要分开**：

- `input` 的 slot 在预览表里逐行变化，用户要能看出「第 5 行的这句和第 6 行不一样」
- `context` 的 slot 在预览里是**同一个值重复出现**，界面上应当合并显示成一条「上下文：项目代号 = XBR-2026」，而不是在每一行里重复

校验时两者的 slot 一视同仁（都必须出现在模板里），但**取值路径不同**：`context` 取第一行数据行的值（整列恒定时与取哪一行无关），若发现该列并非整列恒定，要**明确报错**而不是悄悄取第一行——否则用户以为整表共用一个值，实际前几行悄悄不同。

**「目标区域」= `key` + `input` 组合出来的列集合**，界面上高亮显示；`context` 列在旁边用另一种底色标出，表示"不进目标区域但参与每条任务"。

### 5.1 建工作流时的强校验

- 模板里出现的每个 `{{变量}}` 必须有 `input` 或 `context` 列绑定，否则**拒绝保存**
- 每个 `input` / `context` 列的 slot 必须在模板里出现，否则提示「这一列绑了但模板里没用到，要不要改成忽略」
- 模板里有重复 `{{x}}` 允许（就是想让同一列填两处）
- 单元格为空 → 渲染成空串、**不报错**，但预览里标黄
- **`context` 列若非整列恒定 → 拒绝保存**（见上）
- **全空数据行在解析阶段就剔除**，不进 `total_rows`。真实 Excel 尾部常有一两行格式残留，`ws.max_row` 会把它们算成数据行——不剔的话工作流会跑出一堆「输入全空」的行任务，每行都白烧一次完整计算

### 5.2 行号约定（三个地方必须一致）

- 「行」一律指**数据行**，不含表头行、也不含被剔除的全空行
- `total_rows` = 有效数据行数；`cursor` 是**下一个待放行数据行的 0 基序号**
- `key_col` / `col_index` 是 **0 基列号**（相对整张 sheet，不是不包含表头的偏移）
- 因此数据行序号 r 对应的 sheet 行号是 `header_row + 1 + r`（跳过被剔除的行时用预先算好的行号映射表）。这个换算只在 `server/workflow.py` 里出现一次，别散在各处

### 5.3 取值：公式必须回退

读单元格用 `data_only=True` 拿计算值。**实测：openpyxl 自己写的公式，`data_only=True` 读回来就是 `None`。** 所以回退逻辑是必需的：

```
值 = data_only 读的值
若 值为 None 且 非 data_only 读的原文非空且以 "=" 开头:
    值 = 公式原文
```

不回退的表现是：别的工具生成的表整列喂进提示词全是 `None`，而任务照跑不误，只是算了个空。

## 6. 调度与串行

### 6.1 常规队列排除工作流任务

`_queued_tasks()` 过滤掉 `workflow_id IS NOT NULL` 的行。它们有自己更严的闸门，走常规通道会绕过。

### 6.2 工作流巡检（挂进 `tick()`，位置有讲究）

`tick()` 现在是 `self._reap()` 然后 `self._admit()`。工作流巡检拆成两半挂在**不同位置**：

```python
def tick(self):
    self._reap()
    admitted = self._admit()                    # 常规任务先把空槽填满
    admitted += self._workflow_run_pass()       # 放行：只吃 _admit 没填满的槽
    self._workflow_archive_pass()               # 归档：放行之后做，理由见本节末
    return admitted
```

**两个 pass 各自 `try/except`，异常只记日志，绝不越过 `tick()` 的中线。** 这一条不是洁癖：`_loop()` 是 `try: tick() except: log.exception(...)`（`dispatcher.py:1091-1095`），一次异常就让本轮 `_admit()` 根本没机会执行——而归档 pass 里最容易抛的恰好是 openpyxl 写文件（`PermissionError` / 磁盘满 / `os.replace` 失败）。那样用户会看到**所有常规任务永远停在「排队中」**，日志里每秒一条「调度器轮询出错」，而真正的原因只是一张 Excel 被占用。

**为什么放行必须排在 `_admit()` 之后**：`_admit` 会把空出来的并发槽用常规任务填满，工作流只吃**剩下的空槽**。这样「让路给常规任务」是**顺序带来的结构性结果**，不需要额外查库。反过来（工作流先放行）的话，一个 500 行的工作流会一次占满所有槽，把别人的任务堵到它跑完——而按「失败继续」的策略，它可能跑好几小时。

### 6.3 「归档了吗」是独立于游标的事实（这一处错过，整批锁死）

先说清楚三个量的分工，混用一个就会死锁：

| 量 | 含义 | 谁改 |
| --- | --- | --- |
| `cursor` | 下一个**尚未放行**的数据行序号 | 只在放行时 `+= 1`，单调递增 |
| `tasks.workflow_row` | 这一行任务对应第几行 | 建任务时写死 |
| `tasks.workflow_archived` | 这一行的结果**已经写回表**了吗 | 归档 pass 置 1；重跑时清 0 |

**放行时就前移游标。** 我写反过一版：原先写「放行不动物标、归档时才按条数前移」，再让归档扫 `row < cursor`。推演一遍是死循环——`cursor=0` → 放行 row 0（游标仍 0）→ row 0 跑完 → 归档谓词 `0 < 0` 为假 → 不归档 → 闸门看见 row 0 已终态 → **又放行 row 0** → 无限重复。**这是主干路径，不是边缘情况。**

**但改成「放行时前移、归档扫 `row < cursor`」之后，死循环变成另一种形式——管理员一重跑就锁死。** 闸门②要判「还有行没归档」，若谓词是「`row < cursor` 的行」，那**所有历史行**都算没归档（含早就归档过的），于是 ② 恒真，工作流再也不放行。更隐蔽的是它只在管理员用过重跑之后才发作，平时完全正常。

正确的判据是**行自己的 `workflow_archived` 标记**，与游标无关：

```
未归档 = EXISTS (SELECT 1 FROM tasks
                 WHERE workflow_id = ? AND workflow_archived = 0)
```

三个量这样分工之后，「先归档再下一个」和「重跑不会被卡住」同时成立：归档 pass 扫「终态 且 `workflow_archived = 0`」的行，写回后置 1；管理员重跑把该行清回 0，它自然会被重新归档。**「谁把 1 清回 0」这件事必须写进 `retry` 的实现里**（6.5），漏了就是一次静默丢结果。

### 6.4 放行闸门（**顺序本身是规格的一部分**）

```
放行前逐条判，任一条不满足就跳过本工作流：

① len(self.running) >= self.limit              → 跳过    ← 并发上限
④ 本 workflow 有 queued 的行任务？              → 认领它，返回   ← 见下，**必须排在②前面**
⑤ 本 workflow 有非终态的行任务？                 → 跳过    ← 串行
② 本 workflow 有 workflow_archived = 0 的行？   → 跳过    ← 「先归档再下一个」
③ cursor >= total_rows                         → 置 finished
⑥ 该用户还有非终态的常规任务？                   → 跳过    ← 「排在常规任务之后」
全过 → 读 cursor 行 → 渲染 → 建 task（带 workflow_id, workflow_row=cursor）
     → cursor += 1 → spawn
```

**④ 必须排在 ② 前面，否则「重跑」当场失效。** 这不是排版问题，是死锁：管理员重跑一行，`retry` 把 `status` 写成 `queued` 并清 `archived=0` —— 这条行**同时**满足②（有未归档的行）和④（有 queued 的行）。若②在前，它先命中并跳过，本工作流永不检查④；④永远走不到，那条重跑的行**永远停在「已排队」，且不报错**，而闸门⑤ 又挡着下一行。整批卡死，且只在管理员用过重跑之后才发作。

把④提到最前（并发闸门之后）就顺了：重跑的行被认领、spawn、跑完、归档，`archived` 回到 1，② 自然放行。**这也是为什么 ④ 是「认领并返回」而不是「认领然后继续往下判」**——认领完本轮该工作流的事就做完了，再往下判只会看到自己刚放出去的那一行然后跳过。

**① 是必须的，不是保险。** `dispatcher._admit()` 有 `if len(self.running) >= self.limit: return []`（`dispatcher.py:1148`），而 `_spawn`（`dispatcher.py:1175`）**没有**这道检查——它只做写 brief、发令牌、起进程，没有读 `self.running`。工作流任务被 6.1 排除在常规队列之外，所以除了这条闸门它们没有任何容量约束——两个 running 的工作流，或者一个工作流加一个刚被 `_admit` 放出去的常规任务，会在同一 tick 里各自放行，超发 GPU 槽，全部变慢。

**⑤ + ② 一起构成「一行只做一件事」。** 在跑的行：非终态（⑤ 命中）、未归档（② 也命中）。跑完没归档的行：终态（⑤ 不命中）、未归档（② 命中）。**真正把 ⑤ 和 ② 分开的是 ④ 已经处理掉的那个 queued 行**，所以五条判据合起来的效果就是「同一工作流同时只有一行在推进，且它推进完并归档之前不放下一行」。两条都保留是因为它们各自说得清一件事，删掉任何一条剩下的那条都更难读懂。

**⑥ 不依赖顺序**：`_admit` 先跑（顺序层让路给**所有人**），⑥ 再显式查「该用户有无 queued/running/packaging 的常规任务」。第 2 层不是冗余——顺序层依赖「工作流巡检排在 `_admit` 之后」这一行代码的位置，谁调整 `tick()` 顺序就悄悄失效，而失效表现是「工作流插到别人前面」，不报错。⑥ 把它变成可读、可测的契约。

### 6.5 放行要分两级，否则管理员一动就 stranded

审计现有能力时发现三个洞，都是「复用现有能力」的反例——**能力接上了，但接上之后语义是错的**：

**洞一：管理员「重跑」让行任务 stranded。** `admin/app_admin.py:486` 的 `retry` 把任务改回 `queued`。而 `_queued_tasks()` 按 6.1 要排除 `workflow_id IS NOT NULL`，于是这条任务**永远没人认领**，永远停在「已排队」，且不报错。

**洞二：「强制改状态」比洞一更严重。** `admin/app_admin.py:472` 的 `force_status` 原样写入管理员选的目标，只校验 `target not in db.VALID_STATUSES` 就抛（`:475`）——而 `VALID_STATUSES = STATUS_KEYS + ["failed"]`（`db.py:41`）含 `running` 和 `packaging`。管理员改成这两个值，库里就真写成非终态。`store.claim_queued()`（`store.py:281`）的 CAS 只吃 `WHERE status = 'queued'`（`store.py:341`），接不走；闸门⑤「有其他非终态行任务」从此恒真 → **整个工作流从这一行起永久卡死**，界面显示「正在跑第 N 行」而没有任何进程。

**洞三：`DELETE /api/tasks/{id}` 让工作流的账对不上。** `server/app.py:420` 没有任何守卫。删掉一个行任务后有两种后果，都不报错：

- 删的是**已终态但未归档**的行 → 那一行的结果**静默丢失**（归档再也扫不到它），而闸门② 因为该行不见了反而变成满足，整批继续往下跑——用户以为这一行做完了，表里那一行是空的
- 删的是**在跑**的行 → 记录没了但 agent 进程还在烧 GPU（机制见 6.7：`_db_status` 返回 `""`，`_abort_signal` 直接返回 `None`），闸门② 同时解除，工作流照常放行下一行，于是**并发槽被一个没人管的僵尸占着**

所以删除守卫仍然必要，只是理由从「活锁」变成了**账对不上 + 僵尸进程**——这两种都不报错。

修法分两层：

**放行侧**，把认领拆成两级，而不是给每个动作单独开端点：

```
放行时（闸门④）：
  本 workflow 有 queued 的行任务？→ 认领它（覆盖重跑产生的一切，queued 是它唯一的落点）
  否则                        → 按 6.4 走新建游标行的流程
```

**但两级放行只解决「认领得到」，还差半步：`retry` 必须把 `workflow_archived` 清回 0。** `admin/app_admin.py:486` 的 `retry` 现在只写 `status='queued'`。不清这个标记的话，被重跑的那一行带着 `archived=1`，闸门② 放行、④ 认领、它跑完也进终态——但归档 pass 扫的是「终态 且 `archived=0`」，于是**这行的新结果永远不写回表**，而平台上看起来一切正常（任务是 done 的，只是表里的值还是上一轮的）。

**这是全篇最安静的一种失效**：不报错、不卡住、界面上「已完成」，唯独 Excel 里的结果是旧的。所以围栏必须是端到端的——重跑一行、跑完、然后**打开 result.xlsx 断言那一行的值是新值**，断言任务状态是 done 是不够的。

**守卫侧**，把管理员的越界动作挡住——和删除守卫是同一类修法：

| 动作 | 对 `workflow_id IS NOT NULL` 的任务 |
| --- | --- |
| `DELETE /api/tasks/{id}` | **409**，指向「跳过这一行」或「删掉整个工作流」（理由见洞三） |
| `POST /api/admin/tasks/{id}/status` 改成 `running`/`packaging` | **409**，说明只能改成 `queued`（等价于重跑）或终态 |
| `POST /api/admin/tasks/{id}/retry` | 放行，但**必须同时清 `workflow_archived=0`**（上一节末） |

被 409 挡住的原因要写进响应文案：管理员看到的是「这是工作流行任务，改成「计算中」会让整批卡住」。不说的话他会以为是 bug。

**「跳过这一行」是删除守卫的必要配套，不是可选项**（表见 4.1）：没有它，用户想跳过一行就没有出路，而没有出路他就会去删任务，然后撞上洞三那两种后果。批量场景里「某行序列是坏的，跳过继续」本身也是高频需求。跳过一行 = 写 `workflow_skips` + 把该行 `status` 置终态（否则闸门② 会一直等它）+ 归档写「已跳过」。

### 6.6 中止、暂停、改状态

| 动作 | 语义 | 现状 |
| --- | --- | --- |
| 中止在跑的行任务 | `abort` 路由（`app.py:428`）在 `:487` 写成 `failed` → 归档照常推进 | **已通，不用改** |
| **暂停** | 改 `workflows.status='paused'`，**不碰正在跑的那一行** | 需实现 |
| **中止整批** | `status='paused'` + `abort_requested=1`，杀掉在跑的行任务 | 需实现 |
| 重跑一个终态行任务 | 回到 `queued` 且清 `archived`，被 6.5 的④接住 | 需 6.5 的两级放行 |
| 改状态到 `running`/`packaging` | **409** | 需守卫 |

**「暂停」不杀当前行是有意的**：杀掉更符合直觉，但代价是 GPU 烧了一半的计算直接作废，而批量场景里这一行通常已经跑了很久。文案写明「当前行会跑完再停」。

**「中止整批」必须杀掉当前行**，否则它会一直烧到自然结束，用户点了「中止」却还在跑。所以按钮文案分开写：暂停 = 「当前行会跑完再停」，中止整批 = 「立刻停掉当前行，整批不再继续」。

**`status` 不新增 `aborted` 值**：加第五个状态值要改 `CHECK` 约束，而 SQLite 不支持 `ALTER TABLE ... DROP CONSTRAINT`，只能 `RENAME` + 重建表 + 搬数据——为一次「中止整批」付这个迁移代价不值得。改用 `status='paused'` + `abort_requested=1`（4.1 已把这两个布尔列写进 DDL）：`abort_requested` 只影响「当前行要不要杀」，不影响状态机的四态。

### 6.7 删工作流的磁盘与进程

`DELETE /api/workflows/{id}` 只删数据库——`app.py:199-216` 的 docstring 明写「**只删数据库**」，磁盘交给 janitor（`work/` 7 天、`packages/` 30 天）。但有个坑：**级联删掉 tasks 行后，调度器不会去杀那个还在跑的 agent**——`_db_status` 读不到行返回 `""`，`_abort_signal` 因 `"" not in TERMINAL_STATUSES` 直接返回 `None`（`dispatcher.py:617-618`），于是进程继续烧 GPU 直到自己跑完。

**中止必须走数据库，不能走进程内方法。** 这一点在本机的进程拓扑下尤其要紧：`scripts/start-services.ps1:41` 对 public / internal / admin **三个进程都设了 `PROTFORGE_WEB_WORKERS=1`**，所以三个进程各自都建了一个 `Dispatcher()` 并 `start()`（`app.py:799-801`），靠 `claim_queued` 的 CAS 决定谁真正抢到任务并 spawn。结果是：

- public 进程里的 `app.state.dispatcher` **是活的，但它 `running` 里通常没有别人的 agent**——agent 是当时 CAS 赢的那个进程起的
- 在 public 里调 `app.state.dispatcher.abort(task_id)`，多数时候是**对着一个空字典操作**，静默无效
- `dispatcher.py:1108-1115` 的注释已经把这件事讲清楚了：「用户中止发生在 public app，那是**另一个进程**：那边改得到数据库，改不到这里的进程」

所以中止的唯一跨进程通道就是**现有 abort 路由在用的那套**：写 `status='failed'` + `actor='human'` + 写 `error`，持有该 agent 的那个 dispatcher 在下一次 `_reap`（≤1 秒）里经 `_abort_signal` 看到终态且 actor 是人，于是真去杀进程。`_abort_live_rows` 就照抄这一套，**不要发明新机制**。

删除流程因此是：**把在跑的行逐个写成 `failed`（actor=human）→ 等 `POLL_INTERVAL * 3` → 再删记录**。

等的那 3 秒是**保守估计而不是确认**——public 进程看不到 agent 是否真的死了（不在它的 `running` 里），能观测到的只有数据库。所以：

- 不能写成「轮询直到确认进程不在 `dispatcher.running` 里」，那个字典不是权威来源，照它判会得出「已经停了」的错误结论
- 3 秒短于一行任务的自然时长、长于调度器一个 tick，够把中止信号送到
- **顺序绝不能反**：先写 `failed` 再等，最后才删。反过来（先删）就没有 `failed` 可写了，`_db_status` 立刻返回 `""`，中止信号**根本产生不出来**——而这正是「删工作流留下一个烧到自然结束的僵尸」的确切成因

确认不了也**继续删**：不能让一个卡住的进程挡住用户删工作流。但要在事件里如实写「进程未能确认终止，请检查是否还有残留计算进程」——`running` 不是权威来源，说「已确认」就是撒谎。

### 6.8 开销与占用

`tick()` 每秒一次，所以巡检必须**在没有 running 工作流时一次库查询就返回**。有 `idx_workflows_owner(owner_id, status)` 支撑，`WHERE status='running'` 是索引命中。

**占用上限**：一个工作流同时最多占 **1 个**并发槽（串行保证的结果）。并发上限取 GPU 探测值（`dispatcher.py:777`，只允许 `max_concurrency` 往下调）。所以 500 行的工作流不会一次占满所有槽——它每跑完一行就把槽还回去，间隙里常规任务能插进来。

**已知代价**：单行任务本身可能要跑几十分钟（结构预测就是这个量级），这期间那个槽一直是工作流占着的，别人插不进来。这是「一个结束才下一个」的必然结果，不是缺陷；但界面上要让用户能看见「当前行在跑」，否则他会以为整个平台卡住了。

### 6.9 上下文独立

每行是独立 task、独立 `work/<id>/`、独立 agent 进程、独立令牌，`brief` 只含本行渲染结果。天然成立，但**必须有测试钉死**：「第 N 行的 brief 里不含第 N-1 行的任何值」。

## 7. 归档

四个语义字段，用户为每个 output 列选一个：

| `field` | 来源 |
| --- | --- |
| `status` | `done` / `failed` |
| `summary` | `tasks.summary`（结果说明） |
| `error` | `tasks.error`（失败原因） |
| `artifacts` | `verified=1` 的产物文件名，逗号分隔 |

### 7.1 两个去处

- **平台内**：`tasks` 表本来就有这些字段，界面直接查，不需要额外存
- **回写 xlsx**：`writeback=1` 时每归档一行写一次 `result.xlsx`（**同一个**文件，不是每行一个新文件），用户随时能下载**当前进度**

写入必须走「临时文件 + `os.replace` 原子替换」：用户在浏览器下载的同时平台在写，不原子的话会下载到半个文件。

**但 openpyxl 不支持增量写**——它每次都是「整表 load → 改几个单元格 → 整表 save」。所以「每归档一行写一次」的真实代价是 **500 行 = 500 次整表重写**，而 20MB 的表一次重写并不便宜。这一步在 `tick()` 的主线程上跑，必须：

- 归档 pass 整体在 `try/except` 里（6.2），一次慢重写不能拖死调度
- 大表考虑**批量攒着写**：累计 N 行或 T 秒才落一次盘，界面上说明「结果表每 N 行落一次盘」

**文件被 Excel 占住时（Windows 独占锁）要降级**。批量场景里「我要看着表跑」是最自然的动作，用户下载结果表、用 Excel/WPS 打开着、然后回去点继续——`openpyxl.save()` 和 `os.replace()` 都会抛 `PermissionError`。

降级语义（**用户已拍板**）：**不置 `workflow_archived`**，只把 xlsx 回写标记成待回写（`workflows.writeback_pending=1`），文件解锁后由**下一轮归档 pass 重试补写**。批次停在那一行，界面上明写「结果表被占用，待补写」，关掉 Excel 后自动继续。

理由（**与本节早先那版相反，理由也一并换掉**）：

- **静默丢结果比暂时卡住糟得多。** 置了 `workflow_archived` 就等于宣告「这一行完事了」，而结果根本不在表里：待归档行的取数带 `AND workflow_archived = 0`，置位之后没有任何代码会再碰它，`writeback_pending` 永远挂着没人清。用户开着 Excel 跑完整批，结果表里对应行是**空的**，而且**不报错**、界面显示「已完成」——这正是规格 §6.5 末与 §7.4 反复点名的最坏失效形状。
- **「卡住」在这里是正确的，而且看得见。** 闸门② 本来就是「没归档不放下一行」：被 Excel 独占锁住时停在那一行不是 bug，正是这条闸门在起作用。用户会收到一条 note 事件告诉他「结果表被占用，关掉 Excel/WPS 后会自动重试，本工作流也会在归档完成前停住」，列表卡片上同时挂着「结果表待补写」。
- **重试有界，不会变成另一种卡死。** 去重只压掉重复的 note，**压掉重试就是把上面那个「静默丢数据」原样装回来**——所以每一轮归档都真的重试（实测 59 次/分，这是被接受的代价）。
- 因此本节早先那句「否则工作流会被一个文件锁卡住，而用户看到的只是『停在第 N 行』」**不再成立**：现在界面上明写原因、且关掉 Excel 就自动继续，「停在第 N 行」是一条有解释、有出路、且不丢数据的状态。

### 7.2 归档 pass 的完整职责

归档 pass 每轮对每个**未终态**（`status IN ('running','paused')`）的工作流做四件事，**顺序不能换**：

> 取数范围含 `paused` 是硬要求：中止与暂停都把 status 写成 `paused`，而被中止那一刻**正在跑的那一行会变成终态却未归档**。只扫 `running` 的话这一行永远排在取数范围之外，用户点完中止，已算出的部分结果既不进表也下载不到。归档与「能不能放行下一行」是两件事——放行的闸门① 仍然只认 `running`。

```
1. 取出该 workflow 下 status IN ('done','failed') AND workflow_archived = 0 的行任务
2. 逐行写回 xlsx（7.1）
3. **每一行单独**在写成功后置 workflow_archived = 1
4. 写回被占用 → writeback_pending = 1，**第 3 步不做**，本行下一轮重试（7.1）
5. writeback = 0（回写开关关着）→ 第 2 步不做，**第 3 步照样做**
```

**第 3 步「每行单独置位」不是省事写法，是围栏要求**：一批 5 行里第 3 行抛异常时，前两行必须已经是 `archived=1`，否则下一轮 pass 会把它们连同第 3 行一起重来——若异常是 `PermissionError` 这类稳定失败，那两行的结果会被反复重写几十次直到用户关掉 Excel，而 `archived` 始终是 0。**改成「全批成功再统一置位」看着更整齐，实际是把一次失败放大成 N 次。**

置位的时机是**写完之后**、不是之前：先置后写，一旦写失败那一行就永远不会被重试（正是 6.5 末那个静默丢结果的形状）。

**第 5 步是最容易踩的一条**：关掉回写开关改变的是「结果去哪」，不是「这一行处理完了没有」。跳过置位的话闸门② 会永远等这一行，界面显示「运行中」但一行都不动。

### 7.3 归档前先校验行号还在

写回之前必须确认目标行号没越界（`r <= ws.max_row`）。越界说明 `source.xlsx` 被改过或者被写坏了——**记一条失败原因、置 `workflow_archived=1`、继续**，不要抛异常（那会经 6.2 的 `except` 变成静默的日志行，工作流悄悄卡住）。这类损坏要能在工作流详情页看见。

注意越界时**仍然要置位**：不能归档的行如果永远不置位，闸门② 会一直卡住整批。「置位 + 记失败原因」比「不置位 + 一直卡着」正确。

### 7.4 产物落在哪，直接决定这一行成不成（必须写进提示词）

`artifacts.discover()`（`server/artifacts.py:165`）有三条发现路径，语义**不一样**：

| 落点 | `verified` | 后果 |
| --- | --- | --- |
| `work/<task_id>/` 里的文件 | **恒为 1** | 直接可下载 |
| `outputs/t<task_id>_*`（前缀命中） | 1 | 直接可下载 |
| `outputs/` 下时间窗兜底命中 | **0** | `_package_one` **诚实地失败**，要管理员点「确认归属」再 `repack` |

所以：**工作流行任务的产物必须落在工作目录，或带 `t{task_id}_` 前缀**。落到 `outputs/` 根又没前缀，这一行会以「没有可下载的产物」失败。

按第 2 节定的「失败继续」策略，它不会停住——但会**整批一行都拿不到产物，而且只显示 failed、不报任何错**。这是批量场景特有的放大效应：常规任务失败一次你一眼看见，500 行全失败要翻到第 300 行才发现。

当前 `PLATFORM_RULES` 已经要求「所有中间文件和最终产物都写在这里（工作目录）」，所以默认行为是对的。但这条不能靠默认活着：

- `PLATFORM_RULES` 里**明写**工作流任务的产物落点（工作目录优先；确需写 `outputs/` 时必须带 `t{task_id}_` 前缀）
- 围栏钉住：构造一个产物只落在 outputs 根、无前缀的假 agent，断言该行 failed 且 `error` 里说的是产物不可下载（而不是别的含混原因）
- 工作流列表页要把「失败行数」放到最显眼处，失败时第一眼能看到

### 7.5 归档时机：必须等打包结束，不能等 agent 退出

agent 退出 ≠ 这一行结束。`_settle` 之后任务先进 `packaging`，由 `_pack_scan_once`（`app.py:668`，`PACK_SCAN_INTERVAL = 2.0` 在 `app.py:544`）打包完才到 `done`/`failed`。

**这一条比看上去更关键，因为 `packaging` 可能停在非终态**。`_pack_scan_once` 自己把失败的任务打成 `failed`（所以打包失败不卡流程），但它 `limit=50`（`app.py:672`）且每轮只扫一批——一行任务的产物特别多时，一个工作流同时不会有超过一行在 packaging（串行），所以量不是问题。真正要防的是「某行的打包器挂了、任务永远停在 packaging」：那种情况下闸门② 会一直挡着下一行，**界面必须显示「第 N 行停在打包中」**而不是笼统的「正在跑」——否则用户看到的是「平台卡住了」，而实际差的是一次 repack。

`_workflow_archive_pass()` 的归档条件因此是 **`status IN ('done','failed')` 且 `workflow_archived = 0`**，不是「进程退出了」。好处是归档时 `summary` / `error` / 产物清单都已经是最终值；而且 `packaging` 卡住不会让整批错乱——`_pack_scan_once` 失败会把任务打成 `failed`，归档照常进行。

### 7.6 回写保真度（实测，不是推测）

探针脚本在会话 workspace（不在仓库里，所以实施时不用复现）：`probe_xlsx.py` + `probe_chart.py` 拿真文件跑出来的结果：

| 项目 | 回写后 |
| --- | --- |
| 单元格数据 | 保住 |
| 公式原文（`=LEN(B2)`） | 保住 |
| **公式的缓存值** | **没有** |
| 合并单元格 | 保住 |
| 列宽 | 保住 |
| 数字格式 | 保住 |
| 图表部件（`xl/charts/chart1.xml`） | 保住，openpyxl 能读回 |

**唯一缺的是公式的缓存值**，公式本身没坏——用 Excel/WPS 打开会自己重算。所以界面上的提示文案是：

> 回写后的表里，公式要用 Excel/WPS 打开一次才会显示值。

**未验证**：Excel 实际渲染是否与原文件逐像素一致——本机没有 Excel，验不了。所以文案不写「格式完全没问题」，只说上面这一条实测支持的。

## 8. 界面

### 8.1 主看板

工作流任务**默认不出现**（`GET /api/tasks` 加 `scope=regular|workflow|all`，前端默认 `regular`）。几百个行任务糊在常规看板上不可用。

### 8.2 工作流页（独立页面，不是又一个抽屉）

- **列表**：工作流卡片 —— 名称、总行数、进度条（已跑/总数）、当前在跑第几行、失败数
- **详情**
  - **表格预览用真实表格样式**：行号列、表头粘顶、当前行高亮、状态色点
  - **列角色映射做在表头上**：点表头弹出选角色，不另开映射表单。心智是「看着表选列」，操作就该发生在表上
  - 改完**立刻**在下方显示「第 N 行渲染出来的提示词」预览
  - **控制**：开始 / 暂停 / 继续 / 中止整批 / 下载结果表
- **新建是 3 步向导**：上传 → 选列配模板（实时预览）→ 确认开跑

**独立页面怎么挂上去**：`server/app.py:526` 的静态路由是 `/{asset}` **单段 + 白名单** `USER_UI_FILES = {index.html, app.js, style.css}`。所以：

- 工作流页做成 **`web/workflow.html`**（外加 `workflow.js`），把这几个文件名加进 `USER_UI_FILES`，从首页点入口跳转
- **不能**做成 `/workflows/42` 这种多级路径——单段路由服务不出来，要额外加一条路由
- `/` 仍然只服务 `index.html`，不受影响

工作流任务有自己的一行展示，不复用常规看板卡片——批量的心智是「第 37 / 500 行」，不是一堆卡片。

### 8.3 值班台（admin）也要分开

`admin/app_admin.py:416` 的值班台列表 `limit=500` **同样按常规任务查**，几百个行任务会把管理员的故障排查视图糊满——而值班台恰恰是「现在有什么不对」的地方，混进正常跑的行任务等于把它弄瞎。所以：

- 值班台列表默认也排除工作流行任务（与 8.1 同一个 `scope` 参数，同一套过滤）
- 行任务的故障排查走工作流详情页，那里有「只看失败行」的筛选

`force_status` 的 409 守卫（6.5 守卫侧）也在这个文件里，两件事一起改。

### 8.4 新页面会整片漏出前端的静态检查（这是两个测试的硬编码造成的）

`web/workflow.html` + `web/workflow.js` 是**新文件**，而现有两个前端测试都是**把文件路径写死在模块顶部**的：

| 测试 | 写死的文件 | 新页面的后果 |
| --- | --- | --- |
| `tests/test_web_static.py:26-28` | `index.html` / `style.css` / `app.js` | `workflow.html` 与 `workflow.js` **完全不在检查范围** |
| `tests/test_frontend_url_contract.py:34-35` | `web/app.js` / `admin-ui/app.js` | `workflow.js` 里的 URL **不做契约核对** |

后果不是「新页面少几项检查」，而是**漏掉的恰好是两类已经被坑过的**：

**① 禁用词没人查了。** `test_web_static.py` 的 `BANNED_WORDS = ("AI", "助手", "大模型")`（`:32`）在 `:157-158` 对三个文件全文扫。而「界面禁词」是你反复确认过的产品口径。工作流页必然要写「计算任务」「计算进程」这类文案，**正是最容易顺手写出「AI 辅助分析」的地方**，而新文件在扫描范围外——违规了也不红。

**② URL 拼错没人查了。** `test_frontend_url_contract.py` 的 docstring 记着真机踩出来的那次事故：`apiBase()` 多拼一次 `/api`，**466 条后端测试全绿、静态检查全绿、端到端脚本也过了**，因为它们打的是 API 或三个已知文件。`workflow.js` 会有一整套新的 API 调用（上传、列角色、保存、预览、控制、下载），而这个测试存在的全部意义就是防「前端拼的路径服务端没有」——**它恰好防不住新文件**。

**所以这两处必须显式扩，而不是顺带。** 具体动作：

- `test_web_static.py` 的 `HTML/CSS/JS` 改成列表，新增 `workflow.html` / `workflow.js` 一起进 `_files()`——这样禁用词、零外链、状态色几类检查自动覆盖新页面
- `test_frontend_url_contract.py` 的 `USER_JS` 改成列表，把 `web/workflow.js` 加进去
- **`test_web_static.py:58` 那条会先打红**：`assert r in ("style.css", "app.js", "data:,")` 只认这三个，而 `index.html` 加了 `<a href="workflow.html">` 入口。这条打红是**对的**，扩白名单时要显式把 `workflow.html` 加进去，别为了让测试绿而放宽成"任意同源路径"——放宽了就等于放弃了「首页只准引这三个」这条约束
- 同理 `app.py` 的 `USER_UI_FILES`（`app.py:55`）要加 `workflow.html` 和 `workflow.js`，而 `test_web_static.py` 里应有一条断言**白名单与实际存在的文件互为充要**（防止加了白名单却忘了建文件，或建了文件忘了加白名单而线上 404）

**顺序上有个陷阱**：这两处扩展要在 `web/workflow.html` 落地**之前或同时**做。反过来做的话，中间会有一个窗口期——新文件已存在但不在扫描范围，而所有测试都是绿的，正是「看起来有测试、实际上没测」的最坏状态。

### 8.5 两处容易漏的接线

「工作流行就是带三个字段的普通任务」这个前提，会在**复用既有代码**的地方被悄悄破掉。两处：

| 位置 | 漏了会怎样 |
| --- | --- |
| `store._update()` 的字段白名单 | `workflow_id` / `workflow_row` / `workflow_archived` 写不进任务行 |
| 行任务的 `created` 事件 | 详情页时间线第一行是空的，行任务看起来像凭空出现的 |

**先纠正我自己的一个错误断言**：我原本写「`_update()` 白名单是静默失败的典型，字段不在白名单里时不抛异常、只是这一列没写进去」。**核对代码后确认这是错的**——`server/store.py:164-171` 是这样的：

```python
allowed = {"title", "brief", "status", ..., "done_at"}
bad = set(fields) - allowed
if bad:
    raise Invalid(f"不能更新这些字段：{', '.join(sorted(bad))}")
```

**它是响的**——多传一个字段就抛 `Invalid`。所以漏加白名单的后果不是「静默写不进去」，而是**建行任务这一步直接失败**。这个区别很重要：

- **响的失败**会在第一次真跑时立刻炸出来，不会流到线上
- 我原本描述的那条静默链路（写进去了但值不对 → 调度器筛不出行任务 → 永远不放行）**在 `_update()` 这条路上不成立**

但**测试写法仍然要按「静默」来写**，因为真正的危险在别处：围栏若只断言 `create_task()` 不抛异常，那它在白名单正确时绿、在白名单错误时也**因为异常而绿**（`pytest.raises` 之外的情形，或异常被上层 try 吞掉）。所以断言必须是**读回数据库核对值**：

```python
row = store.get_task(task_id)
assert row["workflow_row"] == 3 and row["workflow_archived"] == 0
```

这条断言在两种坏写法下都会红：白名单缺列（抛异常 → 红），白名单加了但 `create_task` 忘了传参（值为 None → 红）。**断言函数返回成功是最容易恒真的那种断言**，而恒真的断言在围栏里等于没有。

## 9. 待建的东西

| 位置 | 内容 |
| --- | --- |
| `server/workflow.py` | xlsx 读取、列角色、提示词渲染、归档回写（纯函数，好测） |
| `server/db.py` | 三张新表 + 幂等列补齐（见 4.3） |
| `server/store.py` | workflows / workflow_columns / workflow_skips 的 CRUD；`create_task` 接受 workflow 参数；`_update()` 白名单加三列（见 8.5） |
| `server/dispatcher.py` | `_workflow_run_pass()` 与 `_workflow_archive_pass()` 挂进 `tick()`（见 6.2）；`_queued_tasks()` 排除工作流任务 |
| `server/app.py` | 上传端点（落 staging → 校验解析 → 搬定，见 4.5）+ 工作流 CRUD/控制/预览/下载端点；`GET /api/tasks` 加 `scope`；`DELETE /api/tasks/{id}` 加 409 守卫；`USER_UI_FILES` 加工作流页文件 |
| `admin/app_admin.py` | `force_status` 对行任务的 409 守卫（6.5）；值班台列表过滤工作流行任务（8.3） |
| `web/` | 工作流页 `workflow.html` + `workflow.js`，看板加入口链接 |
| `tests/test_web_static.py` | 扫描文件清单扩成列表，纳入工作流页两个文件；`:58` 的首页引用白名单显式加 `workflow.html`（见 8.4） |
| `tests/test_frontend_url_contract.py` | `USER_JS` 扩成列表，纳入 `web/workflow.js`（见 8.4） |
| `agent/run.mjs` | `PLATFORM_RULES` 补一条工作流任务的产物落点（见 7.4） |

**后三项必须在 `web/workflow.html` 落地之前或同时完成**（8.4 末）。反过来做会出现一个窗口期：新文件已在磁盘上却不在任何检查范围里，而所有测试都是绿的——那正是「看起来有测试、实际没测」的状态。

### 9.1 端点清单

```
POST   /api/workflows/upload              multipart 上传，返回解析出的表头/行数/前 50 行
POST   /api/workflows                     建（带列映射与模板，服务端强校验）
GET    /api/workflows                     列表
GET    /api/workflows/{id}                详情（含列映射与进度）
GET    /api/workflows/{id}/preview?row=N  该行渲染出的提示词
POST   /api/workflows/{id}/start|pause|abort
POST   /api/workflows/{id}/rows/{r}/skip  跳过这一行（写 workflow_skips）
GET    /api/workflows/{id}/result.xlsx    下载回写结果（开关打开时才有）
DELETE /api/workflows/{id}                先中止在跑的行任务，再删
```

`DELETE /api/tasks/{id}` 要加守卫：命中 `workflow_id IS NOT NULL` 返回 409，指向「跳过这一行」或「删掉整个工作流」。

### 9.2 这九个端点的归属校验

上面九个端点在本文其他各节里都写了行为，**唯独没写谁能调**。多用户平台上漏掉归属校验不是「功能不对」，是**任何登录用户都能下载别人的生物序列表**，所以这条必须写死：

沿用平台既有约定，一处都不新造：

- **非管理员只看自己的**：所有读与写端点先按 `owner_id` 取，越权抛 **`NotFound`（404）而不是 403**。这是 `store.get_task()` 已经定好的行为（`store.py:121` 的 docstring 明写「抛 NotFound（不抛 403）」），照抄即可。`app.py:387` 那行注释「越权走 store.get_task → NotFound → 404，不返 403（不承认 id 存在）」就是这个决定的理由
- **管理员不受限**：`is_admin = user["role"] == "admin"`，与 `app.py:380-382` 的列表写法一致

三个具体的坑：

**① `result.xlsx` 是风险最高的一个。** 任务详情（JSON）泄露一行序列是一回事，**下载端点泄露整张表**是另一回事——它绕过了所有列表层的过滤，只要 `WHERE id = ?` 忘了带 owner 就直接落到磁盘上。它也是唯一「返回的不是 JSON」的端点，容易被写成另一套取数路径而漏掉校验。**围栏必须是跨用户的**：A 建工作流 → B 用同一端口调 `GET /api/workflows/{A的id}/result.xlsx` → 断言 404 且响应体里不含任何 xlsx 字节。

**② `scope` 参数不能顺手带上越权。** §8.1 给 `GET /api/tasks` 加 `scope=regular|workflow|all`，而现有代码是 `store.list_tasks(None if is_admin else user["id"], ...)`。**`all` 只是「行任务也算进来」，不是「所有人的都算进来」** —— 两个正交维度别混。非管理员传 `scope=all` 只能拿到自己的全部；只有 `is_admin=True` 才跨用户。

**③ 行任务的归属与 actor。** 两个字段，一处写错是显示问题，另一处写错会让闸门判错人：

- **`owner_id` 必须是 `workflows.owner_id`** —— 不是「谁点了开始」，更不是调度器自己。分错了，闸门⑥「该用户还有非终态的常规任务？」就会去查**别人的**队列，A 的工作流被 B 的任务挡住或不等 B（6.4 ⑥）
- **`actor` 不能写 `'workflow'`**。`create_task` 的默认值是 `'human'`（`store.py:77`），但没有任何用户提交过这一行，写成 `human` 详情页就会显示「用户提交了任务」而时间线上找不到那次提交——所以要显式传。**但也不能自己造一个值**：`events` 表的 CHECK 约束是 `actor IN ('human','agent','admin','system')`（`db.py:80`），写 `'workflow'` 会在插入事件时**直接违反约束抛错**，而 `create_task` 本身不写事件，所以这个错会推迟到行任务被认领那一刻才炸——离原因很远。

**取 `actor='system'`，靠事件文案承载来源**，不碰 CHECK 约束：

```
add_event(task_id, kind="created", actor="system",
          message=f"工作流「{workflow.name}」第 {row_index + 1} 行")
```

加一个 CHECK 枚举值本身不是不行，但代价不小：SQLite 不支持 `ALTER TABLE ... DROP CONSTRAINT`，给 `events` 加值意味着**重建表 + 搬数据 + 重建索引和触发器**，而 `events` 正是 `tasks` 的级联子表。为一句文案付这个迁移代价不划算——**来源信息本来就该在 message 里，不该挤进一个枚举**。

### 9.3 管理员对别人的工作流

值班台（8.3）能看到全平台的行任务，于是自然会有「管理员要不要能暂停别人的工作流」这个问题。本期取**只读**：管理员看得见、能诊断，但 `start`/`pause`/`abort`/`skip` 一律按 owner 校验，管理员操作自己的工作流。

理由是这类批量任务的成本很高（单行可能烧几十分钟 GPU），**误停别人的批次不该只靠一次点错来挡**；而值班台要的是「谁家的任务停在第几行」，这属于诊断，不属于干预。真要放开控制权，应该作为独立决策单独做，而不是顺手塞进这一版。

## 10. 测试策略

沿用本项目既有纪律：**每条修复配围栏 + 验证「撤掉修复会红」+「正确重构不被误杀」**。

重点用例：

- **迁移**：拿一个**旧 schema 的库**启动，`workflow_id` / `workflow_row` 必须真的出现（只测新库等于没测）
- **迁移并发**：三个进程同时对同一个旧库调 `init_db()`，必须全部正常退出、三列都在（4.3 末尾那条读-判断-写竞态）
- **迁移不吞错**：把 ALTER 换成会抛「磁盘满」的场景，必须**抛出来**而不是当成功（钉死 4.3 那个 `if "duplicate column" not in ... → raise`）
- **公式回退**：写一张有公式的表，渲染出的提示词里必须是公式原文而不是 `None`
- **上下文独立**：第 N 行的 brief 不含第 N-1 行的值
- **三条串行**：同一 workflow 不会有两个非终态行任务；该用户有常规任务时不放行；归档完成前不放行下一行
- **让路**：常规任务与工作流行任务同时有空槽时，**先给常规任务**。这条要测两次——一次证明顺序生效，一次证明**把 `_workflow_run_pass` 挪到 `_admit` 之前会让测试红**（顺序层会悄悄失效，所以契约层的闸门才是真防线）
- **归档不吞行**：人为造出两行同时终态且都未归档，归档后**两行都要置 `workflow_archived=1`**、且都要写进 xlsx（不许只处理一行就 `break`）
- **闸门不误杀**：用户没有常规任务时一定放行（否则就是「永远不跑」，且不报错）
- **产物落点**：造一个产物只落 outputs 根、无 `t{}_` 前缀的假 agent，断言该行 failed 且 error 说的是产物不可下载
- **归档时机**：任务停在 `packaging` 时**不置 `workflow_archived`**（闸门② 继续挡住下一行）；进 `done` 后才归档
- **回写原子性**：写一半时下载，拿到的必须是完整的旧版或新版
- **列映射校验**：模板里有未绑定的 `{{}}` 必须拒绝保存
- **上传路径穿越**：传 `filename="../../evil.xlsx"`，落盘必须在 `workflows\<id>\source.xlsx`，**不得**出现在 staging 或它外面（钉死 4.5 ①）
- **上传校验**：空文件、`.sh`、xlsx 魔数不对的，都要拒绝且**库里不留痕**（4.5 ②）
- **上传失败清理**：解析失败的 staging 文件必须被删掉，不留垃圾（4.5 ③）
- **看板不混入**：常规 `scope=regular` 的列表里绝不出现工作流任务
- **值班台不混入**：值班台列表同样筛掉工作流行任务（8.3）
- **三列真的写进去了**：建完行任务**立刻从库里读回来**断言 `workflow_row` 等于预期值（不是断言 `create_task` 返回成功——那种断言在「白名单缺列」和「白名单对了但忘传参」两种坏写法下都会绿，见 8.5）
- **新页面进静态检查**：把 `web/workflow.html` 与 `web/workflow.js` 塞进禁用词与零外链的扫描范围，往里放一个「AI」字样必须打红（钉死 8.4——不扩扫描范围的话这两条测试对新文件恒绿）
- **新页面进 URL 契约**：在 `web/workflow.js` 里故意写一个服务端不存在的路径，契约测试必须打红（钉死 8.4——`/api/api/xxx` 那次事故骗过了所有自动化手段）
- **`USER_UI_FILES` 与实际文件互为充要**：白名单里有而磁盘上没有 → 红；磁盘上有而白名单里没有 → 红（防止线上 404 而测试全绿）
- **删除守卫**：`DELETE /api/tasks/{id}` 打在工作流行任务上必须 409（钉死 6.5 洞三：不挡就静默丢结果 + 留僵尸进程）
- **重跑要重新归档**：重跑一行 → 跑完 → **打开 result.xlsx 断言那行是新值**。断言任务 `status='done'` 是不够的（6.5 末：漏清 `workflow_archived` 时状态是 done、表里是旧值）
- **归档逐行置位**：造一批 5 行里中间一行写失败，断言前两行已是 `archived=1`、失败行仍是 0（钉死 7.2——「全批统一置位」会把一次失败放大成 N 次重写）
- **归档行号越界**：把 `source.xlsx` 改短到目标行不存在，归档要记失败原因、**仍置 `archived=1`**、继续，**不抛异常**（钉死 7.3——抛异常会经 6.2 的 `except` 变成一行静默日志；不置位则闸门② 永久卡住）
- **强制改状态守卫**：`force_status` 把行任务改成 `running`/`packaging` 必须 409；改成 `queued` 要放行（钉死 6.5 洞二——这条不钉，整个工作流会从那一行起静默卡死）
- **跳过推进**：跳过一行后，下一行照常放行，那一行的结果列写「已跳过」，且该行**必须是终态**（否则闸门② 会一直等它）。**两种顺序都要钉**：跳过**先于**放行时那一行不得被建任务、不得烧一次计算（放行侧必须读 `workflow_skips`）；跳过**晚于**放行时（管理员重跑把行退回 `queued`）那一行也不许被闸门④ 重新拉起来跑
- **重跑不 stranded**：管理员把一个 failed 行任务重跑回 `queued`，下一轮工作流通道必须认领它（钉死 6.5 第①级，**撤掉 ④ 的提前会让测试红**——② 在前会当场把它挡死）
- **闸门顺序本身要被钉**：把 ④ 挪到 ② 之后（实现时最容易犯的"整理"），重跑那条用例必须红。这条不能靠"重跑不 stranded"顺带覆盖，要单独一条，因为整理顺序的人不会想到去跑重跑用例
- **暂停语义**：暂停后当前行仍会跑完；再 `start` 之后从下一行继续
- **中止整批杀当前行**：`abort_requested=1` 后在跑的行进程真的被中止，且此后不再放行新行（钉死 6.6）。**中止后工作流要自己收敛到终态**（`finished` —— CHECK 里只有这一个终态），不要求用户再点一次「继续」；被中止那一行**照样归档**（归档 pass 的取数必须含 `paused`），归档 pass 看得见它之后下载入口就得可用（判据是「有没有可下的结果表」，不是 `status === 'finished'`）
- **回写被占用要降级**：`result.xlsx` 被独占锁住时，归档结果仍进库，但 **`workflow_archived` 不置位**、`writeback_pending=1`，批次停在那一行并在界面上明写「结果表被占用，待补写」，关掉 Excel 后下一轮自动补写（钉死 7.1。**注意这条与本节早先版本相反**：置位等于宣告「这一行完事了」而结果不在表里，待归档行的取数带 `AND workflow_archived = 0`，置位之后那一行永远不会被重试——静默丢数据比暂时卡住糟得多）
- **回写开关三处接线**：`writeback` 被接口接受、被视图返回、且归档路径真的按它决定要不要写 xlsx；关掉时 `workflow_archived` 仍然全部置 1（否则闸门② 整批死锁）但**不生成 result.xlsx**、下载端点 404（钉死 2 决策表 / 7.1 第 5 步 / 9.1）
- **结果表累积**：连续归档多行之后打开 `result.xlsx`，**每一行都有值**（钉死 7.1「当前进度」——基底必须是已有的 `dest`，不是 `source`。断言必须逐行核对：只断最后一行会被缺陷版本蒙混过去）
- **`context` 列恒定性**：一个整列不恒定的 `context` 列必须**拒绝保存**，不能悄悄取第一行（5.1）
- **全空行不建任务**：尾部残留的全空行不进 `total_rows`，也不会产出行任务（5.1）
- **跨用户下载要 404**：A 建工作流 → B 调 `GET /api/workflows/{A的id}/result.xlsx` → 404，**且响应体不含任何 xlsx 字节**（钉死 9.2 ①——文件端点最容易走另一套取数路径而漏掉归属校验）
- **跨用户控制要 404**：B 对 A 的工作流调 `start`/`pause`/`abort`/`skip`/`DELETE` 全部 404，且**库里没有产生任何副作用**（不只看状态码）
- **`scope=all` 不是越权**：非管理员传 `scope=all` 只能拿到自己的行任务；admin 才能跨用户（钉死 9.2 ②）
- **行任务 owner/actor 正确**：工作流创建的行任务，`owner_id == workflows.owner_id`（不是点开始的人）；`created` 事件的 `actor == 'system'` 且 message 含工作流名与行号（钉死 9.2 ③——`actor` 写 `'workflow'` 会撞上 `events` 表的 CHECK 约束（`db.py:80`），且 `create_task` 不写事件，所以这个错要推迟到行任务被认领时才炸）
- **闸门⑥ 判的是 owner 的常规任务**：owner 有一个排队中的常规任务时工作流不放行；**另一个**用户有排队任务时**照样放行**（钉死 9.2 ③——`owner_id` 分错会拿别人的队列当自己的）
- **管理员对别人的工作流只读**：admin 看得到别人的工作流，但 `pause` 它的行返回 404（钉死 9.3）

## 11. 明确不做（YAGNI）

- 多个工作表联动（只支持单 sheet）
- 工作流之间的依赖/DAG
- 断点续跑的中间态快照（游标本身就是断点）
- 结果的多级分类目录（只有四个语义字段）
- 工作流模板的复用/版本化（提示词直接存在工作流记录里）
- 管理员控制别人的工作流（值班台只读，理由见 9.3）
- 工作流运行时的并发行数 > 1（串行是结构性保证，放开并发等于重写整个闸门）
