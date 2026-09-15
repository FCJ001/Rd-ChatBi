# ============================================================
# badcase 采集 —— 把线上失败查询变成待审评测案例
#
# 纯函数（指纹/分类/校验）在 badcase_store.py，写库在
# repositories/badcase_repo.py；这里只负责「从请求现场收集字段 + 落库」。
#
# ★ 三条铁律：
#   ① fail-open：采集失败绝不能影响用户的查询 —— 与 ctx_store 同一取向。
#      这个模块里的每一个公开函数对外都只可能 warning，不会抛。
#   ② 不落结果行：只存问题/预测 SQL/脱敏错误。结果集不过河（同 ctx_store）。
#   ③ 不落原始 DB 错误：error_message 只放用户可见文案，原始错误靠
#      trace_id 回捞服务端日志 —— 「错误文本可能暴露表结构」是源码里
#      明写的约束（engine.py / execute_sql.py）。
# ============================================================

from __future__ import annotations

import asyncio
from datetime import datetime

from src.core.config import get_settings
from src.core.logger import logger
from src.nl2sql.repositories import BadcaseRepository

# 采集侧看到的三个信号源（优先级从高到低）
#   result_error  执行失败（业务失败，最有价值）
#   error         流水线中途失败（校验/纠错节点）
#   __error__     graph.astream 抛出的异常（模型/基础设施故障）
_ERROR_KEYS = ("result_error", "error", "__error__")

# 落库超时：采集是旁路，不能拖住 SSE 的收尾。正常情况是一次本地 PG 往返。
_DB_TIMEOUT_SECONDS = 3.0


def _extract_error(trace: dict) -> str:
    for key in _ERROR_KEYS:
        v = trace.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _is_infra_failure(trace: dict) -> bool:
    """是否属于「模型/基础设施故障」而非「答得不对」。

    这类（熔断打开、LLM 超时、图执行异常）不是案例质量问题 ——
    把模型没扛住的请求收进审核队列只会淹没真正该补的案例，
    所以默认不收，由 BADCASE_CAPTURE_LLM_ERRORS 单独放行。
    """
    if trace.get("__error__"):
        return True
    return False


async def _ds_id_of_case(code: str) -> int | None:
    """数据源编码 → id。取不到就放弃采集（宁可少采一条，也不写脏数据）"""
    from src.infra.datasources import get_datasource

    ds = await get_datasource(code)
    return ds.id if ds else None


async def _within_daily_cap(datasource_id: int) -> bool:
    """当天自动采集量是否还在上限内。

    ★ 必须有这道闸：header 认证模式下 user_id 可伪造，采集等于对外开了一个
      能写 PG 的口子，无上限会被刷爆（与限流模块同一威胁模型）。
    """
    cap = get_settings().BADCASE_DAILY_CAP
    if cap <= 0:
        return True
    from sqlalchemy import func, select

    from src.infra.db import AsyncSessionLocal
    from src.nl2sql.models import Badcase

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    async with AsyncSessionLocal() as db:
        n = (await db.execute(
            select(func.count()).select_from(Badcase).where(
                Badcase.datasource_id == datasource_id,
                Badcase.created_at >= today,
            )
        )).scalar_one()
    return int(n) < cap


async def _capture(
    *,
    datasource_code: str,
    source: str,
    question: str,
    resolved_question: str,
    session_id: str,
    user_role: str,
    role_params: dict,
    predicted_sql: str,
    error: str,
    row_count: int,
    sql_fix_rounds: int,
    trace_id: str,
    note: str = "",
) -> tuple[int, int] | None:
    """共同落库路径。返回 (id, seen_count)，被跳过时返回 None。

    ★ 全程 fail-open：这个函数 contract 上不会抛异常。
    """
    settings = get_settings()
    if not settings.BADCASE_CAPTURE_ENABLED:
        return None

    from src.nl2sql.badcase_store import classify_capture

    error_type = classify_capture(error, predicted_sql, row_count)
    if not error_type:
        return None  # 查成功且有数据 —— 正常查询，不采

    ds_id = await _ds_id_of_case(datasource_code)
    if ds_id is None:
        logger.warning(f"[badcase] 数据源 {datasource_code} 未注册，跳过采集")
        return None

    if not await _within_daily_cap(ds_id):
        logger.warning(
            f"[badcase] 数据源 {datasource_code} 当日采集量已达上限 "
            f"{settings.BADCASE_DAILY_CAP}，本条跳过（防刷）"
        )
        return None

    from src.infra.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        return await BadcaseRepository(db).upsert_badcase(
            datasource_id=ds_id,
            datasource_code=datasource_code,
            source=source,
            question=question,
            resolved_question=resolved_question,
            session_id=session_id,
            user_role=user_role,
            role_params=role_params,
            predicted_sql=predicted_sql,
            error_type=error_type,
            error_message=error,
            row_count=row_count,
            sql_fix_rounds=sql_fix_rounds,
            trace_id=trace_id,
            note=note,
        )


