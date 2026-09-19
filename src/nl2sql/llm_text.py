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

import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from loguru import logger

from src.core.circuit_breaker import get_llm_breaker
from src.core.metrics import LLM_CALLS, LLM_TOKENS

_LANG_TAGS = ("json", "sql")  # 长的在前，避免 "json" 被 "js" 之类误判（当前就两个，按长到短排）

# 拿不到真实 usage 时按字符数粗估 token（中文 1 token ≈ 1.5 字符，只用于观测趋势）
_CHARS_PER_TOKEN = 1.5


def clean_model_output(text: str) -> str:
    """剥离围栏，再把围栏之外的散文去掉，只留 SQL本体。

    ★ 为什么要去散文：「模型输出的是不是 SQL」靠首 token 判别，而前缀会骗人 ——
      `以下 SQL：` / `这是查询：` / `-- 说明` / 围栏反引号，任何一个前缀都会把
      一条合法 SQL 判成拒答（旧实现 `is_sql_text` 踩的就是这个，见 tests/
      test_refusal_shortcircuit.py）。两种散文形态都要处理：
      ① 围栏前/后各有一坨说明（常见）→ 取围栏内内容；
      ② 没有围栏、SQL 前面垫了一行说明 → 从第一个 SELECT/WITH 起截。
      判别与清洗在同一处收口，"什么是 SQL" 只有一个定义。
    """
    text = (text or "").strip()
    if not text:
        return ""
    if "```" not in text:
        return _cut_to_first_statement(text)
    block = text.split("```")[1]
    # 围栏内第一行可能是语言标签（```sql\nSELECT ...）
    first_line, _, rest = block.partition("\n")
    if first_line.strip().lower() in _LANG_TAGS:
        block = rest
    return _cut_to_first_statement(block.strip())


# 语句起点：行首的 SELECT / WITH（前缀散文通常独占一行）。
_MODEL_OUTPUT_START = re.compile(r"^(?:select|with)\b", re.IGNORECASE | re.MULTILINE)


def _cut_to_first_statement(text: str) -> str:
    """截到第一条语句的起点。★ 必须 `^` 锚行首 —— 不用 `\\b` 扫全文：
    模型拒答「我不能生成删除数据的 SQL 语句。…我可以帮你生成一个查询所有
    投诉记录的 SQL 语句」里就含 SELECT 字样，扫全文会把后半句人话当成 SQL。"""
    m = _MODEL_OUTPUT_START.search(text)
    return text[m.start():] if m else text


def strip_code_fence(text: str) -> str:
    """向后兼容别名（原语义：剥围栏 + 语言标签）。新版同时去围栏外散文，
    见 clean_model_output。"""
    return clean_model_output(text)


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
