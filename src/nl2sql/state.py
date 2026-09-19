# ============================================================
# LangGraph State — 流水线节点间传递的数据
# ============================================================

from typing import TypedDict

from src.nl2sql.entities import ColumnInfo, TableInfo, MetricInfo, ValueInfo


class DateInfoState(TypedDict):
    date: str      # YYYY-MM-DD
    weekday: str   # e.g. "Thursday"
    quarter: str   # e.g. "Q3"
    # 时间锚点（P1）：预计算的相对周期边界，如 "上个月：2026-08-01 ~ 2026-08-31"
    anchors: list[str]


class DBInfoState(TypedDict):
    dialect: str   # e.g. "PostgreSQL"（从业务库连接推断，不再硬编码）
    version: str   # e.g. "16"（拿不到为空）
    sqlglot: str   # e.g. "postgres"（安全层解析/回写方言）


class DataAgentState(TypedDict, total=False):
    """NL2SQL 流水线状态"""
    query: str
    keywords: list[str]
    retrieved_columns: list[ColumnInfo]
    retrieved_values: list[ValueInfo]
    retrieved_metrics: list[MetricInfo]
    table_infos: list[TableInfo]
    metric_infos: list[MetricInfo]
    # few-shot 示例（P1 主线 B）：recall_examples 节点填充，generate_sql 注入 prompt
    few_shot_examples: list[dict]
    date_info: DateInfoState
    db_info: DBInfoState
    # 业务库各事实表时间列的真实上下界（add_context 探测，见 time_bounds.py）
    # ★ 用于两处：注入 prompt 让 LLM 知道"数据到哪天"；空结果时纠正摘要归因
    time_bounds: list
    sql: str
    error: str  # None 表示 SQL 校验通过，非空为错误信息
    # 已进行的 LLM 纠错轮数（correct_sql 累加），预算见 graph.MAX_SQL_FIX_ROUNDS
    sql_fix_rounds: int
    # 执行结果（由 execute_sql 节点填充，供 SSE consumer 读取）
    result_sql: str
    result_columns: list[str]
    result_data: list[dict]
    result_row_count: int
    result_summary: str
    result_error: str
