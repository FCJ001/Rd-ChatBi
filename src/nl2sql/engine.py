# ============================================================
# NL2SQL 核心引擎（一次性 JSON 响应路径）
# 多轮下钻 / 旧接口走这里；9 阶段流水线走 nl2sql/pipeline.py
# ============================================================

from __future__ import annotations

import json
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.nl2sql.llm_text import safe_ainvoke, strip_code_fence, strip_value_labels
from src.nl2sql.prompts import (
    FOLLOWUP_PROMPT,
    NL2SQL_SYSTEM_PROMPT,
    REWRITE_PROMPT,
    SCHEMA_PROMPT,
    SUMMARY_PROMPT,
)
from src.nl2sql.security import (
    apply_role_filter,
    filter_result_columns,
    validate_sql,
)
from src.nl2sql.time_anchors import format_anchor_block
from src.nl2sql.example_store import format_examples_block


def build_schema_prompt(tables, dialect: str = "PostgreSQL") -> str:
    """从元数据动态生成 SCHEMA 描述（多数据源：不再硬编码单项目表结构）。

    tables: list[TableInfo]（来自 PgMetaRepository.get_all_tables()）
    dialect: 从业务库连接推断的方言展示名（engine.run_query 内自动探测后传入）
    """
    lines = [f"## 数据库表结构（{dialect}）"]
    for t in tables:
        desc = f" -- {t.description}" if t.description else ""
        lines.append(f"{t.name}（{t.role}）{desc}:")
        for c in t.columns:
            alias = f"，别名: {', '.join(c.alias)}" if c.alias else ""
            ex = f"，示例: {c.examples[:3]}" if c.examples else ""
            lines.append(f"  {c.name} {c.type} -- {c.description or c.role}{alias}{ex}")
        lines.append("")
    return "\n".join(lines)

MAX_RETRIES = 2
SQL_TIMEOUT = 10


@dataclass
class QueryResult:
    question: str
    sql: str = ""
    data: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    summary: str = ""
    error: str = ""
    success: bool = True


MAX_HISTORY_TURNS = 10


@dataclass
class ConversationContext:
    """单会话的对话历史（进程内形态）。

    ★ 只承载「最近 10 轮」的轻量摘要，不存 result.data —— 每轮最多 100 行
      查询数据，10 轮就是上千行；多轮改写只用到 question/sql/summary，
      存下来纯属浪费内存/带宽（见 CONVERSATION_MAX_AGE_SECONDS 注释）。
    """
    history: list[QueryResult] = field(default_factory=list)

    @property
    def last_result(self) -> QueryResult | None:
        return self.history[-1] if self.history else None

    def add(self, result: QueryResult):
        self.history.append(result)
        if len(self.history) > MAX_HISTORY_TURNS:
            self.history = self.history[-MAX_HISTORY_TURNS:]

    # ── 序列化（Redis backend 用）──────────────────────────────
    def to_payload(self) -> list[dict]:
        """转成 JSON 可存的列表。data/columns 不存（见类 docstring）。"""
        return [
            {
                "question": r.question,
                "sql": r.sql,
                "row_count": r.row_count,
                "summary": r.summary or "",
                "success": r.success,
                "error": r.error or "",
            }
            for r in self.history
        ]

    @classmethod
    def from_payload(cls, payload: list[dict] | None) -> "ConversationContext":
        """从 Redis 读回。字段缺失/类型异常一律降级为默认值 ——
        历史读不出来只该退化成「全新查询」，不该让整个请求 500。"""
        ctx = cls()
        if not isinstance(payload, list):
            return ctx
        for item in payload[-MAX_HISTORY_TURNS:]:
            if not isinstance(item, dict):
                continue
            ctx.history.append(QueryResult(
                question=str(item.get("question") or ""),
                sql=str(item.get("sql") or ""),
                row_count=int(item.get("row_count") or 0),
                summary=str(item.get("summary") or ""),
                error=str(item.get("error") or ""),
                success=bool(item.get("success", True)),
            ))
        return ctx


async def generate_sql(
    question: str,
    llm: BaseChatModel,
    context: ConversationContext | None = None,
    error_hint: str = "",
    schema: str = "",
    examples: list[dict] | None = None,
    dialect: str = "PostgreSQL",
) -> str:
    """LLM 生成 SQL（schema 由调用方按数据源动态生成，默认用内置 SCHEMA_PROMPT）

    P1：prompt 永远附加时间锚点（防 NOW()-interval 当自然月）；
        examples 非空时附加 few-shot 相似问答对。
    dialect：业务库方言展示名（system prompt 措辞跟着数据源走，不再写死 PostgreSQL）。"""
    schema = schema or SCHEMA_PROMPT
    if context and context.last_result and context.last_result.success:
        prompt = FOLLOWUP_PROMPT.format(
            previous_sql=context.last_result.sql,
            previous_summary=context.last_result.summary[:500],
            question=question,
            schema=schema,
        )
    else:
        prompt = NL2SQL_SYSTEM_PROMPT.format(schema=schema, dialect=dialect)

    prompt = prompt + "\n\n" + format_anchor_block()
    if examples:
        prompt = prompt + "\n\n" + format_examples_block(examples)

    messages = [SystemMessage(content=prompt)]
    if error_hint:
        messages.append(HumanMessage(
            content=f"上一轮 SQL 报错：{error_hint}\n请修正。\n\n{question}"
        ))
    else:
        messages.append(HumanMessage(content=question))

    response = await safe_ainvoke(llm, messages)
    return strip_value_labels(strip_code_fence(response.content))


