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
from dataclasses import asdict

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_embedding_model, get_llm
from src.core.base_schema import ResponseSchema
from src.core.deps import UserContext, get_current_user
from src.core.exceptions import BizException
from src.core.logger import logger, trace_id_var
from src.core.rate_limit import enforce_rate_limit
from src.infra.datasources import DataSourceConfig, get_datasource, list_datasources
from src.infra.datasources import dw_session_factory
from src.infra.db import AsyncSessionLocal
from src.infra.es_client import get_es_client
from src.infra.milvus_client import get_milvus_client
from src.nl2sql.admin_deps import require_badcase_admin
from src.nl2sql.badcase_capture import safe_capture_legacy
from src.nl2sql.chart_advisor import recommend_chart
from src.nl2sql.ctx_store import add_turn, clear, get_context, get_history_payload
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
    # 长度上限：question 直接进 LLM prompt（防超长请求烧 token/拖垮流水线），
    # session_id 是字典 key 的一部分（防超长 key 撑内存）
    question: str = Field(..., max_length=2000, description="自然语言数据查询")
    session_id: str = Field(default="default", max_length=128,
                            description="会话ID，同会话多轮下钻")
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


# ── 对话上下文存储（见 src/nl2sql/ctx_store.py）─────────────────────────
# backend=redis：跨 worker 共享、重启不丢、TTL 自动过期
# backend=memory：进程内 dict（开发用）
# ★ key 必须含 user_id：session_id 是用户输入且默认 "default"，
#   只按 project+session 隔离会让同项目所有用户共享/互读对话历史。


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
async def get_datasources(user: UserContext = Depends(get_current_user)):
    """可用数据源列表（需认证：数据源清单属于内部拓扑信息）。

    ★ 同时返回每个数据源的角色清单与候选参数值：不同数据源的角色模型完全
      不同（医院是 doctor/cashier，汽车是 engineer/business/aftersales），
      前端若把角色写死，切数据源后必然对不上 —— 所以由后端下发，
      前端据此动态渲染角色下拉和参数输入。

    ★ 候选值来自 ES 里已有的真实枚举值（值召回那一步顺手就取了），
      否则用户根本不可能知道「电池系统域」的 owner_domain_id 是 1。
    """
    sources = await list_datasources()
    out = []
    for ds in sources:
        roles, options = await _role_options(ds)
        out.append({
            "code": ds.code,
            "name": ds.name,
            "description": ds.description,
            "roles": roles,
            "param_options": options,
        })
    return ResponseSchema(data=out)


async def _role_options(ds) -> tuple[list[dict], dict]:
    """把 role_rules 摊平成前端好用的角色清单 + 参数候选值。

    角色结构：[{name, label, param?, column?, options?}]
      - all   → 无参数（admin）
      - deny  → 无参数，但会被后端拒绝（patient / customer）
      - param → 需要一个运行时参数，并附上候选值供下拉选择
      - value → 固定条件，无需用户输入（cashier / aftersales）

    候选值：对带 param 的列，去 ES 值索引取该列的 source="db" 真实值。
      形如 owner_domain_id 这种数字 ID，用户不可能凭记忆填对；
      而 ES 里恰好存着「1→电池系统域」这层映射的字典值。
    """
    rules = (ds.role_rules or {}).get("roles") or {}
    roles: list[dict] = []
    options: dict[str, list[str]] = {}

    for name, rule in rules.items():
        opt: dict = {"name": name}
        if rule == "all":
            opt["label"] = "全量"
        elif rule == "deny":
            opt["label"] = "无权限"
        elif isinstance(rule, dict) and rule.get("param"):
            opt["label"] = f"按 {rule['param']}"
            opt["param"] = rule["param"]
            opt["column"] = rule.get("column", "")
            opts = await _param_options(ds, rule)
            if opts:
                opt["options"] = opts
                options.setdefault(rule["param"], opts)
        elif isinstance(rule, dict) and rule.get("value"):
            opt["label"] = f"限定 {rule['value']}"
        else:
            opt["label"] = "规则不完整（会被拒绝）"
        roles.append(opt)

    return roles, options


