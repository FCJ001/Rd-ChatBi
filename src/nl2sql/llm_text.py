# ============================================================
# LLM 输出清洗 + 带熔断的调用封装
#
# ★ 不要用 str.lstrip("sql") 剥语言标签：lstrip 按"字符集合"剥离，
#   ```select ... 会被剥成 elect ...（s 剥掉、e 停下），null → ull。
#   必须用 startswith 判断整个标签再按长度截断。
#
# ★ safe_ainvoke 是流水线里**唯一**的 LLM 调用入口：
#   - 熔断器包住所有 LLM 调用（漏掉一处就是个雪崩入口）
#   - token 用量在一处统计（metrics 里 llm_tokens_total 之前定义了没人 inc）
# ============================================================

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from loguru import logger

from src.core.circuit_breaker import get_llm_breaker
from src.core.metrics import LLM_CALLS, LLM_TOKENS

_LANG_TAGS = ("json", "sql")  # 长的在前，避免 "json" 被 "js" 之类误判（当前就两个，按长到短排）

# 拿不到真实 usage 时按字符数粗估 token（中文 1 token ≈ 1.5 字符，只用于观测趋势）
_CHARS_PER_TOKEN = 1.5


def strip_code_fence(text: str) -> str:
    """剥离 ```...``` 围栏和语言标签，返回纯内容。无围栏时原样返回（仅去首尾空白）。"""
    text = text.strip()
    if "```" not in text:
        return text
    block = text.split("```")[1]
    # 围栏内第一行可能是语言标签（```sql\nSELECT ...）
    first_line, _, rest = block.partition("\n")
    if first_line.strip().lower() in _LANG_TAGS:
        block = rest
    return block.strip()


def _text_of(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        # 多模态 content 形如 [{"type": "text", "text": ...}, ...]
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "")


def _record_usage(llm: BaseChatModel, messages: list[BaseMessage], response: Any) -> None:
    """记 token 指标。优先真实 usage，拿不到按字符数粗估（量级正确即可）。"""
    model = getattr(llm, "model_name", None) or getattr(llm, "model", None) or "unknown"
    LLM_CALLS.labels(model=model).inc()

    prompt_tokens = completion_tokens = None
    meta = getattr(response, "response_metadata", None)
    if isinstance(meta, dict):
        usage = meta.get("token_usage") or meta.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
            completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens")

    if prompt_tokens is None:
        prompt_tokens = int(sum(len(_text_of(m)) for m in messages) / _CHARS_PER_TOKEN)
    if completion_tokens is None:
        completion_tokens = int(len(_text_of(response)) / _CHARS_PER_TOKEN)

    LLM_TOKENS.labels(model=model, kind="input").inc(prompt_tokens)
    LLM_TOKENS.labels(model=model, kind="output").inc(completion_tokens)


async def safe_ainvoke(llm: BaseChatModel, messages: list[BaseMessage]) -> Any:
    """带熔断的 llm.ainvoke。

    熔断打开时立刻抛 CircuitOpenError —— 快速失败，不再等满 request_timeout。
    调用方（节点）应把它当成「上游不可用」处理，与「SQL 写错了」区分开。"""
    breaker = get_llm_breaker()

    async def _call():
        return await llm.ainvoke(messages)

    response = await breaker.call(_call)
    try:
        _record_usage(llm, messages, response)
    except Exception as e:
        # 指标统计失败绝不能影响主流程
        logger.debug(f"[llm_text] 记录 token 用量失败: {e}")
    return response
