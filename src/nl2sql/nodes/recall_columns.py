# ============================================================
# Node ②a — LLM 扩展关键词 + Milvus 向量检索相关列
# ============================================================

import asyncio
import json

from langchain_core.messages import HumanMessage, SystemMessage

from src.nl2sql.state import DataAgentState
from src.nl2sql.context import DataAgentContext
from src.nl2sql.llm_text import safe_ainvoke
from src.nl2sql.prompt_loader import load_prompt
from src.core.metrics import RETRIEVAL_REQUESTS


async def recall_columns(state: DataAgentState, ctx: DataAgentContext) -> dict:
    """LLM 扩展关键词 → 向量化 → Milvus 检索列"""
    writer = ctx.get("writer")
    if writer:
        writer({"type": "progress", "step": "召回字段", "status": "running"})

    try:
        llm = ctx["llm"]
        embedding_model = ctx["embedding_model"]
        repo = ctx["milvus_column_repo"]
        keywords = state["keywords"]

        # LLM 扩展关键词
        response = await safe_ainvoke(llm, [
            SystemMessage(content=load_prompt("extend_keywords_for_column_recall")),
            HumanMessage(content=state["query"]),
        ])
        try:
            extra_keywords = json.loads(response.content.strip())
            if not isinstance(extra_keywords, list):
                extra_keywords = []
        except json.JSONDecodeError:
            extra_keywords = []

        # 合并关键词
        all_keywords = list(dict.fromkeys(keywords + extra_keywords))

        # 向量检索：批量向量化（一次 HTTP 而不是每关键词一次）；
        # ★ aembed_* 走 executor、MilvusClient 是同步 SDK 用 to_thread 包一层 ——
        #   直接同步调用会阻塞整个事件循环，并发下所有请求互相卡
        # ★ 向量化失败会让本路召回整个归零，但异常往上抛会变成节点 error、
        #   前端只看到"召回字段"失败 —— 打点区分「embedding 服务挂了」和
        #   「正常召回但没命中」，否则两者在监控上长得一样。
        try:
            vecs = await embedding_model.aembed_documents(all_keywords)
        except Exception:
            RETRIEVAL_REQUESTS.labels(channel="columns", status="failed").inc()
            raise
        retrieved: dict[str, any] = {}
        search_failures = 0
        for kw, vec in zip(all_keywords, vecs):
            try:
                cols = await asyncio.to_thread(repo.search, vec, top_k=5, threshold=0.6)
                for c in cols:
                    if c.id not in retrieved:
                        retrieved[c.id] = c
            except Exception:
                search_failures += 1
                continue
        if search_failures:
            # Milvus 单次检索失败是「部分降级」：其余关键词仍可能召回
            RETRIEVAL_REQUESTS.labels(channel="columns", status="failed").inc()
        RETRIEVAL_REQUESTS.labels(
            channel="columns", status="ok" if retrieved else "empty").inc()

        from src.core.logger import logger
        logger.info(f"[recall_columns] 扩展关键词={extra_keywords}, 向量检索命中 {len(retrieved)} 列")

        if writer:
            writer({"type": "progress", "step": "召回字段", "status": "success"})
        return {"retrieved_columns": list(retrieved.values())}
    except Exception as e:
        if writer:
            writer({"type": "progress", "step": "召回字段", "status": "error"})
        raise
