# ============================================================
# LangGraph Context — 运行时依赖注入（不参与 state 序列化）
# ============================================================

from typing import Any, Callable, TypedDict

from elasticsearch import AsyncElasticsearch
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.nl2sql.repositories import (
    PgMetaRepository,
    MilvusColumnRepository,
    MilvusMetricRepository,
    ESValueRepository,
)


class DataAgentContext(TypedDict, total=False):
    llm: BaseChatModel
    embedding_model: Embeddings
    milvus_client: MilvusClient
    milvus_column_repo: MilvusColumnRepository
    milvus_metric_repo: MilvusMetricRepository
    es_client: AsyncElasticsearch
    es_value_repo: ESValueRepository
    pg_meta_repo: PgMetaRepository
    dw_db_session: AsyncSession
    writer: Callable[[dict[str, Any]], None]  # 进度/结果推送回调
    role: str = "patient"  # 认证用户角色 → execute_sql 行级过滤
    role_rules: dict | None = None  # 数据源权限规则（bi_datasources.role_rules）
    sensitive_columns: list[str] | None = None  # 数据源敏感列：文本拦截 + 结果列过滤
    source_name: str = ""  # 数据源显示名（摘要标注用）
    dept_id: int | None = None  # 医院场景：doctor 所属科室
    owner_domain_id: int | None = None  # ALM 场景：engineer 所属责任域
    business_line: str | None = None  # ALM 场景：business 所属业务线
    # ── badcase 回流（见 src/nl2sql/badcase_store.py）────────────────
    # ★ 流水线内部自己拿不到这些：datasource_id 埋在 pg_meta_repo 的私有属性里，
    #   session_id/user_id 只存在于请求头。由 router 一次性填好，采集侧只读。
    datasource_id: int | None = None
    datasource_code: str = ""
    session_id: str = ""
    user_id: str = ""
    trace_id: str = ""
