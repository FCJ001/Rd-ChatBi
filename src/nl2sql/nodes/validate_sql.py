# ============================================================
# Node ⑦ — EXPLAIN 校验 SQL
# ============================================================

from sqlalchemy import text

from src.nl2sql.state import DataAgentState
from src.nl2sql.context import DataAgentContext


async def validate_sql(state: DataAgentState, ctx: DataAgentContext) -> dict:
    """在数据库上执行 EXPLAIN 校验 SQL"""
    writer = ctx.get("writer")
    if writer:
        writer({"type": "progress", "step": "验证SQL", "status": "running"})

    sql = state["sql"]
    db = ctx["dw_db_session"]

    # 安全规则校验：返回的 validated_sql 是 AST 重写后的语句
    # （LIMIT 强制覆盖、尾部注释剥离），必须回写 state，让下游执行它而不是原始 SQL
    # sensitive_columns：数据源敏感列文本拦截（SELECT * 由 execute_sql 节点结果列过滤兜底）
    from src.nl2sql.security import validate_sql as security_check
    valid, validated = security_check(
        sql, sensitive_columns=ctx.get("sensitive_columns"),
    )
    if not valid:
        if writer:
            writer({"type": "progress", "step": "验证SQL", "status": "error"})
        return {"error": validated}

    try:
        await db.execute(text(f"EXPLAIN {validated}"))

        from src.core.logger import logger
        logger.info("[validate_sql] EXPLAIN 校验通过")

        if writer:
            writer({"type": "progress", "step": "验证SQL", "status": "success"})
        return {"error": None, "sql": validated}
    except Exception as e:
        from src.core.logger import logger
        logger.warning(f"[validate_sql] EXPLAIN 失败: {e}")

        if writer:
            writer({"type": "progress", "step": "验证SQL", "status": "error"})
        return {"error": str(e)}