async def _param_options(ds, rule: dict) -> list[dict]:
    """取某列的真实枚举值作为候选（{value, label}），供前端渲染下拉。

    ★ 两个坑：
      ① role_rules 里的 column 是**裸列名**（owner_domain_id），而 ES 存的是
         全限定名（alm_issues.owner_domain_id）—— 必须按后缀匹配。
      ② 外键列存的是数字 ID（1..9），用户要看的是名字（电池系统域），而请求头
         里必须传 ID。两类信息存在**两列**里。这里按「排序后的位置」把 ID 和
         维表可读名对齐拼起来；拼不上就退回只用 ID，功能不受影响。
    取不到就返回空，前端退化成自由输入。
    """
    try:
        from src.infra.es_client import get_es_client

        es = await get_es_client()
        column = rule.get("column") or ""
        if not column:
            return []
        index = f"chatbi_{ds.es_prefix}_values"

        async def _db_values_of(cid: str) -> list[str]:
            # ★ 用 match 而非 term：column_id 在不同写入路径下格式不一致
            #   （元数据落库写全限定名 `alm_issues.owner_domain_id`，
            #    link_fk_labels 补的是裸名 `owner_domains.name`）。
            #   match 走分词，两种写法都能命中，不依赖命名约定。
            res = await es.search(
                index=index,
                body={"query": {"bool": {"must": [
                    {"match": {"column_id": cid}}, {"term": {"source": "db"}}]}},
                    "size": 200},
            )
            return [str(h["_source"]["value"]) for h in res["hits"]["hits"]
                    if h["_source"].get("value") is not None]

        # ① column 可能是裸列名，先在全限定名里找匹配的那一列
        full = column if "." in column else await _resolve_column(ds, column)
        ids = await _db_values_of(full)
        if not ids:
            return []

        # ② 附上可读名。
        #    ★ 不能靠「排序后按位置对齐」—— 事实表里可能只出现了部分维表值
        #      （alm_issues 只用到 7 个域，owner_domains 有 9 个），数量不等就错位。
        #      正确做法是**按 ID 关联**：维表那列也带 enum_label=主键，
        #      用 ID 做键去查名字。
        id_to_name = await _dim_names_by_id(ds, bare=column, full=full)
        return [{"value": v, "label": id_to_name.get(v, v)}
                for v in sorted(ids, key=_num_or_str)]
    except Exception as e:
        logger.warning(f"[datasources] 取 {rule.get('param')} 候选值失败: {e}")
        return []


async def _dim_names_by_id(ds, bare: str, full: str) -> dict[str, str]:
    """可读名，按 ID 索引：{"1": "电池系统域", ...}。

    ★ 不做任何表名推导。之前试过「从外键列名猜维表名」（owner_domain_id →
       owner_domains.name），单复数/命名约定一变就错，而且事实表和维表的前缀
       压根对不上（alm_issues vs owner_domains）。
       现在直接取索引里**所有带 enum_label 的记录** —— 那些正是
       scripts/link_fk_labels.py 用真 JOIN 建出来的 (ID ↔ 名称) 对应关系。
      记录数很少（每张维表几十条），一次取完即可。
    """
    from src.infra.es_client import get_es_client

    try:
        es = await get_es_client()
        res = await es.search(
            index=f"chatbi_{ds.es_prefix}_values",
            body={"query": {"exists": {"field": "enum_label"}}, "size": 500},
        )
        out: dict[str, str] = {}
        for h in res["hits"]["hits"]:
            src = h["_source"]
            # ★ ES 里 value=名称、enum_label=ID（link_fk_labels.py 补出来的一对）。
            #   所以「用 ID 查名称」就是：以 enum_label 为键、value 为值。
            #   反着取会得到「用名字查 ID」，那对下拉没用。
            if src.get("enum_label"):
                out[str(src["enum_label"])] = str(src["value"])
        return out
    except Exception as e:
        logger.warning(f"[datasources] 取维表名失败: {e}")
        return {}


