# ============================================================
# ChatBI API — NL2SQL 查询 + 图表推荐（生产端口）
#
# 多数据源：请求头 X-Project-Id 路由到对应业务库/元数据/权限规则
# （注册表见 bi_datasources，项目元数据见 conf/projects/{code}.yaml）
#
# GET    /api/v1/bi/datasources             可用数据源列表
# POST   /api/v1/bi/query                   自然语言查数据 → SQL + 数据表 + 图表 + 摘要
# POST   /api/v1/bi/query-stream            9 阶段流水线 SSE 流式查询（含图表）
# GET    /api/v1/bi/history/{session_id}    查看对话历史
# DELETE /api/v1/bi/history/{session_id}    清除对话历史
# ============================================================

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_embedding_model, get_llm
from src.core.base_schema import ResponseSchema
from src.core.deps import UserContext, get_current_user
from src.core.exceptions import BizException
from src.core.logger import logger
from src.infra.datasources import DataSourceConfig, get_datasource, list_datasources
from src.infra.datasources import dw_session_factory
from src.infra.db import AsyncSessionLocal
from src.infra.es_client import get_es_client
from src.infra.milvus_client import get_milvus_client
from src.nl2sql.chart_advisor import recommend_chart
from src.nl2sql.echarts_builder import to_echarts_option
from src.nl2sql.engine import (
    ConversationContext,
    QueryResult,
    resolve_question,
    run_query,
)
from src.nl2sql.pipeline import run_pipeline
from src.nl2sql.repositories import (
    ESValueRepository,
    MilvusColumnRepository,
    MilvusMetricRepository,
    PgMetaRepository,
)

router = APIRouter(prefix="/api/v1/bi", tags=["ChatBI"])


# ── Request / Response models ────────────────────────────────────────────

class BIQueryRequest(BaseModel):
    question: str = Field(..., description="自然语言数据查询")
    session_id: str = Field(default="default", description="会话ID，同会话多轮下钻")
    with_chart: bool = Field(default=True, description="是否返回图表配置")


class BIQueryResponse(BaseModel):
    question: str
    sql: str = ""
    data: list[dict] = []
    columns: list[str] = []
    row_count: int = 0
    summary: str = ""
    chart: dict | None = None
    success: bool = True
    error: str = ""


# ── 对话上下文存储（内存，按 project:session 隔离）────────────────────────

_ctx_store: dict[str, ConversationContext] = {}


def _ctx_key(project_id: str, session_id: str) -> str:
    return f"{project_id}:{session_id}"


def _get_or_create_ctx(project_id: str, session_id: str) -> ConversationContext:
    key = _ctx_key(project_id, session_id)
    if key not in _ctx_store:
        _ctx_store[key] = ConversationContext()
    return _ctx_store[key]


def _user_params(user: UserContext) -> dict:
    """认证用户的行级过滤参数（供 role_rules 引用）"""
    return {
        "dept_id": user.dept_id,
        "owner_domain_id": user.owner_domain_id,
        "business_line": user.business_line,
    }


async def get_project_dw(
    user: UserContext = Depends(get_current_user),
) -> tuple[AsyncSession, DataSourceConfig]:
    """依赖：按 X-Project-Id 打开对应业务库只读 session，并返回数据源配置"""
    ds = await get_datasource(user.project_id)
    if ds is None:
        raise BizException(f"数据源 {user.project_id} 未注册或未启用", code=40004)
    factory = dw_session_factory(user.project_id)
    async with factory() as session:
        yield session, ds


# ════════════════════════════════════════════════════════════════
# 数据源列表（前端切换下拉 / 接入方发现）
# ════════════════════════════════════════════════════════════════

@router.get("/datasources", response_model=ResponseSchema[list])
async def get_datasources():
    """可用数据源列表"""
    sources = await list_datasources()
    return ResponseSchema(data=[
        {"code": ds.code, "name": ds.name, "description": ds.description}
        for ds in sources
    ])


# ════════════════════════════════════════════════════════════════
# 旧引擎 — 一次性 JSON 响应（多轮下钻，低延迟场景）
# ════════════════════════════════════════════════════════════════

