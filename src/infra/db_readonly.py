# ============================================================
# demo 医院运营库只读连接 — NL2SQL 查询目标
#
# 连共享 PG 的 chatbi_demo 库（demo 医院运营数据）。
# 开发期用同一用户，生产环境换成只读用户 + default_transaction_read_only=on。
# ★ engine 由 src/infra/pool.py 按 event loop 分区池化（不再是每请求新建）
# ============================================================

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.infra.pool import get_engine, get_sessionmaker

settings = get_settings()

DEMO_DATABASE_URL = settings.DEMO_DATABASE_URL


def __getattr__(name: str):
    """懒加载（同 src/infra/db.py：模块导入时通常不在 event loop 里）"""
    if name == "readonly_engine":
        return get_engine(DEMO_DATABASE_URL, "demo_readonly")
    if name == "ReadOnlySessionLocal":
        return get_sessionmaker(DEMO_DATABASE_URL, "demo_readonly")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def get_db_readonly() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：只读 session，select 直接过，写操作报错"""
    async with get_sessionmaker(DEMO_DATABASE_URL, "demo_readonly")() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