async def _resolve_column(ds, bare: str) -> str:
    """裸列名 → 全限定列名（ES 里存的是 `表.列`）。

    同一裸名可能出现在多张表（如多张表都有 status），取第一个命中的；
    行级过滤的 column 通常只在事实表上，实际不会歧义。
    """
    from src.infra.es_client import get_es_client

    es = await get_es_client()
    res = await es.search(
        index=f"chatbi_{ds.es_prefix}_values",
        body={"query": {"bool": {"must": [
            {"wildcard": {"column_id": f"*.{bare}"}},
            {"term": {"source": "db"}}]}}, "size": 1,
            "_source": ["column_id"]},
    )
    hits = res["hits"]["hits"]
    return hits[0]["_source"]["column_id"] if hits else bare


def _num_or_str(v: str):
    """数字串按数值排序，其余按字符串 —— 否则 '10' 会排在 '2' 前面"""
    return (0, int(v)) if v.lstrip("-").isdigit() else (1, v)


# ════════════════════════════════════════════════════════════════
# 旧引擎 — 一次性 JSON 响应（多轮下钻，低延迟场景）
# ════════════════════════════════════════════════════════════════

@router.post("/query", response_model=ResponseSchema[BIQueryResponse])
async def bi_query(
    req: BIQueryRequest,
    user: UserContext = Depends(get_current_user),
    # 限流声明在 dw 之前：FastAPI 按参数顺序解析依赖，被限流的请求不占业务库连接
    _: None = Depends(enforce_rate_limit),
    dw: tuple[AsyncSession, DataSourceConfig] = Depends(get_project_dw),
):
    """自然语言数据查询，返回 SQL + 数据表 + 图表 + 摘要"""
    db, ds = dw
    llm = get_llm()
    ctx = await get_context(user.user_id, user.project_id, req.session_id)

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
        sensitive_columns=ds.sensitive_columns,
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

    # ★ 必须显式持久化：run_query 内部的 context.add() 只改内存里那个对象，
    #   而 get_context 在 redis 后端下每次返回的是**新构造的**实例（不落
    #   _MEMORY_STORE）—— 不调 add_turn 的话，/query 这条链路的历史从来没进过
    #   Redis，多轮追问与用户反馈溯源都会失忆。add_turn 读的是 Redis 里的那份，
    #   不会把本轮记两次。
    await add_turn(user.user_id, user.project_id, req.session_id, result)

    # badcase 回流：失败/被拒/空结果的查询自动进审核队列（fail-open）
    await safe_capture_legacy(
        user=user, question=req.question, datasource_code=ds.code,
        result=result, trace_id=trace_id_var.get() or "",
    )

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
    # 限流声明在 dw 之前：被限流的请求不占业务库连接
    _: None = Depends(enforce_rate_limit),
    dw: tuple[AsyncSession, DataSourceConfig] = Depends(get_project_dw),
):
    """自然语言数据查询 — SSE 流式返回各节点执行状态 + 最终结果（含图表）"""
    dw_db, ds = dw
    ctx, meta_db = await _build_pipeline_context(ds, dw_db)
    try:
        # 认证用户角色 + 数据源权限规则 → execute_sql 节点行级过滤
        ctx["role"] = user.role
        ctx["role_rules"] = ds.role_rules
        ctx["source_name"] = ds.name
        ctx["sensitive_columns"] = ds.sensitive_columns
        ctx.update(_user_params(user))

        # ② 多轮上下文判断：追问 → 改写为独立完整问题；无历史 → 全新查询
        conv_ctx = await get_context(user.user_id, user.project_id, req.session_id)
        try:
            question = await resolve_question(req.question, ctx["llm"], conv_ctx)
        except Exception as e:
            logger.warning(f"多轮改写失败，使用原始问题: {e}")
            question = req.question

        # badcase 回流所需的现场：流水线内部拿不到从请求头来的这些字段
        ctx["datasource_id"] = ds.id
        ctx["datasource_code"] = ds.code
        ctx["session_id"] = req.session_id
        ctx["user_id"] = user.user_id
        ctx["trace_id"] = trace_id_var.get() or ""
        # ★ 原始问题与改写后的问题必须都留着：落库/落历史用原始问题，
        #   复现案例用改写后的问题（多轮追问下后者才是独立可跑的）
        ctx["raw_question"] = req.question
        ctx["resolved_question"] = question
    except Exception:
        # 流式响应开始前出错：meta_db 的关闭职责还没移交给 event_stream 的
        # finally，必须在这里关掉，否则连接泄漏
        await meta_db.close()
        raise

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

            # ⑧ 结果写入会话历史（供多轮追问使用）
            #
            # ★ 两个已修的缺陷（它们让「从会话历史挖 badcase」直接空转）：
            #   ① 原来只在 `last_result.get("result_data")` 非空时才写 ——
            #      失败轮次 result_data 是空列表，于是全部失败轮**根本不落历史**。
            #   ② 构造 QueryResult 时不传 success/error，而 dataclass 默认
            #      success=True —— 就算落了也是「假成功」，事后无法区分。
            #   现在无条件落，且如实带上 success/error。
            if last_result:
                err = last_result.get("result_error") or last_result.get("error") or ""
                await add_turn(user.user_id, user.project_id, req.session_id, QueryResult(
                    # ★ 存原始问题：改写后的「住院记录数是多少？」回看历史时对不上号
                    question=req.question,
                    sql=last_result.get("result_sql", ""),
                    row_count=last_result.get("result_row_count", 0),
                    summary=last_result.get("result_summary", ""),
                    success=not err,
                    error=err,
                ))
        finally:
            # 客户端断连（GeneratorExit）时取消流水线：await 未取消的 task
            # 会把剩余的多次 LLM 调用全部跑完才退出，白烧钱。
            # 正常跑完的情况下 cancel() 是 no-op，await 直接返回。
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # loguru 语法：exc_info=True 是 stdlib 风格，loguru 会静默丢 traceback
                logger.opt(exception=True).warning("SSE 流水线任务收尾异常")
            finally:
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
    """获取会话的 NL2SQL 对话历史（按 user:project:session 隔离，只能看自己的）"""
    payload = await get_history_payload(user.user_id, user.project_id, session_id)
    sql_history = [
        {**r, "summary": (r.get("summary") or "")[:200]}
        for r in payload
    ]

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
    """清除会话历史（只能清自己的）"""
    await clear(user.user_id, user.project_id, session_id)
    return ResponseSchema(data={"session_id": session_id, "status": "cleared"})


