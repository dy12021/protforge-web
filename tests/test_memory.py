"""长期记忆与工具问题：数据层的围栏。

**这些测试一个模型都不调。** 复核那条路上的模型调用被换成一个
可替换的 ``runner`` 桩：要验的是「每天一次」的判定、限量的边界、
容错与幂等，不是模型答得好不好。

**数据根是隔离的**（``conftest.py`` 把它指向 tmp_path），本文件自己带夹具，
不依赖任何真实路径，也不碰真实的技能仓库。

围栏必须**双向**：缺陷要红，正确实现不能误红。所以每一条都写成
「为什么会坏」的样子，而不是「现在的实现是什么样子」。
"""
from __future__ import annotations

import json

import memory

DAY = "2026-10-22"
NEXT_DAY = "2026-10-23"


def _write_memory(text: str) -> None:
    p = memory.long_term_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _fake_runner(box: dict):
    """一个「跑成功」的复核桩：记下调用次数与拿到的提示词。"""
    def _run(brief: str, work_dir):
        box["calls"] = box.get("calls", 0) + 1
        box["brief"] = brief
        box["work_dir"] = str(work_dir)
        return {"ok": True, "returncode": 0, "output": "整理完了", "error": ""}
    return _run


# --- 注入块：容错 -------------------------------------------------------------


def test_the_block_is_empty_when_the_memory_file_does_not_exist():
    """文件不在 → 读成「没有内容」，不是异常。

    这个文件是**每个任务开工都要读**的：它坏一次就等于整批任务全炸。
    """
    assert not memory.long_term_path().exists()
    assert memory.read_long_term() == ""
    assert memory.read_entries() == []
    assert memory.memory_block() == ""


def test_broken_bytes_in_the_memory_file_do_not_raise_and_keep_the_good_part():
    """编码坏掉的那一段读成替换字符，**好的一条照样读得出来**。

    整个文件读不出来和读出一半，对下一个任务来说是两回事：
    前者什么都不带，后者还带着能用的那几条。
    """
    p = memory.long_term_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("- [技能 protforge-demo] 好的一条（来源：任务 #1；时效性：稳定）\n",
                 encoding="utf-8")
    with p.open("ab") as fh:
        fh.write(b"- [\xff\xfe] \x81\x81 broken\n")

    block = memory.memory_block()
    assert "好的一条" in block, "一条坏字节把整份文件废掉了"
    assert "broken" not in block, "坏的那条被当成经验读出来了"


def test_a_memory_file_without_entries_reads_as_no_content():
    """格式坏了（没有一行是条目）→ 空块。

    解析不了的东西宁可丢掉，也不要猜：猜出来的条目会被注入**每一个**
    任务的提示词。
    """
    _write_memory("这不是一条经验\n随便一段话\n# 一个标题\n")
    assert memory.memory_block() == ""
    assert memory.read_entries() == []


# --- 注入块：限量 -------------------------------------------------------------


def test_five_hundred_entries_do_not_blow_up_the_block():
    """塞 500 条也不会爆：条数与字数两重上限都在。

    长期记忆无上限增长，而每个任务的上下文不是。一份塞满几百条旧经验的
    提示词会把真正的需求挤到注意力之外，而且那份税是从**所有**任务身上收的。
    """
    for i in range(500):
        memory.append_memory(f"第 {i} 条经验，内容要足够长才能被上限砍掉" * 6,
                             skill="protforge-demo", source=f"任务 #{i}")

    block = memory.memory_block()
    assert block.strip(), "有 500 条经验却一块都没注入"
    assert len(block) <= memory.MEMORY_BLOCK_MAX, f"块有 {len(block)} 字，超了上限"
    assert block.count("\n") + 1 <= memory.MEMORY_BLOCK_ITEMS, "条数超了上限"


def test_only_the_most_recent_entries_are_injected():
    """全量留在文件里，提示词只带最近这些。"""
    for i in range(memory.MEMORY_BLOCK_ITEMS + 6):
        memory.append_memory(f"经验-{i}", source=f"任务 #{i}")

    block = memory.memory_block()
    assert "经验-0" not in block
    assert f"经验-{memory.MEMORY_BLOCK_ITEMS + 5}" in block


