# ============================================================
# Node ⑤ — 注入日期上下文 + 数据库方言
#
# P1 升级①：时间锚点 —— 相对周期（上月/上周/近30天…）边界由 Python
#   预计算注入，LLM 只抄不算。防"NOW()-interval 当自然月"badcase。
# P0 升级②：dialect 从业务库连接推断，去掉硬编码 PostgreSQL/16。
# ============================================================

from datetime import datetime

from src.nl2sql.state import DataAgentState
from src.nl2sql.context import DataAgentContext
from src.nl2sql.time_anchors import build_time_anchors

# SQLAlchemy dialect.name → 展示名（新数据源类型在此登记）
_DIALECT_DISPLAY = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "duckdb": "DuckDB",
    "sqlite": "SQLite",
    "oracle": "Oracle",
    "mssql": "SQL Server",
}

# SQLAlchemy dialect.name → sqlglot 方言名（security.validate_sql 解析/回写用）
_SQLGLOT_DIALECT = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "duckdb": "duckdb",
    "sqlite": "sqlite",
    "oracle": "oracle",
    "mssql": "tsql",
}


def detect_db_info(ctx: DataAgentContext) -> dict:
    """从业务库会话的 bind 推断方言/版本；推断失败回退 PostgreSQL 不阻塞。

    返回三件套：dialect（展示名，进 prompt）、version、sqlglot（安全层解析方言）。"""
    dialect, version, sqlglot = "PostgreSQL", "", "postgres"
    try:
        bind = ctx.get("dw_db_session").bind
        name = getattr(bind.dialect, "name", "postgresql")
        dialect = _DIALECT_DISPLAY.get(name, name)
        sqlglot = _SQLGLOT_DIALECT.get(name, name)
        info = getattr(bind.dialect, "server_version_info", None)
        if info:
            version = str(info[0])
    except Exception:
        pass
    return {"dialect": dialect, "version": version, "sqlglot": sqlglot}


async def add_context(state: DataAgentState, ctx: DataAgentContext) -> dict:
    """注入当前日期 + 时间锚点 + 数据库方言 + 数据时间上界"""
    writer = ctx.get("writer")
    if writer:
        writer({"type": "progress", "step": "添加额外上下文信息", "status": "running"})

    try:
        now = datetime.now()
        quarter = f"Q{(now.month - 1) // 3 + 1}"
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        date_info = {
            "date": now.strftime("%Y-%m-%d"),
            "weekday": weekdays[now.weekday()],
            "quarter": quarter,
            "anchors": [f"{name}：{span}" for name, span in build_time_anchors(now).items()],
        }
        db_info = detect_db_info(ctx)
        bounds = await _detect_bounds(ctx)

        from src.core.logger import logger
        logger.info(
            f"[add_context] 日期={date_info['date']} {date_info['weekday']} {quarter}, "
            f"数据库={db_info['dialect']} {db_info['version']}, "
            f"锚点={len(date_info['anchors'])}条, 数据上界={len(bounds)}列"
        )

        if writer:
            writer({"type": "progress", "step": "添加额外上下文信息", "status": "success"})
        return {"date_info": date_info, "db_info": db_info, "time_bounds": bounds}
    except Exception as e:
        if writer:
            writer({"type": "progress", "step": "添加额外上下文信息", "status": "error"})
        raise


async def _detect_bounds(ctx: DataAgentContext) -> list:
    """探测业务库各事实表的时间上下界。

    ★ 探测失败一律退化成空列表，绝不让它影响查询本身 —— 这只是给 prompt
      补充的一条上下文，不是关键路径。
    ★ 表名从元数据仓取（表名是元数据的一部分），逐表探测有 N 次往返，
      所以只取事实表前缀的表（见 time_bounds._FACT_PREFIXES）并设上限。
    """
    from src.core.logger import logger
    from src.nl2sql.time_bounds import detect_time_bounds

    db = ctx.get("dw_db_session")
    meta_repo = ctx.get("pg_meta_repo")
    if db is None or meta_repo is None:
        return []
    try:
        tables = [t.name for t in await meta_repo.get_all_tables()]
    except Exception as e:
        logger.debug(f"[add_context] 取表名失败，跳过数据上界探测: {e}")
        return []
    try:
        return await detect_time_bounds(db, tables)
    except Exception as e:
        logger.warning(f"[add_context] 数据上界探测失败（不影响查询）: {e}")
        return []