# ════════════════════════════════════════════════════════════════
# badcase 回流（评测集真相来源）
#
# 队列与导出见 src/nl2sql/repositories/badcase_repo.py，
# 纯函数（指纹/分类/校验）见 src/nl2sql/badcase_store.py。
# ════════════════════════════════════════════════════════════════

class BadcaseReportRequest(BaseModel):
    """前端「答得不对」上报。

    ★ 只收前端真正知道的东西：问题、会话、它渲染出来的那条 SQL。
      其余字段（改写后的问题 / 角色 / 行数 / 预测 SQL）一律由服务端从会话
      历史反查 —— 前端能拿到的只有「它收到的最后一帧」，那一帧可能来自另一次
      请求；而 header 认证模式下这些值本来就是可伪造的。宁可少采，不可采脏。
    """
    question: str = Field(..., max_length=2000, description="用户原始问题")
    session_id: str = Field(default="default", max_length=128)
    sql: str = Field(default="", max_length=20000, description="前端展示的 SQL，仅用于比对")
    reason: str = Field(default="", max_length=500, description="可选补充说明")


class BadcaseReviewRequest(BaseModel):
    """审核动作。status 留空表示只改打标字段，不动状态"""
    status: str = Field(default="", description="pending / approved / rejected（留空=只改字段）")
    golden_sql: str | None = Field(default=None, max_length=20000)
    category: str | None = Field(default=None, max_length=50)
    difficulty: str | None = Field(default=None, max_length=10)
    reject_reason: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


