# ============================================================
# 对话上下文存储 —— 双后端（redis / memory）
#
# 解决旧实现的已知局限：进程内 dict 不跨 worker 共享、重启即失。
#   backend=redis（生产默认）：JSON 存 Redis，多实例共享、带 TTL 自动过期
#   backend=memory（开发降级）：进程内 dict + FIFO 淘汰
#
# ★ fail-open 定调：会话历史只影响「多轮追问能否理解上下文」，
#   读不到就退化成全新查询——绝不能因为它把用户的查询请求整个打挂。
#   Redis 故障时降级 memory 并记 warning（与本项目限流模块同一取向）。
#
# ★ 隔离语义（与旧实现一致）：key = {user_id}:{project_id}:{session_id}
#   session_id 是用户输入且默认 "default"，不掺 user_id 会让同项目所有
#   用户共享对话历史。
# ============================================================

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from src.core.config import get_settings
from src.core.logger import logger
from src.nl2sql.engine import ConversationContext

# 进程内兜底桶（memory 后端，以及 redis 故障降级时用）。
# ★ 硬上限：header 认证模式下 user_id 可伪造，无上限的话海量 distinct key
#   会把进程内存打爆。
_MEMORY_STORE: dict[str, ConversationContext] = {}
_MEMORY_STORE_CAP = 5000


def _ctx_key(user_id: str, project_id: str, session_id: str) -> str:
    return f"{user_id or 'anonymous'}:{project_id}:{session_id}"


def _redis_key(key: str) -> str:
    """加前缀，避免与限流等其他 Redis 用途的 key 空间混淆"""
    return f"chatbi:ctx:{key}"


def _evict_memory() -> None:
    """容量到了淘汰最早的一半（dict 保持插入序，FIFO 非精确 LRU：
    活跃会话重建历史即可，只为防内存膨胀，不做精细化）"""
    if len(_MEMORY_STORE) < _MEMORY_STORE_CAP:
        return
    for k in list(_MEMORY_STORE)[:_MEMORY_STORE_CAP // 2]:
        _MEMORY_STORE.pop(k, None)


def _memory_get(key: str) -> ConversationContext:
    """memory 后端 / Redis 故障降级：本地拿历史（读不到就是新会话）"""
    ctx = _MEMORY_STORE.get(key)
    if ctx is None:
        _evict_memory()
        ctx = _MEMORY_STORE[key] = ConversationContext()
    return ctx


async def get_context(user_id: str, project_id: str, session_id: str) -> ConversationContext:
    """读会话历史。任何异常都降级为空上下文（fail-open —— 退化成全新查询）"""
    key = _ctx_key(user_id, project_id, session_id)
    if get_settings().CONVERSATION_BACKEND != "redis":
        return _memory_get(key)

    try:
        from src.infra.redis_client import get_redis

        r = await get_redis()
        raw = await r.get(_redis_key(key))
        if not raw:
            # 新会话：建一个，但**不立刻写回**——空历史写进 Redis 等于给
            # 任意伪造 user_id 的请求都留一个 key，白占内存。
            # 真正的写入发生在第一次 add_turn。
            return ConversationContext()
        return ConversationContext.from_payload(json.loads(raw))
    except Exception as e:
        logger.warning(f"[ctx_store] Redis 读取失败，降级进程内历史: {e}")
        return _memory_get(key)


async def add_turn(user_id: str, project_id: str, session_id: str, result) -> None:
    """追加一轮对话。读改写一体，任何异常都吞掉（历史丢了不影响本次查询）"""
    key = _ctx_key(user_id, project_id, session_id)

    if get_settings().CONVERSATION_BACKEND != "redis":
        _memory_get(key).add(result)
        return

    try:
        from src.infra.redis_client import get_redis

        r = await get_redis()
        rkey = _redis_key(key)
        raw = await r.get(rkey)
        ctx = ConversationContext.from_payload(json.loads(raw)) if raw else ConversationContext()
        ctx.add(result)
        await r.set(
            rkey,
            json.dumps(ctx.to_payload(), ensure_ascii=False),
            ex=get_settings().CONVERSATION_MAX_AGE_SECONDS,
        )
        # 双写进程内：Redis 中途挂掉时降级读取还能拿到本次进程内的历史
        _evict_memory()
        _MEMORY_STORE[key] = ctx
    except Exception as e:
        logger.warning(f"[ctx_store] Redis 写入失败，仅存进程内: {e}")
        _memory_get(key).add(result)


async def get_history_payload(user_id: str, project_id: str, session_id: str) -> list[dict]:
    """给 /history 端点的只读视图（与 QueryResult 解耦，避免到处 import）"""
    ctx = await get_context(user_id, project_id, session_id)
    return ctx.to_payload()


async def clear(user_id: str, project_id: str, session_id: str) -> None:
    """清除会话历史（两个后端都清，避免降级路径残留旧数据）"""
    key = _ctx_key(user_id, project_id, session_id)
    _MEMORY_STORE.pop(key, None)

    if get_settings().CONVERSATION_BACKEND != "redis":
        return
    try:
        from src.infra.redis_client import get_redis

        r = await get_redis()
        await r.delete(_redis_key(key))
    except Exception as e:
        logger.warning(f"[ctx_store] Redis 删除失败（进程内已清）: {e}")


def clear_memory_store() -> None:
    """测试辅助：清空进程内桶"""
    _MEMORY_STORE.clear()


def memory_store_size() -> int:
    """测试辅助：当前进程内桶数量"""
    return len(_MEMORY_STORE)


# ════════════════════════════════════════════════════════════════════════
# 离线挖掘用：遍历全部会话历史（scripts/mine_badcases.py）
# ════════════════════════════════════════════════════════════════════════

async def iter_all_payloads() -> AsyncIterator[list[dict]]:
    """遍历全部会话的历史 payload。**只给离线脚本用，绝不在请求路径调用。**

    ★ 为什么必须放在这里而不是让脚本自己拼 key：`chatbi:ctx:{user}:{project}:{session}`
      是 ctx_store 的私有约定，散到外面必然随时间漂移（改了格式而不自知）。
    ★ 用 scan_iter 而非 keys()：KEYS 是 O(N) 阻塞命令，会把整个 Redis 卡住。
    ★ 返回的只是 payload，**不返回 key** —— user_id 可含 ":"，
      从 key 反解字段必然错位，调用方也不该拿到 user_id（不落库，见 Badcase 模型）。
    """
    import json

    if get_settings().CONVERSATION_BACKEND != "redis":
        # memory 后端下历史只在单个进程的 dict 里，独立脚本进程读不到
        return
    try:
        from src.infra.redis_client import get_redis

        r = await get_redis()
        async for raw_key in r.scan_iter(match=f"{_redis_key('*')}", count=200):
            raw = await r.get(raw_key)
            if not raw:  # scan 与 get 之间可能已过期
                continue
            try:
                yield json.loads(raw)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"[ctx_store] 历史脏数据跳过: {e}")
                continue
    except Exception as e:
        logger.warning(f"[ctx_store] 遍历会话历史失败: {e}")
        return
