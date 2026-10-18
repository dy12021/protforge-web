"""业务数据访问层。所有对 SQLite 的业务查询都在这里，db.py 只管连接与 schema。

三条贯穿全文件的规则：

1. **越权一律抛 NotFound，不抛 403。** 403 等于告诉外部"这个 task id 存在但不是
   你的"，403 与 404 的差别本身就是信息泄露。内部 API 的 detail 恒为中文。

2. **审计物理隔离在 audit_log 表，绝不进 events。** 一次 rfdiffusion 跑几小时会
   产生上万次 bash 调用，混进 events 会把用户时间线刷成日志瀑布，还会让 SSE 的
   游标扫描（WHERE id > ?）越来越慢。audit 子模块就是这条边界。

3. **step 是"当前正在做什么"的单行摘要，不是日志。** 所以 set_step 只写 tasks.step
   一列，不写事件；前端轮询 task.step 即可，不进时间线。
"""
import os
import sqlite3

import db


class StoreError(Exception):
    """store 层异常基类。message 直接作为 API 的 detail，所以恒为中文。"""


class NotFound(StoreError):
    """任务不存在，**或**存在但当前用户无权查看（两者刻意不区分）。"""


class Invalid(StoreError):
    """入参不合法。"""


# step 单行摘要上限。按 Unicode 码点算，不是字节——见 truncate_step。
STEP_MAX = 80
BRIEF_MAX = 20000
TITLE_MAX = 500

# set_status 的 CAS 重试上限。正常情况下第一次就命中，重试只在"读 prev 与写库之间
# 真被别人改了"时发生（最多三个进程：主服务 / 管理员面板 / agent 编排）。给到 8 是
# 为了极罕见的连续抢输也不至于把调用方逼到抛错——抛错在这里比吞掉一次改写诚实。
_STATUS_CAS_TRIES = 8

# 终态只能被**显式的人/管理员动作**带走。自动回写（system / agent 的进度、收口、
# 打包收口）一律不许把 done/failed 推回非终态：用户点了中止，调度器再回写一次
# "正在运行"，等于把中止撤销了。重跑 / 重新打包走 admin，用户中止走 human，都在
# 这条线之外，不受影响。
_EXPLICIT_ACTORS = ("human", "admin")


def truncate_step(text: str) -> str:
    """按 Unicode 码点截断。不能用 [:80] 之外的字节操作——那会切坏 UTF-8。

    Python str 是码点序列，`t[:79] + "…"` 天然不会把一个多字节字符劈成两半。
    反例（禁止）：`t.encode("utf-8")[:80].decode("utf-8")`——尾部半个汉字会抛
    UnicodeDecodeError，或被 errors="ignore" 悄悄吃掉。
    """
    t = (text or "").strip()
    if len(t) <= STEP_MAX:
        return t
    return t[: STEP_MAX - 1] + "…"


def _row_to_dict(row):
    return dict(row) if row is not None else None


def _uploads_dir(task_id: int) -> str:
    return str(db.data_root() / "uploads" / str(task_id))


def _work_dir(task_id: int) -> str:
    return str(db.data_root() / "work" / str(task_id))


# ---------------------------------------------------------------- tasks

def create_task(title: str, brief: str, owner_id: int, actor: str = "human",
                *, workflow_id=None, workflow_row=None) -> dict:
    """建任务，初始状态恒为 queued。

    **不自动写 'created' 事件**——事件流由 API 层负责（用户点了提交才有一条，
    agent 接手才有一条）。测试 test_audit_log_separate_from_events 钉死了这条：
    调一次 add_event 后 list_events 必须是 1。

    brief 原样保存不截断：agent 据此决定调哪些技能、填什么参数，截断了它就自己编。

    workflow_id / workflow_row 是**只在尾部加的关键字参数**，默认 None：既有调用点
    一个字都不用改，落库也就是 NULL——「NULL = 不属于任何工作流」是常规任务的
    契约，闸门⑥ 的 `workflow_id IS NULL` 就靠它。**不要**给它们设非 None 的默认值。
    """
    title = (title or "").strip()
    brief = brief or ""
    if not title:
        raise Invalid("标题不能为空")
    if len(title) > TITLE_MAX:
        raise Invalid(f"标题过长，最多 {TITLE_MAX} 个字符")
    if len(brief) > BRIEF_MAX:
        raise Invalid(f"需求描述过长，最多 {BRIEF_MAX} 个字符")
    if owner_id is None:
        raise Invalid("缺少任务归属用户")

    now = db.now_iso()
    c = db.connect()
    try:
        with c:
            cur = c.execute(
                """INSERT INTO tasks
                   (title, brief, status, step, owner_id, priority,
                    uploads_dir, output_dir, created_at, updated_at,
                    workflow_id, workflow_row)
                   VALUES (?, ?, 'queued', '', ?, 0, '', '', ?, ?, ?, ?)""",
                (title, brief, int(owner_id), now, now, workflow_id, workflow_row),
            )
            tid = cur.lastrowid
            # 目录只写路径不 mkdir：真建目录是上传/打包那一刻的事，
            # 建任务时建空目录会在用户还没传文件时就留下一堆垃圾目录。
            c.execute(
                "UPDATE tasks SET uploads_dir = ?, output_dir = ? WHERE id = ?",
                (_uploads_dir(tid), _work_dir(tid), tid),
            )
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    finally:
        c.close()
    return dict(row)


