# ============================================================
# NL2SQL 图定义 — 单一来源（Single Source of Truth）
#
# 节点注册、并行组、条件分支只在这里声明一次，两台执行器共用同一个
# 编译图，节点行为不会分叉：
#   执行器 A：pipeline.run_pipeline() — graph.astream 流式执行，
#             FastAPI SSE 逐节点推送。实测 astream 在 StreamingResponse
#             里不 hang（回归见 tests/test_langgraph_stream.py）；
#             LangGraph 1.x 真正的坑是不再注入节点第二位置参数（_with_ctx）
#   执行器 B：run_nl2sql_graph()      — graph.ainvoke 一次拿完整终态，
#             离线评测/批处理用
#
# 图结构：
#   extract_keywords
#     → [recall_columns | recall_values | recall_metrics]（并行）
#     → merge_info
#     → [filter_tables | filter_metrics]（并行）
#     → add_context → generate_sql → validate_sql
#     → error ? correct_sql → validate_sql（回环复检，预算 MAX_SQL_FIX_ROUNDS）: execute_sql
#     → END
#   ★ 纠错产物也是 LLM 输出，必须回安全校验（单语句/LIMIT 覆盖），不能直通执行
#
# 节点函数保持 (state, ctx) 签名不变；LangGraph 1.x 不再注入第二位置
# 参数，通过 _with_ctx 适配层从 runtime.context 取出请求级上下文传入。
# ============================================================

from collections.abc import Callable
from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from src.nl2sql.context import DataAgentContext
from src.nl2sql.nodes.add_context import add_context
from src.nl2sql.nodes.correct_sql import correct_sql
from src.nl2sql.nodes.execute_sql import execute_sql
from src.nl2sql.nodes.extract_keywords import extract_keywords
from src.nl2sql.nodes.filter_metrics import filter_metrics
from src.nl2sql.nodes.filter_tables import filter_tables
from src.nl2sql.nodes.generate_sql import generate_sql
from src.nl2sql.nodes.merge_info import merge_info
from src.nl2sql.nodes.recall_columns import recall_columns
from src.nl2sql.nodes.recall_metrics import recall_metrics
from src.nl2sql.nodes.recall_values import recall_values
from src.nl2sql.nodes.validate_sql import validate_sql
from src.nl2sql.state import DataAgentState

# SQL 校验失败后允许的 LLM 纠错轮数（每轮纠错后强制回 validate_sql 复检）
MAX_SQL_FIX_ROUNDS = 1


@dataclass(frozen=True)
class Stage:
    """一个执行阶段：组内节点并行（各自更新 state 的不同 key），组间串行。

    when: 门控谓词，返回 False 时整段跳过（无 error 不跑 correct_sql，
          error 未清不执行 execute_sql）。"""
    name: str
    nodes: tuple[str, ...]
    when: Callable[[DataAgentState], bool] | None = None


# 节点名 → 节点函数（统一签名 (state, ctx) -> partial state）
NODES: dict[str, Callable] = {
    "extract_keywords": extract_keywords,
    "recall_columns": recall_columns,
    "recall_values": recall_values,
    "recall_metrics": recall_metrics,
    "merge_info": merge_info,
    "filter_tables": filter_tables,
    "filter_metrics": filter_metrics,
    "add_context": add_context,
    "generate_sql": generate_sql,
    "validate_sql": validate_sql,
    "correct_sql": correct_sql,
    "execute_sql": execute_sql,
}


def _has_fix_budget(state: DataAgentState) -> bool:
    return state.get("sql_fix_rounds", 0) < MAX_SQL_FIX_ROUNDS


PIPELINE_SPEC: tuple[Stage, ...] = (
    Stage("extract_keywords", ("extract_keywords",)),
    Stage("parallel_recall", ("recall_columns", "recall_values", "recall_metrics")),
    Stage("merge_info", ("merge_info",)),
    Stage("parallel_filter", ("filter_tables", "filter_metrics")),
    Stage("add_context", ("add_context",)),
    Stage("generate_sql", ("generate_sql",)),
    Stage("validate_sql", ("validate_sql",)),
    Stage("correct_sql", ("correct_sql",),
          when=lambda s: bool(s.get("error")) and _has_fix_budget(s)),
    Stage("execute_sql", ("execute_sql",), when=lambda s: not s.get("error")),
)

STAGE_BY_NAME: dict[str, Stage] = {s.name: s for s in PIPELINE_SPEC}


# ════════════════════════════════════════════════════════════════
# 执行器 B：LangGraph 编译图（离线评测 / 批处理，一次拿完整终态）
# ════════════════════════════════════════════════════════════════

def _with_ctx(fn):
    """适配层：LangGraph 注入 runtime，节点拿 runtime.context 作为 ctx。

    未传 context（或空 dict）时 runtime.context 为 None，兜底为空 dict，
    节点内 ctx.get(...) 才安全。
    """

    async def node(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict:
        return await fn(state, runtime.context or {})

    node.__name__ = fn.__name__
    node.__doc__ = fn.__doc__
    return node


def _route_after_validate(state: DataAgentState) -> str:
    """validate_sql 后路由：合法 → 执行；可纠错 → correct_sql；
    纠错预算用尽仍不合法 → 直接结束（拒绝执行）"""
    if not state.get("error"):
        return "ok"
    return "correct" if _has_fix_budget(state) else "__end__"


def build_graph() -> StateGraph:
    builder = StateGraph(DataAgentState, context_schema=DataAgentContext)

    for name, fn in NODES.items():
        builder.add_node(name, _with_ctx(fn))

    builder.add_edge(START, "extract_keywords")

    # 多条出边 = 并行分支；多条入边 = 汇聚（等待分支全部完成）
    for name in ("recall_columns", "recall_values", "recall_metrics"):
        builder.add_edge("extract_keywords", name)
        builder.add_edge(name, "merge_info")

    for name in ("filter_tables", "filter_metrics"):
        builder.add_edge("merge_info", name)
        builder.add_edge(name, "add_context")

    builder.add_edge("add_context", "generate_sql")
    builder.add_edge("generate_sql", "validate_sql")
    builder.add_conditional_edges(
        "validate_sql",
        _route_after_validate,
        {"correct": "correct_sql", "ok": "execute_sql", "__end__": END},
    )
    # 纠错回环：修正后的 SQL 重新过安全校验
    builder.add_edge("correct_sql", "validate_sql")
    builder.add_edge("execute_sql", END)

    return builder.compile()


# 模块级编译一次（无 checkpointer、无 I/O），请求间共享
graph = build_graph()


async def run_nl2sql_graph(query: str, ctx: DataAgentContext) -> DataAgentState:
    """LangGraph 执行器入口：跑完整条流水线，返回最终 state（含执行结果或错误）。

    离线评测 / 批处理用；SSE 在线路径走 pipeline.run_pipeline。"""
    final = await graph.ainvoke({"query": query}, context=ctx)
    return final