@router.post("/query", response_model=ResponseSchema[BIQueryResponse])
async def bi_query(
    req: BIQueryRequest,
    user: UserContext = Depends(get_current_user),
    dw: tuple[AsyncSession, DataSourceConfig] = Depends(get_project_dw),
):
    """自然语言数据查询，返回 SQL + 数据表 + 图表 + 摘要"""
    db, ds = dw
    llm = get_llm()
    ctx = _get_or_create_ctx(user.project_id, req.session_id)

    # 按数据源动态生成 schema（多数据源不再硬编码表结构）
    schema = await _build_schema(ds)
    params = _user_params(user)

    result = await run_query(
        question=req.question,
        llm=llm,
        db=db,
        role=user.role,
        dept_id=user.dept_id,
        context=ctx,
        role_rules=ds.role_rules,
        params=params,
        schema=schema,
        source_name=ds.name,
    )

    resp = BIQueryResponse(
        question=req.question,
        sql=result.sql,
        data=result.data if result.success else [],
        columns=result.columns,
        row_count=result.row_count,
        summary=result.summary,
        success=result.success,
        error=result.error,
    )

    if result.success and result.data and req.with_chart:
        try:
            chart_config = await recommend_chart(
                question=req.question,
                data=result.data,
                columns=result.columns,
                llm=llm,
            )
            resp.chart = to_echarts_option(result.data, chart_config)
        except Exception as e:
            logger.warning(f"图表生成失败: {e}")

    return ResponseSchema(data=resp)


async def _build_schema(ds: DataSourceConfig) -> str:
    """从元数据库动态生成当前数据源的 SCHEMA 描述"""
    from src.nl2sql.engine import build_schema_prompt

    meta_db = AsyncSessionLocal()
    try:
        repo = PgMetaRepository(meta_db, ds.id)
        tables = await repo.get_all_tables()
        for t in tables:
            t.columns = await repo.get_columns_by_table(t.id)
        return build_schema_prompt(tables)
    finally:
        await meta_db.close()


# ════════════════════════════════════════════════════════════════
# 9 阶段流水线 — SSE 流式响应
# ════════════════════════════════════════════════════════════════

def _make_json_safe(obj):
    """递归将 dataclass 对象转换为 JSON 可序列化的 dict"""
    from dataclasses import fields, is_dataclass

    if is_dataclass(obj) and not isinstance(obj, type):
        result = {}
        for f in fields(obj):
            result[f.name] = _make_json_safe(getattr(obj, f.name))
        return result
    elif isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_make_json_safe(item) for item in obj]
    else:
        return obj


async def _build_pipeline_context(
    ds: DataSourceConfig, dw_db: AsyncSession
) -> tuple[dict, AsyncSession]:
    """构建 DataAgentContext，供流水线节点使用（按数据源隔离存储命名）"""
    milvus = get_milvus_client()
    es = await get_es_client()
    meta_db = AsyncSessionLocal()

    ctx = {
        "llm": get_llm(),
        "embedding_model": get_embedding_model(),
        "milvus_client": milvus,
        "milvus_column_repo": MilvusColumnRepository(milvus, prefix=ds.milvus_prefix),
        "milvus_metric_repo": MilvusMetricRepository(milvus, prefix=ds.milvus_prefix),
        "es_client": es,
        "es_value_repo": ESValueRepository(es, prefix=ds.es_prefix),
        "pg_meta_repo": PgMetaRepository(meta_db, ds.id),
        "dw_db_session": dw_db,
    }
    return ctx, meta_db


