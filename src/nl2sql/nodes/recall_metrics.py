# ============================================================
# Node ②c — LLM 扩展关键词 + Milvus 向量检索相关指标
# ============================================================

import asyncio
import json

from langchain_core.messages import HumanMessage, SystemMessage

from src.nl2sql.state import DataAgentState
from src.nl2sql.context import DataAgentContext
from src.nl2sql.llm_text import safe_ainvoke
from src.nl2sql.prompt_loader import load_prompt


async def recall_metrics(state: DataAgentState, ctx: DataAgentContext) -> dict:
    """LLM 扩展关键词 → 向量化 → Milvus 检索指标"""
    writer = ctx.get("writer")
    if writer:
        writer({"type": "progress", "step": "召回指标", "status": "running"})

    try:
        llm = ctx["llm"]
        embedding_model = ctx["embedding_model"]
        repo = ctx["milvus_metric_repo"]
        keywords = state["keywords"]

        # LLM 扩展关键词
        response = await safe_ainvoke(llm, [
            SystemMessage(content=load_prompt("extend_keywords_for_metric_recall")),
            HumanMessage(content=state["query"]),
        ])
        try:
            extra_keywords = json.loads(response.content.strip())
            if not isinstance(extra_keywords, list):
                extra_keywords = []
        except json.JSONDecodeError:
            extra_keywords = []

        all_keywords = list(dict.fromkeys(keywords + extra_keywords))

        # 向量检索：批量向量化（一次 HTTP 而不是每关键词一次）；
        # ★ aembed_* 走 executor、MilvusClient 是同步 SDK 用 to_thread 包一层 ——
        #   直接同步调用会阻塞整个事件循环，并发下所有请求互相卡
        vecs = await embedding_model.aembed_documents(all_keywords)
        retrieved: dict[str, any] = {}
        for kw, vec in zip(all_keywords, vecs):
            try:
                metrics = await asyncio.to_thread(repo.search, vec, top_k=5, threshold=0.6)
                for m in metrics:
                    if m.id not in retrieved:
                        retrieved[m.id] = m
            except Exception:
                continue

        from src.core.logger import logger
        logger.info(f"[recall_metrics] 扩展关键词={extra_keywords}, 向量检索命中 {len(retrieved)} 个指标")

        if writer:
            writer({"type": "progress", "step": "召回指标", "status": "success"})
        return {"retrieved_metrics": list(retrieved.values())}
    except Exception as e:
        if writer:
            writer({"type": "progress", "step": "召回指标", "status": "error"})
        raise
