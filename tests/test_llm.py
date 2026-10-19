"""模型直连那一层的围栏：请求怎么发、回来的东西怎么判。

**这一层是被真机撞出来的**：「工具意见 → 生成修改方案」那条写路径在真实
技能文件上**从来没跑通过**。模型把 8192 的预算全花在思考上，正文那一格
压根没有（``finish_reason`` 是 ``length``），而代码直接
``parsed["choices"][0]["message"]["content"]`` → KeyError → 报成
「模型回的不是预期的 chat 结构」。

那条报错把人引到了完全错误的方向上：

* **不真**——结构是对的，是内容被截断了；
* **看不出该改什么**——该改的是思考档位与预算，不是「换个模型」；
* **错误里那 200 字符全是思考的开头**，谁都看不出是被截断的。

所以这里钉四件事：请求用正式参数名、档位可传可不传、**截断单独说**、
真正结构不对时仍然说结构不对。
"""
from __future__ import annotations

import json

import pytest

import llm


class _Resp:
    """够用的响应桩：只提供 read 与上下文管理器。"""

    def __init__(self, raw: str):
        self._raw = raw.encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def sent(monkeypatch):
    """换掉 urlopen，并把**发出去的请求体**留下来。

    请求体是这里唯一能验的东西：参数名、档位、预算都在里面。
    """
    box = {"reply": "{}", "body": None}

    def _fake(req, timeout=None):
        box["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(box["reply"])

    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake)
    monkeypatch.setenv(llm.key_env(), "test-key")
    return box


def _reply(finish: str = "stop", content=None, reasoning: str | None = None) -> str:
    msg = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return json.dumps({"choices": [{"finish_reason": finish, "index": 0, "message": msg}]})


# --- 请求 -------------------------------------------------------------------

def test_the_request_uses_the_official_parameter_name(sent):
    """**max_completion_tokens，不是 max_tokens。**

    M3.1 正式参数是 max_completion_tokens（max_tokens 已弃用）。两个在这个
    端点上都被接受，但用正式的那个才不会哪天被上游删掉——而删掉的那天，
    表现是 400 或者更糟：**预算悄悄没生效，思考把正文挤没了**。
    """
    sent["reply"] = _reply(content="正文")
    llm.chat_completion("问", max_completion_tokens=12345)
    body = sent["body"]
    assert body.get("max_completion_tokens") == 12345, body
    assert "max_tokens" not in body, f"还在发已弃用的参数：{body}"


def test_the_effort_is_sent_only_when_asked_for(sent):
    """**不给档位 = 用模型默认档**，默认档会自己长。

    实测「按反馈改一份 2KB 技能文档」：默认档思考了 9,441 tokens（43k 字符）、
    花 165 秒，改出来 246 行；low 档思考 385 tokens、9.6 秒，改出来 79 行。

    所以「不传」和「传 low」是两种不同的行为，围栏要把它们分开钉住：
    传了要真的发出去，没传就**一个字都不许发**（发了空串会让部分端点报错）。
    """
    sent["reply"] = _reply(content="正文")
    llm.chat_completion("问", reasoning_effort="low")
    assert sent["body"].get("reasoning_effort") == "low", sent["body"]

    sent["body"] = None
    llm.chat_completion("问")
    assert "reasoning_effort" not in sent["body"], sent["body"]


def test_the_budget_has_a_default_that_fits_thinking(sent):
    """默认预算必须**容得下思考**。

    思考算在同一个额度里。预算给小了，模型思考完就没额度写正文了——
    这不是「输出短一点」，是**一个字都拿不到**。
    """
    sent["reply"] = _reply(content="正文")
    llm.chat_completion("问")
    assert sent["body"]["max_completion_tokens"] >= 16384, sent["body"]


# --- 回来的东西 -------------------------------------------------------------

def test_a_truncated_reply_says_it_was_truncated(sent):
    """**被截断要单独说。** 这正是真机撞到的那一种。

    ``finish_reason="length"`` 时正文那一格是**没有的**——模型把预算全花在
    思考上了。报成「结构不对」既不真，也看不出该改什么。
    """
    sent["reply"] = _reply(finish="length", reasoning="我先想想这个技能怎么改……" * 20)
    with pytest.raises(llm.LLMError) as e:
        llm.chat_completion("问", max_completion_tokens=8192)
    msg = str(e.value)
    assert "截断" in msg, msg
    assert "预期的 chat 结构" not in msg, f"截断被报成了结构不对：{msg}"
    # 说得清该改什么：档位或预算。
    assert "reasoning_effort" in msg or "预算" in msg, msg


def test_a_truncated_reply_without_thinking_is_still_caught(sent):
    """连 reasoning 都没有、只有 finish_reason=length 的那种截断，也要说截断。"""
    sent["reply"] = json.dumps({
        "choices": [{"finish_reason": "length", "index": 0, "message": {"role": "assistant"}}]})
    with pytest.raises(llm.LLMError) as e:
        llm.chat_completion("问")
    assert "截断" in str(e.value), str(e.value)


def test_a_wrong_shape_still_says_the_shape_is_wrong(sent):
    """**别把什么都报成截断。** 结构真的不对时，要说结构不对。

    否则「结构不对」这条消息就变成了垃圾桶：什么错都往里装，
    管理员照着它去查预算，查半天发现不是那回事。
    """
    sent["reply"] = json.dumps({"foo": 1})
    with pytest.raises(llm.LLMError) as e:
        llm.chat_completion("问")
    assert "预期的 chat 结构" in str(e.value), str(e.value)
    assert "截断" not in str(e.value), str(e.value)


def test_a_message_without_content_is_not_a_key_error(sent):
    """message 里没有 content 时，**不许**冒出 KeyError。

    原来这里是 ``parsed["choices"][0]["message"]["content"]`` 直接下标：
    KeyError 会带着一串下标路径从 chat_completion 里穿出去，调用方看到的
    是一句 `KeyError: 'content'`——而它其实是个模型返回形状的问题。
    """
    sent["reply"] = json.dumps({
        "choices": [{"finish_reason": "stop", "index": 0, "message": {"role": "assistant"}}]})
    with pytest.raises(llm.LLMError) as e:
        llm.chat_completion("问")
    assert "KeyError" not in str(e.value), str(e.value)
    assert "预期的 chat 结构" in str(e.value), str(e.value)


def test_an_empty_reply_is_still_reported_as_empty(sent):
    """空内容还是空内容，别被上面那几条改掉。"""
    sent["reply"] = _reply(content="   \n  ")
    with pytest.raises(llm.LLMError) as e:
        llm.chat_completion("问")
    assert "空内容" in str(e.value), str(e.value)


def test_a_normal_reply_comes_back_untouched(sent):
    sent["reply"] = _reply(content="  正文  ")
    assert llm.chat_completion("问") == "  正文  "
