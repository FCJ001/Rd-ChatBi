# ============================================================
# PostgreSQL 异步连接 — 本服务自有库 rd_chatbi（NL2SQL 元数据）
#
# 业务代码全走这里的 get_db()。
# ★ engine 由 src/infra/pool.py 按 event loop 分区池化：
#   同一 loop 内复用连接（不再是 NullPool 的每请求新建），
#   跨 loop 隔离以满足 asyncpg「连接绑定 event loop」的约束。
# ★ pool_pre_ping=True 必须开：容器重启后连接池里的旧连接是死的。
# ============================================================

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import get_settings
from src.infra.pool import get_engine, get_sessionmaker

settings = get_settings()

DATABASE_URL = settings.DATABASE_URL


def __getattr__(name: str):
    """懒加载 engine / sessionmaker。

    模块导入时通常还没跑在 event loop 里（pool.get_engine 需要
    get_running_loop()），所以不能在这里直接建 engine。
    保留 `from src.infra.db import AsyncSessionLocal` 这个老用法可行 ——
    首次属性访问时才解析（此时必然在协程里）。
    """
    if name == "engine":
        return get_engine(DATABASE_URL, "meta")
    if name == "AsyncSessionLocal":
        return get_sessionmaker(DATABASE_URL, "meta")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def AsyncSessionLocal_() -> async_sessionmaker[AsyncSession]:
    """显式取 sessionmaker（不依赖模块 __getattr__ 的场景）"""
    return get_sessionmaker(DATABASE_URL, "meta")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：一个请求一个 session，正常结束提交，异常回滚"""
    async with get_sessionmaker(DATABASE_URL, "meta")() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