def get_task(task_id: int, owner_id=None, is_admin: bool = False) -> dict:
    """取单个任务。owner_id 与任务归属不符时抛 NotFound（不抛 403）。"""
    c = db.connect()
    try:
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        c.close()
    if row is None:
        raise NotFound("任务不存在")
    if not is_admin and owner_id is not None and int(row["owner_id"]) != int(owner_id):
        raise NotFound("任务不存在")
    return dict(row)


def list_tasks(owner_id=None, status=None, *, is_admin=False,
               limit=200, offset=0) -> list:
    """列任务，最新在前。owner_id 为 None 且 is_admin=True 时列全部。"""
    sql = "SELECT * FROM tasks"
    where, args = [], []
    if not is_admin and owner_id is not None:
        where.append("owner_id = ?")
        args.append(int(owner_id))
    if status:
        if status not in db.VALID_STATUSES:
            raise Invalid(f"未知状态：{status}")
        where.append("status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    c = db.connect()
    try:
        rows = c.execute(sql, args).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def _update(task_id: int, **fields) -> dict:
    """内部更新。故意不碰 updated_at——交给 trg_tasks_touch，格式才不会分叉。"""
    if not fields:
        return get_task(task_id)
    allowed = {
        "title", "brief", "status", "step", "priority", "uploads_dir",
        "output_dir", "package_path", "summary", "error", "session_id",
        "done_at",
        # 工作流三列。放行 pass 与归档 pass 全靠它们，改白名单就是给闸门断供。
        "workflow_id", "workflow_row", "workflow_archived",
    }
    bad = set(fields) - allowed
    if bad:
        raise Invalid(f"不能更新这些字段：{', '.join(sorted(bad))}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    c = db.connect()
    try:
        with c:
            cur = c.execute(f"UPDATE tasks SET {cols} WHERE id = ?",
                            list(fields.values()) + [int(task_id)])
            if cur.rowcount == 0:
                raise NotFound("任务不存在")
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        c.close()
    return dict(row)


def update_task(task_id: int, **fields) -> dict:
    """白名单字段更新。给调度器/打包器收口字段用（package_path/summary/error…）。"""
    return _update(task_id, **fields)


def set_step(task_id: int, text: str) -> str:
    """写当前步骤摘要，返回**截断后**的文本（调用方直接拿去广播，别自己再截一次）。"""
    step = truncate_step(text)
    _update(task_id, step=step)
    return step


def set_status(task_id: int, status: str, actor: str = "system",
               message: str = "", note: bool = False) -> dict:
    """改状态并留一条 status 事件。进终态时顺手写 done_at。

    校验用 db.VALID_STATUSES（主链 4 态 + failed），不能用 STATUS_KEYS，
    后者不含 failed，会把"标记失败"判成非法。

    **读 prev、判断、写必须合成一个原子动作。** 旧实现是「连库读 → 内存判断 → 再连库
    写」，中间隔着 add_event 与 _update 两次独立开库，中间是有窗口的：

    - 用户中止（public app 在另一个进程里把状态写成 failed）正好落在这个窗口时，
      set_status(..., "running") 会把中止**静默覆盖**——任务等于没被中止，白烧一轮
      GPU，还交给用户一个"正在运行"；
    - 同源的第二处：prev 是旧值时 ``status in TERMINAL and prev not in TERMINAL``
      判错，done_at 会被多写一次，"进终态的时刻"就此不可信。

    写法照 :func:`claim_queued` 的路子：**CAS**——UPDATE 带 ``WHERE status = 刚读到
    的 prev``，单条语句原子，谁先到谁赢；rowcount=0 说明这个窗口确实被别人占了，
    就重读 prev 重新判断。**不用 BEGIN IMMEDIATE**：Python 的 sqlite3 只在第一条写
    语句前隐式开事务，裸 BEGIN 会和它打架；而且它把读也关进写锁，随后 add_event
    那条新连接会撞上 SQLite 的单写者锁自锁（claim_queued 的 docstring 记着这条）。

    **自动回写不许把终态推回非终态。** 读到 prev 已是终态（done/failed）、请求的状态
    不是终态、而发起方又不是 human/admin 时，放弃这次改写、返回当前行，并补一条 log
    说明为什么没改成——沉默地不生效会让人以为中止没成功。管理员改状态、重跑推回队列、
    重新打包都是 human/admin 的显式动作，一次干净调用，不受这条限制，语义与旧实现
    完全一致。调度器收口（dispatcher._settle 推 packaging）与打包器收口（推 done）走的
    正是 system/agent，正好被这条挡住：中止之后进程再退出也不会把任务翻回"进行中"。

    事件与状态写同处一个事务：分两次开库时，两个并发调用可能一个先提交状态、另一个
    先提交事件，时间线上的 from/to 链会自己错位（先记 packaging→done、后记
    queued→running），审计链就不可信了。
    """
    if status not in db.VALID_STATUSES:
        raise Invalid(f"未知状态：{status}")
    tid = int(task_id)
    prev = ""
    won = False
    refused = False
    for _attempt in range(_STATUS_CAS_TRIES):
        c = db.connect()
        try:
            row = c.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None:
                raise NotFound("任务不存在")
            prev = str(row["status"])
            if (prev in db.TERMINAL_STATUSES
                    and status not in db.TERMINAL_STATUSES
                    and actor not in _EXPLICIT_ACTORS):
                refused = True          # 别人已经收口/中止了，别盖回去
                break
            # done_at 与状态同一条 UPDATE：条件成立才写，prev 是真读到的值，
            # 所以"第一次进终态"只会被认一次。
            if status in db.TERMINAL_STATUSES and prev not in db.TERMINAL_STATUSES:
                cur = c.execute(
                    "UPDATE tasks SET status = ?, done_at = ? WHERE id = ? AND status = ?",
                    (status, db.now_iso(), tid, prev))
            else:
                cur = c.execute(
                    "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
                    (status, tid, prev))
            won = cur.rowcount == 1
            if won:
                if note:
                    _insert_event(c, tid, "note", actor, message or status)
                else:
                    _insert_event(c, tid, "status", actor, message,
                                  from_status=prev, to_status=status)
            c.commit()
        finally:
            c.close()
        if won:
            break
    else:
        raise StoreError("任务状态正在被其他进程修改，请稍后重试")

    if refused:
        add_event(tid, "log", actor,
                  f"状态已被改成{db.STATUS_LABEL.get(prev, prev)}，"
                  f"没有再改成{db.STATUS_LABEL.get(status, status)}")
    return get_task(tid, is_admin=True)


def claim_queued(task_id: int) -> str:
    """把排队中的任务**原子地**抢成 running，返回抢到的当前状态。

    为什么调度器不能用 :func:`set_status`：它是无条件覆盖。调度器读完队列、还没
    起进程的那一瞬，用户可以点中止（public app 在另一个进程里把状态写成
    ``failed``）。这时再 ``set_status(..., "running")`` 就把中止覆盖掉了——任务
    等于没被中止，白烧一轮 GPU，还交给用户一个"正在运行"。

    ``UPDATE ... WHERE status='queued'`` 在 SQLite 里是单条语句的原子操作，谁先到
    谁赢。返回 ``"queued"`` 表示抢到了（调用方接着起进程）；返回别的状态表示这一轮
    轮到自己之前它已经不在队列里了。

    事件单独补写：``add_event`` 自己开连接，在本连接还持着写事务时调会撞上
    SQLite 的单写者锁，所以先 commit 再写。
    """
    task_id = int(task_id)
    c = db.connect()
    try:
        cur = c.execute(
            "UPDATE tasks SET status = 'running' WHERE id = ? AND status = 'queued'",
            (task_id,),
        )
        won = cur.rowcount == 1
        c.commit()
    finally:
        c.close()
    if won:
        add_event(task_id, "status", "system", "计算进程已启动",
                  from_status="queued", to_status="running")
        return "queued"
    row = get_task(task_id, is_admin=True)      # 抢不到就得说清它去哪了
    return str(row.get("status") or "")


# ---------------------------------------------------------------- events

def _insert_event(c, task_id: int, kind: str, actor: str, message: str = "",
                  from_status=None, to_status=None) -> int:
    """在**调用方给定的连接/事务**里插一条事件，返回事件 id。

    抽出来是因为 set_status 要把「改状态」和「记事件」放进同一个事务：它们是同一
    件事的两半，拆成两次开库写就会在并发下错位（先提交状态、后提交事件的那一方，
    时间线上的 from/to 会指错来处）。CHECK 兜底非法 kind/actor 的行为与 add_event
    一致——只有一条 INSERT 实现，复制两份必然会漂移。
    """
    try:
        cur = c.execute(
            """INSERT INTO events
               (task_id, kind, from_status, to_status, actor, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(task_id), kind, from_status, to_status, actor,
             message or "", db.now_iso()),
        )
    except sqlite3.IntegrityError as e:
        # CHECK 与外键都是 IntegrityError。混成一句话的话，"任务不存在"会被报成
        # "事件类型或来源不合法"——排查的人会去翻 kind/actor 白名单，而真因在
        # 任务那一侧（后台线程跨过了测试的库边界、或任务已被删）。
        if "FOREIGN KEY" in str(e):
            raise NotFound("任务不存在")
        raise Invalid("事件类型或来源不合法")
    if cur.rowcount == 0:
        raise NotFound("任务不存在")
    return cur.lastrowid


def add_event(task_id: int, kind: str, actor: str, message: str = "",
              from_status=None, to_status=None) -> int:
    """写一条时间线事件，返回事件 id（前端拿它做 SSE 游标）。

    kind / actor 的合法性交给 schema 的 CHECK 兜底，不在这里复制一份白名单——
    复制就会漂移，漂移之后是静默丢事件而不是报错。
    """
    c = db.connect()
    try:
        with c:
            return _insert_event(c, task_id, kind, actor, message,
                                 from_status=from_status, to_status=to_status)
    finally:
        c.close()


def add_step_event(task_id: int, actor: str, message: str):
    """写一条 step 事件；与**上一条 step 完全同文**时跳过，返回 ``None``。

    agent 侧的心跳每 60 秒会把最后一次 ``report_step`` 的原话重发一次——长命令跑着
    的时候模型没有机会再调 ``report_step``。没有这条去重，一条二十分钟的命令会在
    时间线上刷出二十行一模一样的话。时间线要回答的是"发生了什么变化"，
    "还活着吗"是 ``tasks.step`` 与 ``updated_at`` 的事。

    只跟**上一条**比，不是跟全表比：文本来回切换时（做A → 做B → 做A），
    那两条相隔很远的同文事件都是真实的变化，吞掉就丢了过程。

    返回 ``None`` 表示被去重了，调用方**照样**要写 ``tasks.step`` 与唤醒 SSE——
    去重的是时间线噪音，不是"还活着"这个信号。
    """
    c = db.connect()
    try:
        last = c.execute(
            "SELECT message FROM events WHERE task_id = ? AND kind = 'step' "
            "ORDER BY id DESC LIMIT 1", (int(task_id),)).fetchone()
        if last is not None and last["message"] == message:
            return None
        with c:
            return _insert_event(c, task_id, "step", actor, message)
    finally:
        c.close()


def list_events(task_id: int, after_id: int = 0, limit: int = 500) -> list:
    """按 id 升序列时间线事件。after_id 是 SSE 游标：只取比它新的。"""
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (int(task_id), int(after_id), int(limit)),
        ).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def latest_event_id(task_id: int) -> int:
    """当前最大事件 id。SSE 首帧拿它当游标，浏览器重连时不会重复推送历史。"""
    c = db.connect()
    try:
        row = c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events WHERE task_id = ?",
                        (int(task_id),)).fetchone()
    finally:
        c.close()
    return int(row["m"])


def record_finished(task_id: int, actor: str = "system") -> None:
    """标记计算进程已收口（无论成功失败）。幂等：重复调用不会插第二条。

    用 events.kind='finished' 而不是 audit_log：它每任务至多一条，留在时间线上
    是有用的（用户能看到"进程已结束"），且 events 的 CHECK 里本就列了 finished。
    """
    c = db.connect()
    try:
        with c:
            c.execute(
                """INSERT INTO events (task_id, kind, actor, message, created_at)
                   SELECT ?, 'finished', ?, '', ?
                   WHERE NOT EXISTS (
                     SELECT 1 FROM events WHERE task_id = ? AND kind = 'finished'
                   )""",
                (int(task_id), actor, db.now_iso(), int(task_id)),
            )
    finally:
        c.close()


def clear_finished(task_id: int) -> None:
    """清掉本任务的收口标记。**只在"重跑"时调**。

    ``finished`` 的语义是"**本轮**计算进程已收口"，不是"历史上曾经成功过"。
    管理员点重跑就是开新一轮，标记必须跟着清——不清的话第二轮的 finish 回调
    会被 422 拒掉，而调度器 _settle 读到的是上一轮的标记，会把第二轮任何退出
    （包括直接崩溃）都判成"正常收口"，最后打出���一份与本次运行无关的旧结果包。

    只删这一种 kind，其他事件一个不动：时间线是给人看的历史，不该被重跑抹掉。
    """
    c = db.connect()
    try:
        with c:
            c.execute("DELETE FROM events WHERE task_id = ? AND kind = 'finished'",
                      (int(task_id),))
    finally:
        c.close()


def has_finished(task_id: int) -> bool:
    """计算进程是否已收口。调度器用它区分"重连"与"真的刚跑完"。"""
    c = db.connect()
    try:
        row = c.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND kind = 'finished' LIMIT 1",
            (int(task_id),)).fetchone()
    finally:
        c.close()
    return row is not None


# ---------------------------------------------------------------- 子模块

class _Artifacts:
    """产物清单。下载列表直接按它渲染，不要另扫 output_dir。"""

    NAMES = ("path", "name", "kind", "size", "source", "verified", "created_at")

    def add(self, task_id: int, path: str, name: str, kind: str = "file",
            size: int = 0, source: str = "prefix") -> int:
        """登记一个产物。同一路径重复登记会**更新**而不是插第二条。

        agent 重跑 / 断点续跑会重新扫一遍 output_dir，不去重的话下载列表会翻倍。
        去重靠本函数自己查——schema 的 artifacts 表**没有** (task_id, path) 唯一
        约束，写 ON CONFLICT 会被 sqlite 直接拒（no matching PRIMARY KEY or
        UNIQUE constraint），SCHEMA 是计划钦定的不能改，所以就在这里查。
        查与写同处一个事务，sqlite 写事务串行化，不会插出两条。
        """
        name = (name or "").strip()
        if not name:
            raise Invalid("产物名不能为空")
        if "/" in name or "\\" in name or ".." in name:
            raise Invalid("产物名只能是文件名，不能含路径分隔符")
        now = db.now_iso()
        c = db.connect()
        try:
            with c:
                row = c.execute(
                    "SELECT id FROM artifacts WHERE task_id = ? AND path = ?",
                    (int(task_id), path)).fetchone()
                if row is None:
                    if c.execute("SELECT 1 FROM tasks WHERE id = ?",
                                 (int(task_id),)).fetchone() is None:
                        raise NotFound("任务不存在")
                    cur = c.execute(
                        """INSERT INTO artifacts
                           (task_id, path, name, kind, size, source, verified, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                        (int(task_id), path, name, kind, int(size), source, now),
                    )
                    aid = cur.lastrowid
                else:
                    c.execute(
                        """UPDATE artifacts
                           SET name = ?, kind = ?, size = ?, source = ?, verified = 1
                           WHERE id = ?""",
                        (name, kind, int(size), source, row["id"]),
                    )
                    aid = row["id"]
        finally:
            c.close()
        return int(aid)

    def list(self, task_id: int) -> list:
        c = db.connect()
        try:
            rows = c.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY id ASC",
                (int(task_id),)).fetchall()
        finally:
            c.close()
        return [dict(r) for r in rows]

    def get(self, task_id: int, artifact_id: int) -> dict:
        c = db.connect()
        try:
            row = c.execute(
                "SELECT * FROM artifacts WHERE id = ? AND task_id = ?",
                (int(artifact_id), int(task_id))).fetchone()
        finally:
            c.close()
        if row is None:
            raise NotFound("产物不存在")
        return dict(row)


class _Audit:
    """审计流。**只进 audit_log 表，绝不写 events。** 见模块 docstring 第 2 条。"""

    def log(self, task_id, kind: str, command: str = "", exit_code=None,
            elapsed_ms=None, actor: str = "agent") -> int:
        """记一条审计。task_id 可为 None（任务外的操作，如登录、配置改动）。"""
        c = db.connect()
        try:
            with c:
                cur = c.execute(
                    """INSERT INTO audit_log
                       (task_id, kind, command, exit_code, elapsed_ms, actor, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (int(task_id) if task_id is not None else None, kind,
                     command or "", exit_code, elapsed_ms, actor, db.now_iso()),
                )
            return cur.lastrowid
        finally:
            c.close()

    def list(self, task_id=None, after_id: int = 0, limit: int = 200) -> list:
        if task_id is None:
            c = db.connect()
            try:
                rows = c.execute(
                    "SELECT * FROM audit_log WHERE id > ? ORDER BY id DESC LIMIT ?",
                    (int(after_id), int(limit))).fetchall()
            finally:
                c.close()
        else:
            c = db.connect()
            try:
                rows = c.execute(
                    """SELECT * FROM audit_log WHERE task_id = ? AND id > ?
                       ORDER BY id DESC LIMIT ?""",
                    (int(task_id), int(after_id), int(limit))).fetchall()
            finally:
                c.close()
        return [dict(r) for r in rows]


artifacts = _Artifacts()
audit = _Audit()


# --- 工作流 -----------------------------------------------------------------
# 业务 SQL 全部集中在本模块（见模块 docstring 第 1 条），工作流这一段也遵守：
# 别在旁边另起一个 workflow.py，那会让「改了一份另一份还在跑」从十几次变成更多次。

_WORKFLOW_FIELDS = {
    "name", "sheet", "source_path", "prompt_template", "header_row", "key_col",
    "total_rows", "cursor", "writeback", "writeback_pending",
    "abort_requested", "result_path", "status",
}
_ROLES = ("key", "input", "context", "output", "ignore")


def create_workflow(owner_id: int, name: str, prompt_template: str, *,
                    sheet: str = "", source_path: str = "", header_row: int = 1,
                    key_col: int = 0, total_rows: int = 0,
                    writeback: int = 0) -> dict:
    """建工作流。**不设 status**——它恒为表默认值 'draft'。

    cursor 恒从 0 起：它表示「下一个尚未放行的数据行序号」，建工作流时还没有任何
    行被放行。abort_requested / writeback_pending 同理，由默认值兜住。
    """
    name = (name or "").strip()
    if not name:
        raise Invalid("工作流名称不能为空")
    if not (prompt_template or "").strip():
        raise Invalid("提示词模板不能为空")
    if owner_id is None:
        raise Invalid("工作流缺少归属用户")
    now = db.now_iso()
    c = db.connect()
    try:
        with c:
            cur = c.execute(
                """INSERT INTO workflows
                   (owner_id, name, sheet, source_path, prompt_template,
                    header_row, key_col, total_rows, cursor, writeback,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (int(owner_id), name, sheet, source_path, prompt_template,
                 int(header_row), int(key_col), int(total_rows),
                 1 if writeback else 0, now, now))
            wid = cur.lastrowid
        row = c.execute("SELECT * FROM workflows WHERE id = ?", (wid,)).fetchone()
    finally:
        c.close()
    return dict(row)


def list_workflows(owner_id=None, *, is_admin: bool = False) -> list[dict]:
    sql = "SELECT * FROM workflows"
    where, args = [], []
    if not is_admin and owner_id is not None:
        where.append("owner_id = ?")
        args.append(int(owner_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    c = db.connect()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def get_workflow(workflow_id: int, owner_id=None, is_admin: bool = False) -> dict:
    """越权抛 NotFound（不抛 403）——与 get_task 同约定，不承认 id 存在。"""
    c = db.connect()
    try:
        row = c.execute("SELECT * FROM workflows WHERE id = ?",
                        (int(workflow_id),)).fetchone()
    finally:
        c.close()
    if row is None:
        raise NotFound("工作流不存在")
    if not is_admin and owner_id is not None and int(row["owner_id"]) != int(owner_id):
        raise NotFound("工作流不存在")
    return dict(row)


def update_workflow(workflow_id: int, **fields) -> dict:
    """白名单更新。**不做归属校验**——调用方（HTTP 层）必须先 get_workflow 验过
    owner，否则这里等于把别人的工作流开着改。"""
    bad = set(fields) - _WORKFLOW_FIELDS
    if bad:
        raise Invalid(f"不能更新这些字段：{', '.join(sorted(bad))}")
    if not fields:
        return get_workflow(workflow_id)
    if "status" in fields and fields["status"] not in ("draft", "running", "paused", "finished"):
        raise Invalid(f"未知的工作流状态：{fields['status']}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    c = db.connect()
    try:
        with c:
            cur = c.execute(f"UPDATE workflows SET {cols} WHERE id = ?",
                            list(fields.values()) + [int(workflow_id)])
            if cur.rowcount == 0:
                raise NotFound("工作流不存在")
        row = c.execute("SELECT * FROM workflows WHERE id = ?",
                        (int(workflow_id),)).fetchone()
    finally:
        c.close()
    return dict(row)


def list_workflow_columns(workflow_id: int) -> list[dict]:
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT * FROM workflow_columns WHERE workflow_id = ? ORDER BY col_index",
            (int(workflow_id),)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def set_workflow_columns(workflow_id: int, cols: list[dict]) -> list[dict]:
    """整体替换一列组——**删掉本次没传的那些列**，靠 UNIQUE(workflow_id, col_index)
    保证幂等。只 DELETE 本次传到的 col_index 是错的：用户解绑一列再保存，旧列行会
    残留下来继续参与渲染。

    col_index 缺键/不是整数都抛 :class:`Invalid`（→ 422），不能让它冒成 KeyError/
    ValueError——那会让 Task 6 返 500，而不是「这一列填错了」这种能看懂的话。
    """
    seen = set()
    for col in cols:
        if col.get("role", "ignore") not in _ROLES:
            raise Invalid(f"未知的列角色：{col.get('role')}")
        if "col_index" not in col:
            raise Invalid("每一列都必须带列号（col_index）")
        try:
            idx = int(col["col_index"])
        except (TypeError, ValueError):
            raise Invalid(f"列号必须是整数：{col['col_index']!r}") from None
        if idx in seen:
            raise Invalid(f"第 {idx} 列出现了两次")
        seen.add(idx)
    c = db.connect()
    try:
        with c:
            c.execute("DELETE FROM workflow_columns WHERE workflow_id = ?",
                      (int(workflow_id),))
            for col in cols:
                c.execute(
                    """INSERT INTO workflow_columns
                       (workflow_id, col_index, header, role, slot, field)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (int(workflow_id), int(col["col_index"]),
                     col.get("header", ""), col.get("role", "ignore"),
                     col.get("slot", ""), col.get("field", "")))
    finally:
        c.close()
    return list_workflow_columns(workflow_id)


def delete_workflow(workflow_id: int, owner_id: int | None = None,
                    is_admin: bool = False) -> None:
    """删工作流。tasks.workflow_id 的 ON DELETE CASCADE 会带走行任务记录；
    磁盘由 janitor 负责（规格 6.7 要求先中止在跑的行任务，那是 app 层的事）。

    **归属校验放在 store 层，不只放 HTTP 层**：这是唯一破坏性的函数，而删除端点
    漏过它一次。漏了的后果不是「删掉别人的东西」，是**杀掉别人正在跑的 GPU 计算
    并伪造其时间线**——所以闸门必须贴着函数本身，让「HTTP 层忘了先 get_workflow」
    这种疏漏无法再犯。越权抛 NotFound（不抛 403，与 get_workflow/get_task 同约定：
    承认「这东西存在」本身就是信息泄露）。

    **缺 owner_id 默认拒绝**（owner_id=None 且非管理员 → NotFound）：这一层存在的
    全部意义就是拦住「新调用方忘传参数」那种疏漏，所以默认必须是拒绝。注意这里
    不能只靠 get_workflow —— 它在 owner_id 为 None 时是**放行**的（语义是
    「没声明是谁就当内部调用」），直接拿它当闸门的话，忘传参数恰好会删掉别人的
    工作流，正好是这一层要防的那件事。
    """
    if owner_id is None and not is_admin:
        raise NotFound("工作流不存在")
    get_workflow(workflow_id, owner_id=owner_id, is_admin=is_admin)
    c = db.connect()
    try:
        with c:
            cur = c.execute("DELETE FROM workflows WHERE id = ?",
                            (int(workflow_id),))
            if cur.rowcount == 0:
                raise NotFound("工作流不存在")
    finally:
        c.close()


def skip_workflow_row(workflow_id: int, row_index: int, reason: str = "") -> None:
    c = db.connect()
    try:
        with c:
            c.execute(
                """INSERT OR IGNORE INTO workflow_skips
                   (workflow_id, row_index, reason, created_at)
                   VALUES (?, ?, ?, ?)""",
                (int(workflow_id), int(row_index), reason, db.now_iso()))
    finally:
        c.close()


def list_workflow_skips(workflow_id: int) -> set[int]:
    c = db.connect()
    try:
        return {int(r[0]) for r in c.execute(
            "SELECT row_index FROM workflow_skips WHERE workflow_id = ?",
            (int(workflow_id),)).fetchall()}
    finally:
        c.close()


# --- 调度器闸门要问的问题（规格 6.3 / 6.4）---------------------------------

_LIVE = ("queued", "running", "packaging")
# 终态**只用** db.TERMINAL_STATUSES，本文件不抄第二份——理由见下面那个函数的 docstring。


def list_running_workflows() -> list[dict]:
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT * FROM workflows WHERE status = 'running' ORDER BY id").fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def workflow_has_unarchived(workflow_id: int) -> bool:
    """闸门②。判据是**行自己的标记**，与 cursor 无关。

    写成 `workflow_row < cursor` 会把所有历史行都算成没归档（含早已归档过的），
    于是②恒真、整批永不放行——而且只在管理员用过重跑之后才发作。
    """
    c = db.connect()
    try:
        return c.execute(
            "SELECT 1 FROM tasks WHERE workflow_id = ? AND workflow_archived = 0"
            " LIMIT 1", (int(workflow_id),)).fetchone() is not None
    finally:
        c.close()


def workflow_has_live_rows(workflow_id: int) -> bool:
    """闸门⑤：在跑 / 排队 / 打包中的行任务。"""
    marks = ",".join("?" * len(_LIVE))
    c = db.connect()
    try:
        return c.execute(
            f"SELECT 1 FROM tasks WHERE workflow_id = ? AND status IN ({marks})"
            " LIMIT 1", (int(workflow_id), *_LIVE)).fetchone() is not None
    finally:
        c.close()


def workflow_queued_rows(workflow_id: int) -> list[dict]:
    """闸门④：要认领的 queued 行任务。管理员重跑产生的就在这里。"""
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT * FROM tasks WHERE workflow_id = ? AND status = 'queued'"
            " ORDER BY workflow_row", (int(workflow_id),)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def workflow_unarchived_terminal_rows(workflow_id: int) -> list[dict]:
    """归档 pass 的取数：终态且还没写回表的行。

    终态集合**直接用** db.TERMINAL_STATUSES，本文件不另抄一份。抄一份的后果不是
    今天的 bug，而是将来某次给终态集合加了值、忘了改这里：归档 pass 扫不到那些行 →
    闸门② 的「有未归档的行」恒真 → 整批锁死，且不报任何错（规格 6.3 那一族）。
    同一文件另有 6 处直接用 db.TERMINAL_STATUSES，store.py:210 记着为什么必须用它。
    """
    marks = ",".join("?" * len(db.TERMINAL_STATUSES))
    c = db.connect()
    try:
        rows = c.execute(
            f"SELECT * FROM tasks WHERE workflow_id = ?"
            f" AND status IN ({marks}) AND workflow_archived = 0"
            " ORDER BY workflow_row",
            (int(workflow_id), *db.TERMINAL_STATUSES)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def list_workflow_rows(workflow_id: int, row_index=None) -> list[dict]:
    c = db.connect()
    try:
        if row_index is None:
            rows = c.execute(
                "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY workflow_row",
                (int(workflow_id),)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM tasks WHERE workflow_id = ? AND workflow_row = ?",
                (int(workflow_id), int(row_index))).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def user_has_live_regular_tasks(owner_id: int) -> bool:
    """闸门⑥。注意 workflow_id IS NULL —— 行任务自己不能把自己挡住。

    这一句写反（漏掉 IS NULL）的后果没有任何报错：闸门永远为真，工作流永远不放行。
    """
    marks = ",".join("?" * len(_LIVE))
    c = db.connect()
    try:
        return c.execute(
            f"SELECT 1 FROM tasks WHERE owner_id = ? AND workflow_id IS NULL"
            f" AND status IN ({marks}) LIMIT 1",
            (int(owner_id), *_LIVE)).fetchone() is not None
    finally:
        c.close()