def _match_history_turn(history: list[dict], question: str) -> dict | None:
    """从会话历史里找出被反馈的那一轮（最近的一条匹配）。

    ★ 按归一化问题匹配而不是「取最后一条」：用户可能在拿到结果后又问了几句
      才回头点反馈，取最后一条会张冠李戴。
    """
    from src.nl2sql.badcase_store import normalize_question

    target = normalize_question(question)
    for turn in reversed(history):
        if normalize_question(turn.get("question") or "") == target:
            return turn
    return None


@router.post("/badcases", response_model=ResponseSchema[dict])
async def report_badcase(
    req: BadcaseReportRequest,
    user: UserContext = Depends(get_current_user),
    # 限流必须挂：header 模式下 user_id 可伪造，不挂等于开了一个无限写 PG 的口子
    _: None = Depends(enforce_rate_limit),
):
    """用户标记「答得不对」→ 进待审队列（普通用户即可调用）"""
    from src.nl2sql.badcase_store import fingerprint
    from src.nl2sql.repositories import BadcaseRepository

    ds = await get_datasource(user.project_id)
    if ds is None:
        raise BizException(f"数据源 {user.project_id} 未注册或未启用", code=40004)

    history = await get_history_payload(user.user_id, user.project_id, req.session_id)
    turn = _match_history_turn(history, req.question)

    note = ""
    predicted_sql = ""
    resolved_question = ""
    row_count = 0
    if turn is None:
        # 历史里没有（TTL 过期 / 前端对不上号）—— 仍然收，但标注来源存疑。
        # ★ 不返回 4xx：用户反馈的价值高于一致性校验，硬拒等于把 bug 挡在门外。
        note = "会话历史中未找到对应轮次，predicted_sql 缺失"
        predicted_sql = req.sql
    else:
        predicted_sql = turn.get("sql") or ""
        resolved_question = turn.get("question") or ""
        row_count = int(turn.get("row_count") or 0)
        if req.sql and predicted_sql and req.sql != predicted_sql:
            note = "前端上报 SQL 与服务端历史不一致（以服务端为准）"
        elif not predicted_sql:
            predicted_sql = req.sql

    async with AsyncSessionLocal() as db:
        case_id, seen_count = await BadcaseRepository(db).upsert_badcase(
            datasource_id=ds.id,
            datasource_code=ds.code,
            source="manual",
            question=req.question,
            resolved_question=resolved_question,
            session_id=req.session_id,
            user_role=user.role,
            role_params=_user_params(user),
            predicted_sql=predicted_sql,
            error_type="unsatisfied",
            error_message="用户标记答非所问",
            row_count=row_count,
            trace_id=trace_id_var.get() or "",
            note=(note + ("；" + req.reason if req.reason else "")).strip("；"),
        )

    logger.info(
        f"[badcase] 用户反馈已记录 id={case_id} ds={ds.code} "
        f"fp={fingerprint(ds.id, req.question)[:8]} seen={seen_count}"
    )
    return ResponseSchema(data={
        "id": case_id,
        "status": "pending",
        "seen_count": seen_count,
        "matched_history": turn is not None,
    })