async def setup_readonly_session(db: AsyncSession) -> None:
    """执行前设置会话安全属性：超时 + 只读（第四层防线）。

    供旧引擎与流水线 execute_sql 节点共用，保证两条路径行为一致。"""
    await db.execute(text(f"SET LOCAL statement_timeout = '{SQL_TIMEOUT * 1000}'"))
    await db.execute(text("SET LOCAL default_transaction_read_only = on"))


async def execute_sql(sql: str, db: AsyncSession) -> tuple[list[dict], list[str]]:
    """执行 SQL，返回 (rows, columns)"""
    await setup_readonly_session(db)
    result = await db.execute(text(sql))
    columns = list(result.keys())
    rows = [dict(row) for row in result.mappings().all()]
    return rows, columns


async def generate_summary(
    question: str, data: list[dict], llm: BaseChatModel, source_name: str = "业务数据库",
    extra_note: str = "",
) -> str:
    """LLM 生成数据摘要（source_name 标注当前数据源，多数据源不再硬编码）。

    extra_note：代码判定出的事实补充（见 time_bounds.beyond_data_upper_bound）。
    ★ 实测缺陷：问「这个月17号」时数据上界是 09-16，摘要却写成「销量归零、
      断崖式下滑」。原因是摘要模型**不知道数据到哪天为止** —— 把"查不到"
      当成了"业务为零"。这里把上界事实告诉它，让它能给出正确归因。
      只在结果为空时才需要，非空结果不传（省 token，也避免干扰正常叙述）。
    """
    result_str = json.dumps(data[:20], ensure_ascii=False, default=str)
    prompt = SUMMARY_PROMPT.format(
        question=question, result=result_str, source_name=source_name,
    ) + (extra_note or "")
    response = await safe_ainvoke(llm, [SystemMessage(content=prompt)])
    return response.content


async def _empty_result_note(db, data: list[dict], role_filter, role_rules, params,
                              sql: str = "") -> str:
    """结果异常时，给摘要模型的事实补充。

    ★ 只做**代码可判定**的归因，不写"请你自己注意"式的提示 —— 实测后者
      无效：模型仍先给结论、后补免责声明。
    ★ 上界探测失败就返回空串：宁可不说，也不能编一个上界误导摘要。
    """
    from src.nl2sql.time_bounds import beyond_data_upper_bound, detect_time_bounds, looks_empty

    # ① 行级权限过滤导致的空 —— 最优先，否则会退化成"该范围没数据"（信息泄露）
    if looks_empty(data) and role_filter.filtered:
        return ("\n\n【重要】本次查询已按当前用户的数据权限注入过滤条件。"
                "结果为空时，正确结论是「该范围不在你的数据权限内」，"
                "**不要**说成「该范围没有数据」或「尚未接入」。")
    # ② 查询引用的日期超出数据上界 —— 与"结果是否为空"无关：
    #    多列对比（本月0/上月66）不是空结果，却同样在误报"断崖下滑"。
    try:
        bounds = await detect_time_bounds(db)
    except Exception:
        return ""
    return beyond_data_upper_bound(sql, bounds)