# --- 工具问题：按日期追加 ------------------------------------------------------


def test_two_issues_on_the_same_day_are_two_entries():
    """同一天追加两次 → 两条，**不是一条**。

    覆盖掉的话前面那条就永远没了，而用户是按文件逐条修的。
    """
    memory.append_tool_issue("第一个问题：这个工具缺了 --seed 参数", day=DAY)
    memory.append_tool_issue("第二个问题：这一步只能靠猜", day=DAY)

    text = memory.read_tool_issues(DAY)
    assert "第一个问题" in text and "第二个问题" in text, text
    assert text.count("第一个问题") == 1
    assert len(memory.list_tool_issue_files()) == 1, "同一天不该有两个文件"


def test_a_new_day_gets_its_own_file():
    """跨天 → 新文件，前一天的内容不动。"""
    memory.append_tool_issue("第一天的问题", day=DAY)
    memory.append_tool_issue("第二天的问题", day=NEXT_DAY)

    assert memory.tool_issue_path(DAY).is_file()
    assert memory.tool_issue_path(NEXT_DAY).is_file()
    assert "第一天的问题" not in memory.read_tool_issues(NEXT_DAY)

    dates = [f["date"] for f in memory.list_tool_issue_files()]
    assert dates == [NEXT_DAY, DAY], "工具问题文件没有按日期倒序排"


def test_the_issue_file_is_named_after_the_date():
    """文件名就是日期：用户按日期找文件，不用猜哪份是哪天的。"""
    memory.append_tool_issue("一个问题", day=DAY)
    assert memory.tool_issue_path(DAY).name == "2026-10-22.md"
    assert memory.tool_issue_path(DAY).parent == memory.tool_issues_dir()


def test_an_empty_issue_is_not_written():
    """空文本不写：建一个只有标题的空文件没有意义。"""
    memory.append_tool_issue("   ", day=DAY)
    assert not memory.tool_issue_path(DAY).exists()


# --- 每日复核：每天一次 --------------------------------------------------------


def test_the_second_run_on_the_same_day_does_not_review_again():
    """同一天第二次调用**不再整理**。

    整理要调模型、要联网查证，一天一次就够。反复整理既费钱，
    又会把刚刚确认过的东西再改一遍。
    """
    box = {}
    first = memory.run_daily_review(now=f"{DAY}T09:00:00", runner=_fake_runner(box))
    assert first["ok"] and not first["skipped"]
    assert box["calls"] == 1

    second = memory.run_daily_review(now=f"{DAY}T23:59:00", runner=_fake_runner(box))
    assert second["skipped"], "同一天整理了两次"
    assert box["calls"] == 1


def test_the_review_state_is_persisted_on_disk():
    """「今天整理过了」是**落盘**的，不靠进程内存。

    平台有 public / admin / internal 三个进程：靠内存的话，
    每个进程都以为「今天还没整理过」，一天能整理三次。
    """
    box = {}
    memory.run_daily_review(now=f"{DAY}T09:00:00", runner=_fake_runner(box))

    p = memory.review_state_path()
    assert p.is_file(), "复核状态没有落盘"
    state = json.loads(p.read_text(encoding="utf-8"))
    assert state["last_review_date"] == DAY
    assert state["last_ok"] is True


def test_forcing_a_review_runs_it_again():
    """force=True 时无视「今天已经整理过」，强制再跑一次。"""
    box = {}
    memory.run_daily_review(now=f"{DAY}T09:00:00", runner=_fake_runner(box))
    out = memory.run_daily_review(now=f"{DAY}T10:00:00", force=True,
                                  runner=_fake_runner(box))
    assert not out["skipped"]
    assert box["calls"] == 2


def test_a_failed_review_can_be_retried_the_same_day():
    """**失败的那次不算「今天整理过了」。**

    一次偶发失败（模型调不通、网络抽风）不该把这一整天的复核都卡掉——
    那样的话一条过期经验会带着一整天的旧知识去喂后面每一个任务。
    """
    def _boom(brief, work_dir):
        return {"ok": False, "returncode": 1, "output": "", "error": "模型调不通"}

    first = memory.run_daily_review(now=f"{DAY}T09:00:00", runner=_boom)
    assert not first["ok"]

    box = {}
    second = memory.run_daily_review(now=f"{DAY}T10:00:00", runner=_fake_runner(box))
    assert not second["skipped"], "一次失败把当天的额度占掉了"
    assert box["calls"] == 1


