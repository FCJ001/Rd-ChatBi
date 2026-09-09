# ============================================================
# NL2SQL 核心引擎
# ★ 修复医疗版 validated NameError (search_sql_raw 返回二元组)
# ★ 新增 last_sql 支持下钻追问
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

from src.nl2sql.prompts import (
    FOLLOWUP_PROMPT,
    NL2SQL_SYSTEM_PROMPT,
    REWRITE_PROMPT,
    SCHEMA_PROMPT,
    SUMMARY_PROMPT,
)


def build_schema_prompt(tables) -> str:
    """从元数据动态生成 SCHEMA 描述（多数据源：不再硬编码单项目表结构）。

    tables: list[TableInfo]（来自 PgMetaRepository.get_all_tables()）
    """
    lines = ["## 数据库表结构（PostgreSQL）"]
    for t in tables:
        desc = f" -- {t.description}" if t.description else ""
        lines.append(f"{t.name}（{t.role}）{desc}:")
        for c in t.columns:
            alias = f"，别名: {', '.join(c.alias)}" if c.alias else ""
            ex = f"，示例: {c.examples[:3]}" if c.examples else ""
            lines.append(f"  {c.name} {c.type} -- {c.description or c.role}{alias}{ex}")
        lines.append("")
    return "\n".join(lines)
from src.nl2sql.security import apply_role_filter, validate_sql

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


@dataclass
class ConversationContext:
    history: list[QueryResult] = field(default_factory=list)

    @property
    def last_result(self) -> QueryResult | None:
        return self.history[-1] if self.history else None

    def add(self, result: QueryResult):
        self.history.append(result)
        if len(self.history) > 10:
            self.history = self.history[-10:]


async def generate_sql(
    question: str,
    llm: BaseChatModel,
    role: str = "admin",
    dept_id: int | None = None,
    context: ConversationContext | None = None,
    error_hint: str = "",
    schema: str = "",
) -> str:
    """LLM 生成 SQL（schema 由调用方按数据源动态生成，默认用内置 SCHEMA_PROMPT）"""
    schema = schema or SCHEMA_PROMPT
    if context and context.last_result and context.last_result.success:
        prompt = FOLLOWUP_PROMPT.format(
            previous_sql=context.last_result.sql,
            previous_summary=context.last_result.summary[:500],
            question=question,
            schema=schema,
        )
    else:
        prompt = NL2SQL_SYSTEM_PROMPT.format(schema=schema)

    messages = [SystemMessage(content=prompt)]
    if error_hint:
        messages.append(HumanMessage(
            content=f"上一轮 SQL 报错：{error_hint}\n请修正。\n\n{question}"
        ))
    else:
        messages.append(HumanMessage(content=question))

    response = await llm.ainvoke(messages)
    sql = response.content.strip()
    if "```" in sql:
        sql = sql.split("```")[1].lstrip("sql").strip()
    return sql


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
    question: str, data: list[dict], llm: BaseChatModel, source_name: str = "业务数据库"
) -> str:
    """LLM 生成数据摘要（source_name 标注当前数据源，多数据源不再硬编码）"""
    result_str = json.dumps(data[:20], ensure_ascii=False, default=str)
    prompt = SUMMARY_PROMPT.format(question=question, result=result_str, source_name=source_name)
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    return response.content


async def run_query(
    question: str,
    llm: BaseChatModel,
    db: AsyncSession,
    role: str = "admin",
    dept_id: int | None = None,
    context: ConversationContext | None = None,
    role_rules: dict | None = None,
    params: dict | None = None,
    schema: str = "",
    source_name: str = "业务数据库",
) -> QueryResult:
    """完整 NL2SQL 流程：生成 SQL → 安全校验 → 行过滤 → 执行 → 摘要

    role_rules/params: 数据驱动的行级过滤（见 security.apply_role_filter）
    schema: 当前数据源的动态表结构描述
    source_name: 摘要里标注的数据源名"""
    error_hint = ""

    for attempt in range(MAX_RETRIES + 1):
        raw_sql = await generate_sql(
            question, llm, role, dept_id, context, error_hint, schema,
        )
        logger.info(f"NL2SQL (attempt {attempt + 1}): {raw_sql}")

        valid, validated = validate_sql(raw_sql)
        if not valid:
            result = QueryResult(question=question, sql=raw_sql,
                                 error=f"安全校验失败: {validated}", success=False)
            if context:
                context.add(result)
            return result

        allowed, filtered_sql = apply_role_filter(
            validated, role, role_rules=role_rules, params=params,
        )
        if not allowed:
            result = QueryResult(question=question, sql=raw_sql,
                                 error=filtered_sql, success=False)
            if context:
                context.add(result)
            return result

        try:
            data, columns = await execute_sql(filtered_sql, db)
            summary = await generate_summary(question, data, llm, source_name)

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
                result = QueryResult(question=question, sql=filtered_sql,
                                     error=f"执行失败: {error_hint}", success=False)
                if context:
                    context.add(result)
                return result

        except Exception as e:
            await db.rollback()
            error_hint = str(e)
            logger.warning(f"异常 (attempt {attempt + 1}): {e}")
            if attempt == MAX_RETRIES:
                result = QueryResult(question=question, sql=raw_sql,
                                     error=f"执行失败: {error_hint}", success=False)
                if context:
                    context.add(result)
                return result

    result = QueryResult(question=question, sql="", error="未知错误", success=False)
    if context:
        context.add(result)
    return result


# ★ 修复 NameError：search_sql_raw 返回 (data, executed_sql) 二元组
async def search_sql_raw(
    question: str,
    llm: BaseChatModel,
    db: AsyncSession,
    role: str = "admin",
    dept_id: int | None = None,
    role_rules: dict | None = None,
    params: dict | None = None,
    schema: str = "",
) -> tuple[list[dict], str]:
    """检索 NL2SQL 原始结果，返回 (rows, executed_sql)"""
    result = await run_query(
        question, llm, db, role, dept_id,
        role_rules=role_rules, params=params, schema=schema,
    )
    if result.success:
        return result.data, result.sql
    return [], result.sql


async def search_sql(
    question: str,
    llm: BaseChatModel,
    db: AsyncSession,
    role: str = "admin",
    dept_id: int | None = None,
    role_rules: dict | None = None,
    params: dict | None = None,
    schema: str = "",
) -> str:
    """检索 NL2SQL 结果，返回 LLM 摘要"""
    result = await run_query(
        question, llm, db, role, dept_id,
        role_rules=role_rules, params=params, schema=schema,
    )
    if result.success:
        return result.summary
    return result.error


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
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    rewritten = response.content.strip()
    if not rewritten or rewritten in ("无", "原问题"):
        return question

    from src.core.logger import logger
    logger.info(f"[resolve_question] 追问改写: {question[:50]} → {rewritten[:80]}")
    return rewritten
