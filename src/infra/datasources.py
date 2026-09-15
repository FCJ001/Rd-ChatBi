# ============================================================
# 多数据源路由 — 按 X-Project-Id 动态选择查询库 / 向量库 / 权限规则
#
# 用法：
#   ds = await get_datasource("rd_agent")        # 注册表配置（缓存）
#   engine = get_dw_engine("rd_agent")           # 只读 engine（缓存）
#   session = await get_dw_session("rd_agent")   # 请求级 session
# ============================================================

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.core.config import get_settings
from src.core.logger import logger
from src.infra.db import AsyncSessionLocal
from src.infra.pool import get_engine

# 进程级缓存：数据源配置一旦加载即复用（注册新数据源需重启或调用 clear）
# ★ 注意 engine **不在这里缓存**：它按 event loop 分区，缓存在 pool.py 里，
#   这里缓存会拿到别的 loop 的 engine（asyncpg 会直接报错）。
_ds_cache: dict[str, "DataSourceConfig | None"] = {}


@dataclass
class DataSourceConfig:
    """数据源注册表行（bi_datasources）的内存映射"""
    id: int
    code: str
    name: str
    dsn: str
    milvus_prefix: str = "chatbi"
    es_prefix: str = "chatbi"
    role_rules: dict[str, Any] = field(default_factory=dict)
    # 物理存在但元数据故意隐藏的敏感列：SQL 文本拦截 + 执行层结果列过滤
    sensitive_columns: list[str] = field(default_factory=list)
    description: str = ""
    enabled: bool = True


async def list_datasources() -> list[DataSourceConfig]:
    """全部启用的数据源（供 /datasources 端点与前端切换）"""
    from src.nl2sql.models import BiDatasource

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(BiDatasource).where(BiDatasource.enabled.is_(True))
        )).scalars().all()
    return [_to_config(r) for r in rows]


async def get_datasource(code: str) -> DataSourceConfig | None:
    """按编码取数据源配置（进程级缓存）"""
    if code in _ds_cache:
        return _ds_cache[code]

    from src.nl2sql.models import BiDatasource

    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(BiDatasource).where(
                BiDatasource.code == code,
                BiDatasource.enabled.is_(True),
            )
        )).scalar_one_or_none()

    config = _to_config(row) if row else None
    _ds_cache[code] = config
    if config is None:
        logger.warning(f"[datasources] 未注册或未启用的数据源: {code}")
    return config


def clear_datasource_cache() -> None:
    """注册新数据源后清缓存（测试用）"""
    _ds_cache.clear()


def get_dw_engine(code: str) -> AsyncEngine:
    """按数据源编码取只读业务库 engine（按 event loop 分区池化，见 infra/pool.py）"""
    config = _ds_cache.get(code)
    if config is None:
        # get_datasource 是 async，这里同步读缓存；未加载说明调用顺序错了
        raise RuntimeError(f"数据源 {code} 未加载（先 await get_datasource）")
    return get_engine(config.dsn, f"dw:{code}")


def dw_session_factory(code: str) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_dw_engine(code),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_dw_session(code: str) -> AsyncSession:
    """请求级只读 session（FastAPI 依赖或 pipeline ctx 用）"""
    factory = dw_session_factory(code)
    async with factory() as session:
        yield session


def _to_config(row) -> DataSourceConfig:
    return DataSourceConfig(
        id=row.id,
        code=row.code,
        name=row.name,
        dsn=row.dsn,
        milvus_prefix=row.milvus_prefix or "chatbi",
        es_prefix=row.es_prefix or "chatbi",
        role_rules=row.role_rules or {},
        # getattr：迁移前旧库没有该列时不至于整体不可用
        sensitive_columns=list(getattr(row, "sensitive_columns", None) or []),
        description=row.description or "",
        enabled=bool(row.enabled),
    )