def test_the_review_brief_says_where_everything_is_and_what_to_check():
    """提示词必须交代清楚：东西在哪、怎么判失效、失效了怎么办、什么时候算完。

    少交代一样，复核进程就凭自己的理解乱改——而它改的是每个任务都要读的东西。
    """
    memory.append_tool_issue("某工具缺了个参数，只能靠猜", day=DAY)
    brief = memory.build_review_brief(DAY)

    assert str(memory.long_term_path()) in brief, "没告诉它长期记忆在哪"
    assert str(memory.tool_issues_dir()) in brief, "没告诉它工具问题在哪"
    assert str(memory.tool_issue_path(DAY)) in brief, "没告诉它当天的文件是哪份"
    assert "某工具缺了个参数" in brief, "没把已经攒了哪些工具问题列出来"
    assert memory.VOLATILE_MARK in brief, "没说要查证标注了时效性的内容"
    assert "联网搜索" in brief, "没说要联网查证"
    assert "删掉" in brief and "改写" in brief, "没说确认失效了怎么办"
    assert str(memory.review_report_path(DAY)) in brief, "没说什么时候算整理完"


# --- 迁移 ---------------------------------------------------------------------


def _write_legacy_lessons() -> None:
    p = memory.legacy_lessons_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# 已沉淀的经验\n\n"
        "- 先跑一条野生型对照定标，再批量跑（来自任务 #12，admin 于 2026-10-19 归档）\n"
        "- 批量跑之前先把参数口径写下来（来自任务 #13，admin 于 2026-10-19 归档）\n",
        encoding="utf-8")


def test_the_old_lessons_file_moves_into_the_long_term_memory():
    """旧的全局经验文件搬进长期记忆，带上来源。"""
    _write_legacy_lessons()

    out = memory.migrate_legacy()
    assert out["lessons"]["added"] == 2, out

    text = memory.long_term_path().read_text(encoding="utf-8")
    assert "先跑一条野生型对照定标" in text
    assert "批量跑之前先把参数口径写下来" in text
    assert "任务 #12" in text, "来源没带过来：以后没法顺着它找到出处"


def test_migrating_twice_changes_nothing():
    """迁移**幂等**：跑两次结果一样，不会写出第二遍。

    幂等靠内容去重，不靠状态文件：状态文件丢了、或者有人手工改过长期记忆，
    重跑也不该写出重复条目——而重复的经验会被每个任务都读一遍。
    """
    _write_legacy_lessons()

    first = memory.migrate_legacy()
    snapshot = memory.long_term_path().read_bytes()

    second = memory.migrate_legacy()
    assert second["lessons"]["added"] == 0, f"第二次又搬了一遍：{second}"
    assert second["lessons"]["skipped"] == first["lessons"]["skipped"] + first["lessons"]["added"]
    assert memory.long_term_path().read_bytes() == snapshot, "第二次迁移改了文件"


def test_the_old_tool_gaps_land_in_the_dated_issue_file():
    """旧反思正文里的「工具意见」搬进**按日期命名**的文件，且同样幂等。"""
    d = memory.legacy_reflections_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "task-7.md").write_text(
        "# 反思 · 任务 #7\n\n- 收工时间：2026-10-18T11:33:01.000\n\n"
        "## 工具意见\n- [技能 protforge-demo] 这个技能没留下调用记录\n",
        encoding="utf-8")

    out = memory.migrate_legacy()
    assert out["tool_issues"]["added"] == 1, out

    text = memory.read_tool_issues("2026-10-18")
    assert "这个技能没留下调用记录" in text, text
    assert "protforge-demo" in text, "技能名没带过来"

    again = memory.migrate_legacy()
    assert again["tool_issues"]["added"] == 0, f"第二次又搬了一遍：{again}"