async def run_query(
    question: str,
    llm: BaseChatModel,
    db: AsyncSession,
    role: str = "patient",
    dept_id: int | None = None,
    context: ConversationContext | None = None,
    role_rules: dict | None = None,
    params: dict | None = None,
    schema: str = "",
    source_name: str = "业务数据库",
    sensitive_columns: list[str] | None = None,
    examples: list[dict] | None = None,
) -> QueryResult:
    """完整 NL2SQL 流程：生成 SQL → 安全校验 → 行过滤 → 执行 → 摘要

    role_rules/params: 数据驱动的行级过滤（见 security.apply_role_filter）
    schema: 当前数据源的动态表结构描述
    source_name: 摘要里标注的数据源名
    sensitive_columns: 数据源敏感列 —— 文本层拦截 + 结果列过滤（堵 SELECT *）
    examples: few-shot 相似问答对（example_store.find_similar_examples 的结果，
              调用方按数据源检索后传入；None = 该数据源未建示例库）"""
    error_hint = ""

    # P0：方言探测一次，schema 头部跟着数据源走（无论调用方写了什么方言名都重写）
    from src.nl2sql.nodes.add_context import detect_db_info
    db_info = detect_db_info({"dw_db_session": db})
    if schema:
        first_line, _, rest = schema.partition("\n")
        if "## 数据库表结构" in first_line:
            schema = f"## 数据库表结构（{db_info['dialect']}）" + (("\n" + rest) if rest else "")

    for attempt in range(MAX_RETRIES + 1):
        raw_sql = await generate_sql(
            question, llm, context=context, error_hint=error_hint, schema=schema,
            examples=examples, dialect=db_info["dialect"],
        )
        logger.info(f"NL2SQL (attempt {attempt + 1}): {raw_sql}")

        # ★ 拒答短路：模型输出自然语言而非 SQL（典型：敏感字段被第一层防线
        #   隐藏，模型如实说「没有这个字段」）。纠错只会让它编造列名硬凑。
        from src.nl2sql.nodes.generate_sql import is_sql_text
        if raw_sql.strip() and not is_sql_text(raw_sql):
            result = QueryResult(
                question=question, sql="",
                error=f"该请求无法生成查询：{raw_sql.strip()[:300]}", success=False,
            )
            if context:
                context.add(result)
            return result

        valid, validated = validate_sql(
            raw_sql, sensitive_columns=sensitive_columns,
            dialect=db_info.get("sqlglot", "postgres"),
        )
        if not valid:
            # 安全校验失败 → 把拒绝原因喂回 LLM 纠错重试（如误用敏感字段时改写）。
            # ★ fail-closed 不变：只有过校验的 SQL 才会到执行；预算用尽仍失败则终止。
            if attempt == MAX_RETRIES:
                result = QueryResult(question=question, sql=raw_sql,
                                     error=f"安全校验失败: {validated}", success=False)
                if context:
                    context.add(result)
                return result
            error_hint = f"安全校验被拒：{validated}。请改写（敏感字段不在 schema 中，" \
                         f"用提供的编号/ID 列替代），只输出一条 SELECT 语句"
            logger.warning(f"安全校验失败 (attempt {attempt + 1}): {validated}")
            continue

        role_filter = apply_role_filter(
            validated, role, role_rules=role_rules, params=params,
        )
        allowed, filtered_sql = role_filter.allowed, role_filter.sql
        if not allowed:
            result = QueryResult(question=question, sql=raw_sql,
                                 error=filtered_sql, success=False)
            if context:
                context.add(result)
            return result

        try:
            data, columns = await execute_sql(filtered_sql, db)
            # 执行层防线：SELECT * 能穿过文本校验，敏感列在这里从结果集剔除
            # （必须在 generate_summary 之前，否则敏感数据仍会进 LLM prompt）
            columns, data = filter_result_columns(columns, data, sensitive_columns)
            # ★ 语义为空时必须换归因，否则摘要会把"没有数据"说成"业务为零"：
            #   ① 被行级权限过滤 → 说"不在你的权限内"（防信息泄露 + 假工单）
            #   ② 查的时间段超出数据上界 → 说"该时段数据尚未产生"（防误报异常）
            extra = await _empty_result_note(
                db, data, role_filter, role_rules, params, sql=filtered_sql)
            summary = await generate_summary(
                question, data, llm, source_name, extra_note=extra)

            result = QueryResult(
                question=question, sql=filtered_sql,
                data=data, columns=columns,
                row_count=len(data), summary=summary,
            )
            if context:
                context.add(result)
            return result

        except DBAPIError as e:
            await db.rollback()  # ★ 重置 abort 状态，否则后续重试全在坏事务里
            if "canceling statement" in str(e) or "timeout" in str(e).lower():
                result = QueryResult(question=question, sql=filtered_sql,
                                     error=f"查询超时（{SQL_TIMEOUT}秒）", success=False)
                if context:
                    context.add(result)
                return result
            error_hint = str(e)
            logger.warning(f"SQL 执行失败 (attempt {attempt + 1}): {e}")
            if attempt == MAX_RETRIES:
                # 原始 DB 错误只留日志（error_hint 仅供 LLM 纠错），
                # 不透给用户 —— 错误文本可能暴露表结构
                result = QueryResult(question=question, sql=filtered_sql,
                                     error="查询执行失败，请调整问题后重试", success=False)
                if context:
                    context.add(result)
                return result

        except Exception as e:
            await db.rollback()
            error_hint = str(e)
            logger.warning(f"异常 (attempt {attempt + 1}): {e}")
            if attempt == MAX_RETRIES:
                result = QueryResult(question=question, sql=raw_sql,
                                     error="查询执行失败，请调整问题后重试", success=False)
                if context:
                    context.add(result)
                return result

    result = QueryResult(question=question, sql="", error="未知错误", success=False)
    if context:
        context.add(result)
    return result


async def resolve_question(
    question: str,
    llm: BaseChatModel,
    context: ConversationContext | None = None,
) -> str:
    """多轮上下文判断（工作流第②步）。

    历史对话：基于上次 SQL 改写（追问）→ 补全为可独立执行的完整问题
    无历史对话：全新查询（原样返回）"""
    if not context or not context.last_result or not context.last_result.success:
        return question

    prompt = REWRITE_PROMPT.format(
        previous_question=context.last_result.question,
        previous_sql=context.last_result.sql,
        question=question,
    )
    response = await safe_ainvoke(llm, [SystemMessage(content=prompt)])
    rewritten = response.content.strip()
    if not rewritten or rewritten in ("无", "原问题"):
        return question

    from src.core.logger import logger
    logger.info(f"[resolve_question] 追问改写: {question[:50]} → {rewritten[:80]}")
    return rewritten