@router.post("/query-stream")
async def bi_query_stream(
    req: BIQueryRequest,
    user: UserContext = Depends(get_current_user),
    dw: tuple[AsyncSession, DataSourceConfig] = Depends(get_project_dw),
):
    """自然语言数据查询 — SSE 流式返回各节点执行状态 + 最终结果（含图表）"""
    dw_db, ds = dw
    ctx, meta_db = await _build_pipeline_context(ds, dw_db)

    # 认证用户角色 + 数据源权限规则 → execute_sql 节点行级过滤
    ctx["role"] = user.role
    ctx["role_rules"] = ds.role_rules
    ctx["source_name"] = ds.name
    ctx.update(_user_params(user))

    # ② 多轮上下文判断：追问 → 改写为独立完整问题；无历史 → 全新查询
    conv_ctx = _get_or_create_ctx(user.project_id, req.session_id)
    try:
        question = await resolve_question(req.question, ctx["llm"], conv_ctx)
    except Exception as e:
        logger.warning(f"多轮改写失败，使用原始问题: {e}")
        question = req.question

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        # 注入 writer 回调，节点通过它推送进度/结果事件
        def writer(msg: dict):
            queue.put_nowait({"__progress__": msg})
        ctx["writer"] = writer

        async def run_to_queue():
            try:
                async for event in run_pipeline(question, ctx):
                    await queue.put(event)
            except Exception as e:
                logger.error(f"Pipeline 执行失败: {e}")
                await queue.put({"__error__": str(e)})
            finally:
                await queue.put(None)  # sentinel

        task = asyncio.ensure_future(run_to_queue())

        # execute_sql 结果，用于写入会话历史（多轮追问）
        last_result: dict = {}

        try:
            # 先推送多轮判断结果
            yield f"data: {json.dumps({'node': 'resolve_question', 'step': 0, 'data': {'raw': req.question, 'resolved': question, 'is_followup': question != req.question}}, ensure_ascii=False, default=str)}\n\n"

            node_count = 0
            while True:
                event = await queue.get()
                if event is None:
                    break
                if "__error__" in event:
                    yield f"data: {json.dumps({'node': 'error', 'step': node_count, 'data': {'error': event['__error__']}}, ensure_ascii=False, default=str)}\n\n"
                    break
                if "__progress__" in event:
                    # 结果事件里可能带 Decimal/datetime 等，统一转 JSON 安全类型
                    safe_progress = _make_json_safe(event["__progress__"])
                    yield f"data: {json.dumps(safe_progress, ensure_ascii=False, default=str)}\n\n"
                    continue

                node_count += 1
                node_name = list(event.keys())[0] if event else "unknown"
                node_data = event.get(node_name, {})
                safe_data = _make_json_safe(node_data)

                payload = {
                    "node": node_name,
                    "step": node_count,
                    "data": safe_data if safe_data else {},
                }

                if node_name == "execute_sql" and isinstance(node_data, dict):
                    last_result = node_data

                    # ⑤ LLM 推荐图表 → ⑥ ECharts option 渲染
                    if node_data.get("result_data"):
                        try:
                            chart_config = await recommend_chart(
                                question=req.question,
                                data=node_data["result_data"],
                                columns=node_data.get("result_columns", []),
                                llm=ctx["llm"],
                            )
                            payload["chart"] = to_echarts_option(
                                node_data["result_data"], chart_config
                            )
                            payload["chart_config"] = chart_config
                        except Exception as e:
                            logger.warning(f"图表生成失败: {e}")

                yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

            # ⑧ 成功结果写入会话历史（供多轮追问使用）
            if last_result and last_result.get("result_data"):
                conv_ctx.add(QueryResult(
                    question=question,
                    sql=last_result.get("result_sql", ""),
                    data=last_result.get("result_data", []),
                    columns=last_result.get("result_columns", []),
                    row_count=last_result.get("result_row_count", 0),
                    summary=last_result.get("result_summary", ""),
                ))
        finally:
            await task  # ensure pipeline completes
            await meta_db.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ════════════════════════════════════════════════════════════════
# 会话历史
# ════════════════════════════════════════════════════════════════

@router.get("/history/{session_id}", response_model=ResponseSchema[dict])
async def get_history(
    session_id: str,
    user: UserContext = Depends(get_current_user),
):
    """获取会话的 NL2SQL 对话历史"""
    ctx = _ctx_store.get(_ctx_key(user.project_id, session_id))
    sql_history = []
    if ctx:
        for r in ctx.history:
            sql_history.append({
                "question": r.question,
                "sql": r.sql,
                "row_count": r.row_count,
                "summary": r.summary[:200] if r.summary else "",
                "success": r.success,
                "error": r.error,
            })

    return ResponseSchema(data={
        "session_id": session_id,
        "project_id": user.project_id,
        "sql_history": sql_history,
    })


@router.delete("/history/{session_id}", response_model=ResponseSchema[dict])
async def clear_history(
    session_id: str,
    user: UserContext = Depends(get_current_user),
):
    """清除会话历史"""
    _ctx_store.pop(_ctx_key(user.project_id, session_id), None)
    return ResponseSchema(data={"session_id": session_id, "status": "cleared"})