@router.get("/badcases/stats", response_model=ResponseSchema[dict])
async def badcase_stats(
    user: UserContext = Depends(get_current_user),
    _admin: str = Depends(require_badcase_admin),
):
    """审核队列概览（管理员）"""
    from src.infra.datasources import get_datasource
    from src.nl2sql.repositories import BadcaseRepository

    ds = await get_datasource(user.project_id)
    if ds is None:
        raise BizException(f"数据源 {user.project_id} 未注册或未启用", code=40004)

    async with AsyncSessionLocal() as db:
        data = await BadcaseRepository(db, ds.id).stats()
    return ResponseSchema(data=data)


@router.get("/badcases", response_model=ResponseSchema[dict])
async def list_badcases(
    status: str = "pending",
    error_type: str = "",
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(get_current_user),
    _admin: str = Depends(require_badcase_admin),
):
    """待审队列（管理员）。默认只按 status 过滤，datasource 固定为当前请求头的那个"""
    from src.nl2sql.repositories import BadcaseRepository

    ds = await get_datasource(user.project_id)
    if ds is None:
        raise BizException(f"数据源 {user.project_id} 未注册或未启用", code=40004)

    async with AsyncSessionLocal() as db:
        repo = BadcaseRepository(db, ds.id)
        cases, total = await repo.list_cases(
            status=status or None,
            error_type=error_type or None,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
    return ResponseSchema(data={
        "total": total,
        "items": [asdict(c) for c in cases],
        "datasource": ds.code,
    })


@router.patch("/badcases/{case_id}", response_model=ResponseSchema[dict])
async def review_badcase(
    case_id: int,
    req: BadcaseReviewRequest,
    user: UserContext = Depends(get_current_user),
    _admin: str = Depends(require_badcase_admin),
):
    """审核：打标 / 补 golden_sql / 通过 / 驳回（管理员）"""
    from src.nl2sql.badcase_store import CaseValidationError
    from src.nl2sql.repositories import (
        STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED, BadcaseRepository,
    )

    ds = await get_datasource(user.project_id)
    if ds is None:
        raise BizException(f"数据源 {user.project_id} 未注册或未启用", code=40004)

    if req.status not in ("", STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED):
        raise BizException(f"不支持的状态流转: {req.status}", code=40005)

    async with AsyncSessionLocal() as db:
        repo = BadcaseRepository(db, ds.id)
        current = await repo.get_case(case_id)
        if current is None:
            raise BizException(f"案例 {case_id} 不存在或不属于数据源 {ds.code}", code=40404)

        # approve 是最严的一道门：它决定一条线上记录能不能进评测集。
        # 真正的把守点在 repo.update_review 里（那条路径所有调用方都要过），
        # 这里先拦一道是为了把失败原因作为 40006 回给前端而不是 500。
        try:
            updated = await repo.update_review(
                case_id,
                status=req.status or None,
                golden_sql=req.golden_sql,
                category=req.category,
                difficulty=req.difficulty,
                reject_reason=req.reject_reason,
                note=req.note,
                reviewer=_admin,
                sensitive_columns=ds.sensitive_columns,
            )
        except CaseValidationError as e:
            raise BizException(f"不能通过：{e}", code=40006)

    logger.info(f"[badcase] 审核 id={case_id} → {updated.status} by={_admin}")
    return ResponseSchema(data=asdict(updated))


@router.post("/badcases/export", response_model=ResponseSchema[dict])
async def export_badcases(
    user: UserContext = Depends(get_current_user),
    _admin: str = Depends(require_badcase_admin),
):
    """导出预览（管理员）：把 approved 案例渲染成评测器吃的 JSON。

    ★ 只返回预览，不写文件 —— 落盘由 scripts/export_badcase_cases.py 做，
      且必须走 git 提交，评测集的变化要能在 PR 里被 review。
    """
    from src.nl2sql.badcase_export import build_export

    ds = await get_datasource(user.project_id)
    if ds is None:
        raise BizException(f"数据源 {user.project_id} 未注册或未启用", code=40004)

    async with AsyncSessionLocal() as db:
        payload = await build_export(db, ds)
    return ResponseSchema(data=payload)
