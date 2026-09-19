# ============================================================
# LLM 供应商切换回归
#
# 背景：LLM 从 qwen-max 整体切到 DeepSeek（embedding 仍留 DashScope）。
# 这组测试锁住切供应商时最容易悄悄坏掉的几件事：
#   ① key / model / base_url 必须指向同一服务商（混搭只会得到 401）
#   ② 数据源级 llm_config 要能压过全局默认（「改库在线生效」的前提）
#   ③ 认不出的 provider 必须回退默认，不能猜
#   ④ embedding 不能被顺手换掉（DeepSeek 无 embedding 接口 + 维度已定死）
#
# ★ 曾有一组「重活/轻活分档」的测试，随分档一起删除了：DeepSeek 官方
#   /v1/models 只提供 deepseek-flash 与 deepseek-v4-pro，没有第二个模型可
#   做档位区分（v4-pro 是纯文本、不支持图片，且延迟约 2.4×）。
#   分档代码保留在 git 历史里，要恢复改配置即可。
# ============================================================

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.deps import (  # noqa: E402
    LLM_PROVIDERS,
    get_llm,
    get_llm_for_datasource,
)
from src.core.config import get_settings  # noqa: E402


def test_chat_config_is_internally_consistent():
    """★ CHAT_* 三件套必须指向同一服务商。

    只换 model 不换 base_url（或反过来）是切供应商最容易翻的车：模型名是
    DeepSeek 的、endpoint 还是 DashScope 的，报错只会说「模型不存在」，
    排查方向全错。这里断言默认 provider 的 base_url / key 环境变量名
    与 CHAT_* 配置一致。"""
    settings = get_settings()
    default_provider = "deepseek"
    assert settings.BASE_URL_CHAT == LLM_PROVIDERS[default_provider]["base_url"]
    assert settings.CHAT_API_KEY_ENV == LLM_PROVIDERS[default_provider]["api_key_env"]


def test_default_llm_uses_chat_model_and_base_url():
    settings = get_settings()
    llm = get_llm()
    assert llm.model_name == settings.CHAT_MODEL
    assert llm.openai_api_base == settings.BASE_URL_CHAT


def test_unknown_provider_falls_back_to_default():
    """未注册 provider 一律回退默认，不猜、不报错"""
    llm = get_llm_for_datasource({"provider": "not-a-real-provider", "model": "x"})
    assert llm.model_name == get_settings().CHAT_MODEL


def test_datasource_config_overrides_default():
    """数据源级 llm_config 显式指定模型时优先。

    否则「改库 → 清缓存 → 在线生效」这条链路会失效，表现为
    「改了库但模型没变」，排查成本很高。没有对应 provider 的 key 时
    按契约回退全局默认，不报错、不猜。"""
    settings = get_settings()
    llm = get_llm_for_datasource({"provider": "dashscope", "model": "qwen-max"})
    if not settings.DASHSCOPE_API_KEY:
        assert llm.model_name == settings.CHAT_MODEL
    else:
        assert llm.model_name == "qwen-max"


def test_registry_has_deepseek_and_dashscope():
    """deepseek = LLM 供应商；dashscope 保留给 embedding 与在线回切"""
    assert "deepseek" in LLM_PROVIDERS
    assert "dashscope" in LLM_PROVIDERS
    assert LLM_PROVIDERS["deepseek"]["api_key_env"] == "DEEPSEEK_API_KEY"


def test_embedding_stays_on_dashscope():
    """★ 向量模型不能跟着 LLM 换供应商：DeepSeek 没有 embedding 接口，
    且 Milvus 现存向量是 DashScope text-embedding-v3 的 1024 维 ——
    换供应商要全量重建向量。
    这条测试防的是「切供应商时顺手把 embedding 也改了」。"""
    from src.api.deps import get_embedding_model

    emb = get_embedding_model()
    assert emb.model == get_settings().EMBEDDING_MODEL
    assert get_settings().EMBEDDING_MODEL.startswith("text-embedding")
