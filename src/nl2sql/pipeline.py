# ============================================================
# NL2SQL 流水线入口 — LangGraph astream 驱动，async generator 流式返回
#
# 图结构与节点注册见 graph.py；本模块只做流式封装：
# astream(stream_mode="updates") 每个节点完成即产出 {节点名: 返回值}
# 事件（并行分支各自独立出事件），与 SSE 消费端契约保持一致。
#
# astream 可直接交给 FastAPI StreamingResponse 消费（astream 是普通
# async generator，在请求事件循环上迭代，无额外线程/循环切换）；
# 这里保留 router 的 queue 桥接以透传节点内的 writer 进度事件。
# ============================================================

from src.nl2sql.context import DataAgentContext
from src.nl2sql.graph import graph


async def run_pipeline(query: str, ctx: DataAgentContext):
    """流式执行 NL2SQL 流水线，逐节点产出 {node_name: result} 事件。"""
    from src.core.logger import logger

    logger.info(f"[pipeline] starting for query: {query[:80]}")
    event_count = 0

    async for update in graph.astream({"query": query}, context=ctx, stream_mode="updates"):
        if not isinstance(update, dict):
            continue
        for node_name, result in update.items():
            if node_name.startswith("__"):
                continue
            event_count += 1
            logger.info(f"[pipeline] event #{event_count}: {node_name}")
            yield {node_name: result}

    logger.info(f"[pipeline] complete, {event_count} events")
