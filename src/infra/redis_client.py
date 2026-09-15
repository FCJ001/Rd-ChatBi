# ============================================================
# Redis 异步客户端 — 懒加载单例（限流等跨 worker 状态用）
# ============================================================

import threading

from redis import asyncio as aioredis

from src.core.config import get_settings

_client: aioredis.Redis | None = None
_lock = threading.Lock()


async def get_redis() -> aioredis.Redis:
    """进程级单例。Redis 宕机时由调用方决定降级策略（限流 fail-open）。"""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = aioredis.from_url(
                    get_settings().REDIS_URL,
                    decode_responses=True,
                )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
