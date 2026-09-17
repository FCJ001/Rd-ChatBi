# ============================================================
# API 层共享依赖：LLM / Embedding 模型实例（模块级缓存）
# ============================================================

import os
from functools import lru_cache

from langchain_community.embeddings import DashScopeEmbeddings
from langchain_openai import ChatOpenAI

from src.core.config import get_settings


@lru_cache
def _get_llm() -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.CHAT_MODEL,
        api_key=settings.DASHSCOPE_API_KEY,
        base_url=settings.BASE_URL_CHAT,
        temperature=0,
        # 不设超时的话 LLM 挂起会无限占用 SSE 连接与工作协程
        request_timeout=settings.LLM_REQUEST_TIMEOUT,
        max_retries=settings.LLM_MAX_RETRIES,
    )


@lru_cache
def _get_llm_deepseek() -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.DEEPSEEK_MODEL,
        api_key=settings.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
        temperature=0,
    )


@lru_cache
def _get_embedding_model() -> DashScopeEmbeddings:
    settings = get_settings()
    return DashScopeEmbeddings(
        model=settings.EMBEDDING_MODEL,
        dashscope_api_key=settings.DASHSCOPE_API_KEY,
    )


# ── 在线模型切换：provider 注册表（密钥只从环境读，不进库）──
LLM_PROVIDERS: dict[str, dict[str, str]] = {
    "dashscope": {
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
    },
}


@lru_cache(maxsize=8)
def _get_llm_for(model: str, api_key: str, base_url: str, timeout: float, retries: int) -> ChatOpenAI:
    """按 (model, endpoint) 复用实例——同配置只建一次连接。"""
    return ChatOpenAI(
        model=model, api_key=api_key, base_url=base_url,
        temperature=0, request_timeout=timeout, max_retries=retries,
    )


def get_llm_for_datasource(llm_config: dict | None) -> ChatOpenAI:
    """数据源级 LLM：llm_config={"provider","model"}，缺省回退全局 CHAT_MODEL。

    切换路径：UPDATE bi_datasources SET llm_config=... → clear_datasource_cache()
    → 下一个请求即用新模型（无需重启）。
    """
    settings = get_settings()
    cfg = llm_config or {}
    provider = cfg.get("provider", "dashscope")
    model = cfg.get("model") or settings.CHAT_MODEL
    prov = LLM_PROVIDERS.get(provider)
    if prov is None:  # 未注册 provider 一律回退默认，不猜
        return get_llm()
    # key 优先从 settings（.env 已加载）取，兜底进程环境——避免「.env 有 key
    # 但 os.getenv 取不到」导致的静默回退
    api_key = getattr(settings, prov["api_key_env"], "") or os.getenv(prov["api_key_env"], "")
    if not api_key:
        return get_llm()
    return _get_llm_for(model, api_key, prov["base_url"],
                        settings.LLM_REQUEST_TIMEOUT, settings.LLM_MAX_RETRIES)


def get_llm() -> ChatOpenAI:
    return _get_llm()


def get_embedding_model() -> DashScopeEmbeddings:
    return _get_embedding_model()
