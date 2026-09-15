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
    # ★ badcase 采集的收敛点：把各节点返回值并起来就是完整现场。
    #   为什么不放在 router 里逐个失败出口收：失败出口会随节点增减而漂移，
    #   而这里「每个节点的返回值都路过一次」，天然不会漏。
    trace: dict = {}

    try:
        async for update in graph.astream({"query": query}, context=ctx, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            for node_name, result in update.items():
                if node_name.startswith("__"):
                    continue
                if isinstance(result, dict):
                    trace.update(result)
                event_count += 1
                logger.info(f"[pipeline] event #{event_count}: {node_name}")
                yield {node_name: result}

        logger.info(f"[pipeline] complete, {event_count} events")
    finally:
        # 正常跑完、节点抛异常、客户端断连都会走到这里。
        # ★ safe_capture_pipeline 内部吞掉一切异常（含 CancelledError），
        #   采集失败绝不能影响已经发给用户的查询结果。
        try:
            from src.nl2sql.badcase_capture import safe_capture_pipeline

            await safe_capture_pipeline(ctx, trace)
        except Exception as e:  # noqa: BLE001 —— 最后一道兜底
            logger.warning(f"[badcase] 采集入口异常（不影响查询）: {e}")
