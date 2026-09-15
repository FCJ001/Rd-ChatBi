# ============================================================
# API 限流 — 按 user_id 滑动窗口
#
# backend=redis：ZSET 跨 worker 计数（生产多实例必须用这个）；
# backend=memory：进程内 deque，仅单 worker 开发用。
# Redis 不可用时降级 memory（fail-open：限流是成本防护，不应因它拒绝服务，
# 但降级会记 warning，靠告警发现）。
#
# 接入：router 的查询端点挂 Depends(enforce_rate_limit)。
# ============================================================

import time
import uuid
from collections import deque

from fastapi import Depends, HTTPException, Request

from src.core.config import get_settings
from src.core.deps import UserContext, get_current_user
from src.core.logger import logger
from src.core.metrics import RATE_LIMIT_REJECTED

# memory 后端的桶：{user_key: deque[timestamp]}（perf_counter 单调时钟）
_memory_buckets: dict[str, deque] = {}
_MEMORY_BUCKET_CAP = 100_000  # 防桶本身无限增长（异常流量下 memory 后端的自保）


def _check_memory(key: str, window: int, max_requests: int) -> bool:
    """进程内滑动窗口。返回 True=放行。"""
    now = time.perf_counter()
    bucket = _memory_buckets.get(key)
    if bucket is None:
        if len(_memory_buckets) >= _MEMORY_BUCKET_CAP:
            _evict_memory_buckets(now, window)
        bucket = _memory_buckets.setdefault(key, deque())
    while bucket and bucket[0] <= now - window:
        bucket.popleft()
    if len(bucket) >= max_requests:
        return False
    bucket.append(now)
    return True


def _evict_memory_buckets(now: float, window: int) -> None:
    """桶数到达上限时先清已过期条目；仍满（大量活跃桶）才整体重置。
    直接 clear() 会让攻击者用海量伪造 user_id 把所有人的限流计数清零。"""
    horizon = now - window
    for k in list(_memory_buckets):
        b = _memory_buckets[k]
        if not b or b[-1] <= horizon:
            del _memory_buckets[k]
    if len(_memory_buckets) >= _MEMORY_BUCKET_CAP:
        _memory_buckets.clear()


async def _check_redis(key: str, window: int, max_requests: int) -> bool | None:
    """Redis ZSET 滑动窗口（跨 worker）。返回 True=放行，None=Redis 不可用需降级。

    ★ zadd 先于 zcard、同在一个 MULTI/EXEC（transaction=True 不被其他命令
    交错）：旧实现先 zcard 再 zadd，并发突发下 N 个请求都看到旧计数、
    集体放行超限。新顺序下被拒请求也会占一个窗口内名额（其 member 到期
    自然淘汰），持续过载时保持拒绝语义，不会越限越松。"""
    from src.infra.redis_client import get_redis

    try:
        r = await get_redis()
        now = time.time()
        # member 用 ns+uuid 保证唯一（同一秒多个请求都要占位）
        member = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        async with r.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zadd(key, {member: now})
            pipe.zcard(key)
            pipe.expire(key, window)
            res = await pipe.execute()
        return int(res[2]) <= max_requests
    except Exception as e:
        logger.warning(f"[rate_limit] Redis 不可用，降级 memory 后端: {e}")
        return None


async def enforce_rate_limit(
    request: Request,
    user: UserContext = Depends(get_current_user),
) -> None:
    """FastAPI 依赖：查询端点挂上即可生效；超限抛 HTTP 429。"""
    settings = get_settings()
    if not settings.RATE_LIMIT_ENABLED:
        return

    # 匿名请求（无 user_id）共用一个桶，仍有总量兜底
    key = f"rate_limit:{user.user_id or 'anonymous'}"
    window = settings.RATE_LIMIT_WINDOW_SECONDS
    max_requests = settings.RATE_LIMIT_MAX_REQUESTS

    if settings.RATE_LIMIT_BACKEND == "redis":
        allowed = await _check_redis(key, window, max_requests)
    else:
        allowed = None
    if allowed is None:
        allowed = _check_memory(key, window, max_requests)

    if not allowed:
        RATE_LIMIT_REJECTED.labels(endpoint=request.url.path).inc()
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