async def capture_pipeline_result(ctx: dict, trace: dict) -> tuple[int, int] | None:
    """流水线路径采集（/query-stream）。

    ★ `trace` 是各节点返回值的并集（见 pipeline.run_pipeline）：
      有 result_sql 说明模型确实产出了 SQL —— 哪怕最终被角色规则拒绝，
      那也是有审核价值的案例；故判据是「有没有 SQL」而非「错误是否为真」。
    """
    if not trace:
        return None
    return await _capture(
        datasource_code=ctx.get("datasource_code", ""),
        source="api_error",
        question=ctx.get("raw_question") or ctx.get("question") or "",
        # 改写后的问题：多轮追问下这才是「独立可复现」的那句
        resolved_question=ctx.get("resolved_question") or ctx.get("question") or "",
        session_id=ctx.get("session_id", ""),
        user_role=ctx.get("role", ""),
        role_params={k: ctx.get(k) for k in ("dept_id", "owner_domain_id", "business_line")},
        predicted_sql=trace.get("result_sql") or trace.get("sql") or "",
        error=_extract_error(trace),
        row_count=trace.get("result_row_count") or 0,
        sql_fix_rounds=trace.get("sql_fix_rounds") or 0,
        trace_id=ctx.get("trace_id", ""),
    )


async def capture_legacy_result(
    *,
    user,
    question: str,
    datasource_code: str,
    result,
    trace_id: str = "",
) -> tuple[int, int] | None:
    """旧引擎采集（/query，一次性 JSON）。

    注意旧引擎的 result.success=False 时 result.sql 可能是「原始 SQL」（安全层
    拒绝的那条），仍值得存 —— 审核人要看的正是「模型写了什么」。
    """
    return await _capture(
        datasource_code=datasource_code,
        source="api_error",
        question=question,
        resolved_question=question,
        session_id=getattr(user, "session_id", "") or "",
        user_role=getattr(user, "role", "") or "",
        role_params={
            "dept_id": getattr(user, "dept_id", None),
            "owner_domain_id": getattr(user, "owner_domain_id", None),
            "business_line": getattr(user, "business_line", None),
        },
        predicted_sql=getattr(result, "sql", "") or "",
        error=getattr(result, "error", "") or "",
        row_count=getattr(result, "row_count", 0) or 0,
        sql_fix_rounds=0,
        trace_id=trace_id,
    )


async def safe_capture_pipeline(ctx: dict, trace: dict) -> None:
    """pipeline.py 收尾调用的入口：把一切异常（含超时）挡在主流程之外。

    ★ 为什么必须显式捕 CancelledError：客户端断连时 event_stream 的 finally
      会 cancel 掉整个流水线任务，此时本函数里的 await 会被取消。而
      CancelledError 继承自 BaseException —— `except Exception` 抓不住它，
      于是异常会穿透到 async generator 的收尾逻辑，变成
      「async generator ignored GeneratorExit」。这里把它咽掉是安全的：
      取消的语义就是「这个请求不要了」，采集本来也没必要跑完。
    """
    settings = get_settings()
    if not settings.BADCASE_CAPTURE_ENABLED:
        return
    if _is_infra_failure(trace) and not settings.BADCASE_CAPTURE_LLM_ERRORS:
        return
    try:
        await asyncio.wait_for(capture_pipeline_result(ctx, trace), _DB_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        logger.debug("[badcase] 采集随请求取消而中止（不影响查询）")
    except Exception as e:
        logger.warning(f"[badcase] 采集失败（不影响查询）: {e}")


async def safe_capture_legacy(*, user, question: str, datasource_code: str,
                              result, trace_id: str = "") -> None:
    """/query 路径的 fail-open 包装（同上，只是没有取消场景）"""
    if not get_settings().BADCASE_CAPTURE_ENABLED:
        return
    try:
        await asyncio.wait_for(
            capture_legacy_result(user=user, question=question,
                                  datasource_code=datasource_code,
                                  result=result, trace_id=trace_id),
            _DB_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.warning(f"[badcase] 采集失败（不影响查询）: {e}")
