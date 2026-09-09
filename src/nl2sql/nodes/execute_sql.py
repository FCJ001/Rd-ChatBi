# ============================================================
# Node ⑨ — 执行 SQL + 返回结果
# ============================================================

from sqlalchemy import text

from src.nl2sql.engine import generate_summary, setup_readonly_session
from src.nl2sql.state import DataAgentState
from src.nl2sql.context import DataAgentContext


async def execute_sql(state: DataAgentState, ctx: DataAgentContext) -> dict:
    """执行 SQL 并返回结果"""
    writer = ctx.get("writer")
    if writer:
        writer({"type": "progress", "step": "执行SQL", "status": "running"})

    sql = state["sql"]
    db = ctx["dw_db_session"]
    llm = ctx["llm"]

    # 角色行级过滤 —— 规则来自 bi_datasources.role_rules（按项目配置，数据驱动）
    from src.nl2sql.security import apply_role_filter
    allowed, filtered_sql = apply_role_filter(
        sql,
        ctx.get("role", "patient"),
        role_rules=ctx.get("role_rules"),
        params={
            "dept_id": ctx.get("dept_id"),
            "owner_domain_id": ctx.get("owner_domain_id"),
            "business_line": ctx.get("business_line"),
        },
    )

    columns, rows, summary, error = [], [], "", ""
    if not allowed:
        error = filtered_sql  # 如 customer 角色直接拒绝
    else:
        try:
            await setup_readonly_session(db)
            result = await db.execute(text(filtered_sql))
            columns = list(result.keys())
            rows = [dict(row) for row in result.mappings().all()]
            source_name = ctx.get("source_name") or "业务数据库"
            summary = await generate_summary(state["query"], rows, llm, source_name)
        except Exception as e:
            columns, rows, summary, error = [], [], "", str(e)

    from src.core.logger import logger

    if error:
        logger.error(f"[execute_sql] 执行失败: {error}")
        if writer:
            writer({"type": "progress", "step": "执行SQL", "status": "error"})
    else:
        logger.info(f"[execute_sql] 返回 {len(rows)} 行, {len(columns)} 列")
        if writer:
            writer({"type": "progress", "step": "执行SQL", "status": "success"})
            writer({"type": "result", "data": {
                "sql": filtered_sql,
                "columns": columns,
                "data": rows,
                "row_count": len(rows),
                "summary": summary,
            }})

    return {
        "error": error,
        "result_sql": filtered_sql,
        "result_columns": columns,
        "result_data": rows,
        "result_row_count": len(rows),
        "result_summary": summary,
        "result_error": error or "",
    }
