# ============================================================
# Node ⑧ — LLM 纠错 SQL
# ============================================================

import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from src.nl2sql.llm_text import safe_ainvoke, strip_code_fence
from src.nl2sql.state import DataAgentState
from src.nl2sql.context import DataAgentContext
from src.nl2sql.prompt_loader import load_prompt


async def correct_sql(state: DataAgentState, ctx: DataAgentContext) -> dict:
    """根据数据库报错信息，LLM 修正 SQL"""
    writer = ctx.get("writer")
    if writer:
        writer({"type": "progress", "step": "校正SQL", "status": "running"})

    try:
        llm = ctx["llm"]

        # 序列化上下文
        tables_list = []
        for t in state["table_infos"]:
            cols = [{"name": c.name, "type": c.type, "role": c.role,
                     "description": c.description} for c in t.columns]
            tables_list.append({"name": t.name, "role": t.role, "columns": cols})
        table_infos_str = yaml.dump(tables_list, allow_unicode=True, default_flow_style=False)

        metrics_list = [{"name": m.name, "description": m.description} for m in state["metric_infos"]]
        metric_infos_str = yaml.dump(metrics_list, allow_unicode=True, default_flow_style=False)

        # 加载并填充 prompt
        # ★ 结构化内容先进，用户输入（query）最后填充：str.replace 是顺序替换，
        #   query 里若含 "{sql}"/"{error}" 字面量，先填会被后续 replace 二次替换
        system_prompt = load_prompt("correct_sql")
        system_prompt = system_prompt.replace("{table_infos}", table_infos_str)
        system_prompt = system_prompt.replace("{metric_infos}", metric_infos_str)
        system_prompt = system_prompt.replace("{sql}", state["sql"])
        system_prompt = system_prompt.replace("{error}", state.get("error", ""))
        system_prompt = system_prompt.replace("{query}", state["query"])

        response = await safe_ainvoke(llm, [
            SystemMessage(content=system_prompt),
            HumanMessage(content="请根据错误信息修正上述 SQL"),
        ])

        sql = strip_code_fence(response.content)

        from src.core.logger import logger
        logger.info(f"[correct_sql] 纠错后 SQL ({len(sql)} 字符): {sql[:200]}")

        if writer:
            writer({"type": "progress", "step": "校正SQL", "status": "success"})
        # sql_fix_rounds 供图路由判断纠错预算（用尽仍报错则拒绝执行）
        return {"sql": sql, "error": None,
                "sql_fix_rounds": state.get("sql_fix_rounds", 0) + 1}
    except Exception as e:
        if writer:
            writer({"type": "progress", "step": "校正SQL", "status": "error"})
        raise
