"""平台直连模型的入口：配置常量 + 一次 chat 补全。

**为什么单独一个文件**：模型配置原先长在 :mod:`dispatcher` 里，而反思
（任务收工后的自我总结）也要用同一套端点。放在 dispatcher 里就得让
reflection 反向 import 一个 118KB 的模块（还容易绕成循环依赖），所以
把配置与调用一并搬到这里，dispatcher 改成从这儿取——它对外的名字保持不变，
``dispatcher.LLM_PROVIDER_ID`` 之类的引用照旧能用。

零外链零依赖：走标准库 :mod:`urllib`，和 server/ 其余部分一致。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

PI_AGENT_DIRNAME = ".pi-agent"

LLM_BASE_URL_ENV = "PROTFORGE_LLM_BASE_URL"
LLM_MODEL_ENV = "PROTFORGE_LLM_MODEL"
# 放的是"环境变量名"，不是密钥本身
LLM_KEY_ENV_ENV = "PROTFORGE_LLM_API_KEY_ENV"
LLM_CTX_ENV = "PROTFORGE_LLM_CONTEXT_WINDOW"
LLM_MAXTOK_ENV = "PROTFORGE_LLM_MAX_TOKENS"
LLM_PROVIDER_ID = "protforge-llm"

# 默认值与 llm-quant 的 [llm] 段一致——那是本机上已经跑通过的同一套端点。
_LLM_DEFAULTS = {
    LLM_BASE_URL_ENV: "https://api.minimax.cn/v1",
    LLM_MODEL_ENV: "MiniMax-M3.1-Flash-Preview",
    LLM_KEY_ENV_ENV: "MINIMAX_API_KEY",
    LLM_CTX_ENV: "1000000",
    LLM_MAXTOK_ENV: "8192",
}

# 反思这一次调用要等多久。给得比 RPC_TIMEOUT 长：这是一次完整的读+写，
# 而 RPC_TIMEOUT 是给管理员"问一句很快就有回音"的命令设计的。
CHAT_TIMEOUT = 300.0

# 一次调用的输出预算。**思考也算在里面**：M3.1 的 reasoning_content 与正文
# 一起算 max_completion_tokens——思考花光的额度，正文一个字都拿不到。
CHAT_MAX_COMPLETION_TOKENS = 32768


class LLMError(RuntimeError):
    """模型调不通。调用方据此把反思标成失败，而不是让后台线程死掉。"""


def _llm_setting(env_name: str) -> str:
    return (os.environ.get(env_name) or _LLM_DEFAULTS[env_name]).strip()


def base_url() -> str:
    return _llm_setting(LLM_BASE_URL_ENV).rstrip("/")


def model_id() -> str:
    return _llm_setting(LLM_MODEL_ENV)


def key_env() -> str:
    """密钥所在的**环境变量名**。返回名字而不是密钥值。"""
    return _llm_setting(LLM_KEY_ENV_ENV)


def have_key() -> bool:
    return bool(os.environ.get(key_env(), "").strip())


def materialize_model_config(work_dir: Path) -> tuple[Path, bool]:
    """在任务工作目录里写一份 pi 的 models.json。返回 (agent 目录, 是否有密钥)。

    **密钥不写进文件**：apiKey 写成 ``$MINIMAX_API_KEY``，由 pi 在运行时从
    环境变量插值。写死密钥的话，密钥会跟着任务目录进备份、进同步、可能进 zip。
    """
    env = key_env()
    have = have_key()

    agent_dir = Path(work_dir) / PI_AGENT_DIRNAME
    agent_dir.mkdir(parents=True, exist_ok=True)

    def _int(env_name: str) -> int:
        raw = _llm_setting(env_name)
        try:
            return int(raw)
        except ValueError:
            log.warning("%s 不是整数（%r），退回默认值", env_name, raw)
            return int(_LLM_DEFAULTS[env_name])

    config = {
        "providers": {
            LLM_PROVIDER_ID: {
                "name": "ProtForge 计算平台（OpenAI 兼容）",
                "baseUrl": base_url(),
                "api": "openai-completions",
                "apiKey": f"${env}",
                # **models 是数组，不是按 id 建的字典**，而且每项必须有 id。
                # 写成字典不会报任何错，整个 provider 会被静默丢掉——表现是
                # ModelRuntime.getModels(provider) 返回空，agent 就一直不吭声。
                "models": [{
                    "id": model_id(),
                    "name": model_id(),
                    "api": "openai-completions",
                    "reasoning": True,
                    "input": ["text"],
                    "contextWindow": _int(LLM_CTX_ENV),
                    "maxTokens": _int(LLM_MAXTOK_ENV),
                    "cost": {"input": 0, "output": 0,
                             "cacheRead": 0, "cacheWrite": 0},
                }],
            },
        },
    }
    (agent_dir / "models.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return agent_dir, have


def chat_completion(prompt: str, *, system: str = "",
                    max_completion_tokens: int | None = None,
                    reasoning_effort: str | None = None,
                    timeout: float = CHAT_TIMEOUT) -> str:
    """一次 chat 补全，返回助手正文。

    只取 ``choices[0].message.content``。**没有重试**：反思是一次性记录，
    重试会让"这条反思出自哪次调用"这件事说不清；失败就如实标成失败，
    管理员看得见，也不会把一次失败的记录当成一条空经验写进记忆。

    **「被截断」与「结构不对」必须分开说。** 真机上撞到过：默认思考档把
    8192 的预算全花在思考上，正文那一格压根**没有**（``finish_reason`` 是
    ``length``），而这里原来直接 ``["content"]`` → KeyError → 报成
    「模型回的不是预期的 chat 结构」——既不真（结构是对的）也看不出该改
    什么（该改的是档位或预算），而错误里带的那 200 字符全是思考的开头。

    **思考档位不给就等于用模型默认档，而默认档会自己长。** 实测「按反馈改
    一份 2KB 技能文档」：默认档思考了 9,441 tokens（43k 字符）、花 165 秒，
    改出来 246 行；low 档思考 385 tokens、9.6 秒，改出来 79 行。
    档位只能给合法值——M3.1 的 thinking 强制开启，none/disabled 直接 400。
    """
    if not have_key():
        raise LLMError(f"环境变量 {key_env()} 没值，发不出模型请求")

    limit = int(max_completion_tokens or CHAT_MAX_COMPLETION_TOKENS)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_id(),
        "messages": messages,
        # **正式参数是 max_completion_tokens**（max_tokens 已弃用）。
        # 两者在这个端点上都认，但用正式的那个才不会哪天被上游删掉。
        "max_completion_tokens": limit,
        "temperature": 0.2,      # 复盘要的是如实，不是发挥
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url()}/chat/completions", data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get(key_env(), '').strip()}",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:                                     # noqa: BLE001
            pass
        raise LLMError(f"模型返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连不上模型端点：{exc.reason}") from exc
    except OSError as exc:                          # 含超时
        raise LLMError(f"请求模型失败：{exc}") from exc

    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise LLMError(f"模型回的不是 JSON：{raw[:200]}") from exc
    if not isinstance(parsed, dict) or not parsed.get("choices"):
        raise LLMError(f"模型回的不是预期的 chat 结构（没有 choices）：{raw[:200]}")
    choice = parsed["choices"][0]
    if not isinstance(choice, dict):
        raise LLMError(f"模型回的不是预期的 chat 结构：{raw[:200]}")
    msg = choice.get("message")
    msg = msg if isinstance(msg, dict) else {}
    finish = choice.get("finish_reason") or ""
    content = msg.get("content")
    think = msg.get("reasoning_content") or ""
    # **截断单独说**：正文那一格没有，是预算被思考吃光了。
    # 这时候报「结构不对」会把人引到完全错误的方向上去查。
    if finish == "length" or (content is None and think):
        raise LLMError(
            f"模型输出在 {limit} tokens 处被截断"
            f"（finish_reason={finish or 'length'}），只回了思考过程没回正文"
            f"（思考了 {len(think)} 字符）。"
            "把思考档位调低（reasoning_effort=low）或把预算调大再试。")
    if not isinstance(content, str):
        raise LLMError(
            f"模型回的不是预期的 chat 结构（message 里没有 content）：{raw[:200]}")
    if not content.strip():
        raise LLMError("模型回了一段空内容")
    return content
