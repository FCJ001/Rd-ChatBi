# ============================================================
# Elasticsearch 单例 client — NL2SQL 列值召回
# ============================================================

from elasticsearch import AsyncElasticsearch

from src.core.config import get_settings

_es_client: AsyncElasticsearch | None = None


async def get_es_client() -> AsyncElasticsearch:
    """线程安全单例，懒加载"""
    global _es_client
    if _es_client is None:
        settings = get_settings()
        _es_client = AsyncElasticsearch(f"http://{settings.ES_HOST}:{settings.ES_PORT}")
    return _es_client


async def check_es_health() -> bool:
    try:
        es = await get_es_client()
        return await es.ping()
    except Exception:
        return False


async def close_es_client():
    global _es_client
    if _es_client:
        await _es_client.close()
        _es_client = None
