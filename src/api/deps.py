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
    """全局默认 LLM（全链路单模型）。

    ★ CHAT_MODEL / BASE_URL_CHAT / CHAT_API_KEY_ENV 三件套必须指向同一个服务商 ——
      默认是 DeepSeek，所以默认也读 DEEPSEEK_API_KEY。只改 CHAT_MODEL 却仍用
      DashScope 的 key 和 endpoint，会得到 401。
    """
    settings = get_settings()
    return _get_llm_for(settings.CHAT_MODEL, _provider_key("deepseek"),
                        settings.BASE_URL_CHAT,
                        settings.LLM_REQUEST_TIMEOUT, settings.LLM_MAX_RETRIES)


def _provider_key(provider: str) -> str:
    """provider 名 → 密钥。优先 settings（.env 已加载），兜底进程环境 ——
    避免「.env 有 key 但 os.getenv 取不到」导致的静默回退。"""
    settings = get_settings()
    env_name = LLM_PROVIDERS[provider]["api_key_env"]
    return getattr(settings, env_name, "") or os.getenv(env_name, "")


@lru_cache
def _get_embedding_model() -> DashScopeEmbeddings:
    """★ 向量模型必须留在 DashScope：DeepSeek **没有 embedding 接口**，
    而 Milvus 里已有的列/指标/示例向量全是 text-embedding-v3 的 1024 维。
    换 embedding 供应商 = 维度对不上 + 全部向量必须重建，不是改个配置的事。
    所以「LLM 全换 DeepSeek」不含这一层。"""
    settings = get_settings()
    return DashScopeEmbeddings(
        model=settings.EMBEDDING_MODEL,
        dashscope_api_key=settings.DASHSCOPE_API_KEY,
    )


# ── 在线模型切换：provider 注册表（密钥只从环境读，不进库）──
# ★ dashscope 这一项当前**不可用**（实测 2026-09-19）：同一个 DASHSCOPE_API_KEY
#   打 /embeddings 成功（1024 维正常返回），打 /chat/completions 却 401
#   `Incorrect API key provided` —— 说明该账号的 LLM 权限与 embedding 权限
#   不是一回事。注册表保留它，是为了「拿到可用的 DashScope LLM key 后回切
#   qwen 无需改代码」，但**不要假设现在切过去能跑**。
LLM_PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
    },
    "dashscope": {
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
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

    ★ 数据源级配置**优先于**全局默认：用户在库里显式指定了模型就该用它。
      代价：数据源级模型若不支持图片输入，未来接多模态要在这里挡一道。

    切换路径：UPDATE bi_datasources SET llm_config=... → clear_datasource_cache()
    → 下一个请求即用新模型（无需重启）。
    """
    settings = get_settings()
    cfg = llm_config or {}
    provider = cfg.get("provider", "deepseek")
    model = cfg.get("model") or settings.CHAT_MODEL
    prov = LLM_PROVIDERS.get(provider)
    if prov is None:  # 未注册 provider 一律回退默认，不猜
        return get_llm()
    api_key = _provider_key(provider)
    if not api_key:
        return get_llm()
    return _get_llm_for(model, api_key, prov["base_url"],
                        settings.LLM_REQUEST_TIMEOUT, settings.LLM_MAX_RETRIES)


def get_llm() -> ChatOpenAI:
    return _get_llm()


def get_embedding_model() -> DashScopeEmbeddings:
    return _get_embedding_model()
