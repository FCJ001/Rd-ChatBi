# ============================================================
# Node ②b — ES 检索列的**真实枚举值**（告诉模型合法取值集合）
# ============================================================

import json

from langchain_core.messages import HumanMessage, SystemMessage

from src.nl2sql.state import DataAgentState
from src.nl2sql.context import DataAgentContext
from src.nl2sql.llm_text import safe_ainvoke
from src.nl2sql.prompt_loader import load_prompt


async def recall_values(state: DataAgentState, ctx: DataAgentContext) -> dict:
    """LLM 扩展关键词 → ES 全文检索列值枚举"""
    writer = ctx.get("writer")
    if writer:
        writer({"type": "progress", "step": "召回字段取值", "status": "running"})

    try:
        llm = ctx["llm"]
        repo = ctx["es_value_repo"]
        keywords = state["keywords"]

        # LLM 扩展关键词
        response = await safe_ainvoke(llm, [
            SystemMessage(content=load_prompt("extend_keywords_for_value_recall")),
            HumanMessage(content=state["query"]),
        ])
        try:
            extra_keywords = json.loads(response.content.strip())
            if not isinstance(extra_keywords, list):
                extra_keywords = []
        except json.JSONDecodeError:
            extra_keywords = []

        all_keywords = list(dict.fromkeys(keywords + extra_keywords))

        # ES 检索。两类记录都会命中，各自有用途：
        #   source="alias" —— YAML 手写的同义词。它负责「用户的话 → 找到这一列」，
        #     例：库里 status 存英文 'paid'，用户说「已缴费」，靠 alias 才能命中这列。
        #   source="db"   —— 业务库里的**真实取值**，可直接写进 WHERE。
        # 两者由 merge_info 分别标注后挂到列的 examples 上，模型按标注区分使用。
        retrieved: dict[str, any] = {}
        for kw in all_keywords:
            try:
                values = await repo.search(kw, size=10)
                for v in values:
                    if v.id not in retrieved:
                        retrieved[v.id] = v
            except Exception:
                continue

        from src.core.logger import logger
        logger.info(f"[recall_values] 扩展关键词={extra_keywords}, ES 命中 {len(retrieved)} 条真实枚举值")

        if writer:
            writer({"type": "progress", "step": "召回字段取值", "status": "success"})
        return {"retrieved_values": list(retrieved.values())}
    except Exception as e:
        if writer:
            writer({"type": "progress", "step": "召回字段取值", "status": "error"})
        raise
