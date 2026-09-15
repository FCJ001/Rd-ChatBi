# ============================================================
# Node ⑨ — 执行 SQL + 返回结果
# ============================================================

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.nl2sql.engine import SQL_TIMEOUT, generate_summary, setup_readonly_session
from src.nl2sql.security import filter_result_columns
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
            # 执行层防线：SELECT * 能穿过文本校验，敏感列在这里从结果集剔除
            # （必须在 generate_summary 之前，否则敏感数据仍会进 LLM prompt）
            columns, rows = filter_result_columns(
                columns, rows, ctx.get("sensitive_columns"),
            )
            source_name = ctx.get("source_name") or "业务数据库"
            summary = await generate_summary(state["query"], rows, llm, source_name)
        except DBAPIError as e:
            # 与旧引擎 run_query 对齐：超时归类为友好提示，其余不透出原始
            # 数据库错误文本（可能暴露表结构），详情只进日志
            from src.core.logger import logger
            await db.rollback()
            if "canceling statement" in str(e) or "timeout" in str(e).lower():
                error = f"查询超时（{SQL_TIMEOUT}秒），请缩小查询范围"
            else:
                error = "数据库执行失败，请调整问题后重试"
            logger.warning(f"[execute_sql] DBAPIError: {e}")
            columns, rows, summary = [], [], ""
        except Exception as e:
            from src.core.logger import logger
            await db.rollback()
            error = "查询执行失败，请调整问题后重试"
            logger.warning(f"[execute_sql] 执行异常: {e}")
            columns, rows, summary = [], [], ""

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
        # ★ 被角色规则拒绝时，apply_role_filter 返回的第二个值是**拒绝原因**
        #   而不是 SQL（见 security.apply_role_filter）。直接把它当 result_sql
        #   会把「当前角色 patient 无数据查询权限」这句中文写成「生成的 SQL」——
        #   前端 SQL 段显示错误文案，badcase 回流也会存下这条垃圾预测。
        #   这种时候模型确实生成过 SQL，而它就在 state 里，用它。
        "result_sql": sql if not allowed else filtered_sql,
        "result_columns": columns,
        "result_data": rows,
        "result_row_count": len(rows),
        "result_summary": summary,
        "result_error": error or "",
    }
