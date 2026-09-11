# ============================================================
# 图表推荐 — LLM 根据查询结果推荐图表类型与配置
# 渲染由前端 ECharts 完成（echarts_builder.to_echarts_option），
# 服务端不产出图片。
# ============================================================

from __future__ import annotations

import json

import pandas as pd
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from loguru import logger

from src.nl2sql.prompts import CHART_ADVISOR_PROMPT


async def recommend_chart(
    question: str,
    data: list[dict],
    columns: list[str],
    llm: BaseChatModel,
) -> dict:
    """LLM 推荐图表类型和配置，返回 {chart_type, title, x_column, y_column, ...}"""
    if not data:
        return {"chart_type": "table", "title": "无数据", "description": "查询结果为空"}

    df = pd.DataFrame(data)
    preview = df.head(5).to_string(index=False)

    prompt = CHART_ADVISOR_PROMPT.format(
        question=question,
        preview=preview,
        columns=columns,
        row_count=len(data),
    )

    response = await llm.ainvoke([SystemMessage(content=prompt)])
    content = response.content.strip()

    try:
        if "```" in content:
            content = content.split("```")[1].lstrip("json").strip()
        return json.loads(content)
    except Exception as e:
        logger.warning(f"图表推荐解析失败: {e}")
        return {"chart_type": "table", "title": "查询结果", "description": ""}
