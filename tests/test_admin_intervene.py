"""值班台干预动作的「结果可见性」测试。

干预是值班台唯一能改变别人任务的动作：插话、追加要求、切模型、切档位。
这类动作的**结果**必须看得见——这是值班台存在的意义（交代每个动作的结果）。

真机踩出来的缺陷：`sendRpc` 的失败提示只写在**卡片里**的 note 上。
任务刚好在这 10 秒轮询窗口里收口时，整张卡片连同 note 一起被移除，
管理员点完插话转个身，失败提示就没了。消失的不是提示，是
「这个干预动作失败了」这件事本身。

这类缺陷前端冒烟抓不到（它只断言渲染函数不抛），所以只能钉源码契约。
"""
from __future__ import annotations

# 复用 test_admin_static 的读取助手，不另写一份
from test_admin_static import _fn, _read, JS  # noqa: F401


def _send_rpc() -> str:
    return _fn("sendRpc")


def test_intervention_failure_is_written_to_the_board_bar():
    """失败必须往**板级**提示条再写一份。

    卡片里的 note 活不过下一轮轮询：任务在这 10 秒里收口 → 卡片被移除 →
    提示一起没了。承载提示的 DOM 节点不该和提示的生命周期绑定。
    """
    body = _send_rpc()
    catch = body.split("catch")[-1]
    assert "setMsg" in catch, (
        "干预失败只写在卡片 note 上：卡片一被移除，管理员就再也看不到"
        "「刚才那个干预失败了」")
    assert "board-msg" in catch, "失败提示没写到板级提示条上"
    assert "干预失败" in catch, "板级提示没说明这是干预失败，容易被当成别的错误"


def test_intervention_success_does_not_clobber_the_board_bar():
    """成功路径不该刷板级提示条。

    顶部的提示条是全局状态条；每次插话成功都去覆盖它，
    会把管理员刚看到的别的东西（比如产物确认结果）顶掉。
    """
    body = _send_rpc()
    ok_path = body.split("catch")[0]
    assert "board-msg" not in ok_path, (
        "干预成功也去写板级提示条，会盖掉顶部的其他状态")


def test_the_note_is_still_set_immediately():
    """即时反馈不能因为加了板级提示就丢掉——卡片还在时，离手最近的那个位置
    才是第一眼看到的地方。"""
    body = _send_rpc()
    assert body.count("note.textContent") >= 3, (
        "卡片上的即时反馈（发送中 / 成功 / 失败）被改动了")


def test_the_four_interventions_are_all_wired():
    """插话、追加要求、切模型、切档位四个入口都要真的发 RPC。

    这四个是 spec §8.1 点名的干预能力。少一个就是值班台少一块功能，
    而按钮少一个不会让任何测试变红——除非有人专门数。
    """
    src = _read(JS)
    for command in ("steer", "follow_up", "set_model", "set_thinking_level"):
        assert f'"{command}"' in src or f"'{command}'" in src, (
            f"干预命令 {command} 在前端没有任何入口")
