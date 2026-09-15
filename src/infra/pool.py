# ============================================================
# 事件循环安全连接池 —— 解决「NullPool 没池化」与「asyncpg 不能跨 loop」
#
# 原实现（已知局限）：PG 连接用 NullPool 规避「asyncpg 连接绑定 event loop」，
# 代价是每请求新建 TCP + 认证往返，高并发下开销显著。
#
# 做法：**按 event loop 分区池化**。
#   同一 loop 内连接复用；不同 loop 各持一个池，互不可见。
#   生产 uvicorn 单进程只有一个常驻 loop → 等价于标准连接池；
#   测试里 pytest-asyncio 每个用例新建 loop → 各拿各的池，不会串。
#
# ★ 实测确认的边界（别照抄网上的结论）：
#   - **建 engine 本身与 loop 无关**：导入期（没有运行中的 loop）调用
#     create_async_engine(pool_size=...) 完全没问题，池类型照样是
#     AsyncAdaptedQueuePool。
#   - **连出来的连接绑 loop**：同一个连接在 loop A 用过后，拿到 loop B 用
#     会炸 `Future ... attached to a different loop`。
#   → 所以隔离的粒度是 **engine/池**，不是「延迟到有 loop 再建」。
#     按 loop id 缓存 engine 即可，不必搞懒构造那一套。
#
# ★ 为什么 key 用 id(loop)：asyncio 的事件循环对象可能被 GC 后地址复用，
#   但这里同时把 engine 存在 dict 里（强引用），旧 loop 的 engine 不会被
#   误当成新 loop 的。代价是旧 loop 的池要等 dispose_loop_pools() 才释放。
#
# 无 loop 时的 key 用 -1：保证 `from src.infra.db import AsyncSessionLocal`
# （import 期真的会求值这个名字）不炸，且拿到的 engine 对所有 loop 都未使用过。
# ============================================================

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import get_settings
from src.core.logger import logger

_NO_LOOP_KEY = -1

# (loop key, url) → (loop 身份, engine, sessionmaker)
#
# ★ key 必须含 url：多数据源下同一个 loop 里会同时用到「本服务元数据库」
#   和「各业务只读库」两类 engine。只按 loop 分区的话，**先创建的那个 engine
#   会被后续所有数据源复用** —— 表现为查 hospital_demo 却报
#   `relation "departments" does not exist`（实际连的是 rd_chatbi），
#   而且按数据源切换时好时坏，极难排查。
#
# ★ 还要存 loop 对象本身：`id(loop)` 在 loop 被回收后**会被新 loop 复用**，
#   只按 id 判断会把一个绑在死 loop 上的池当成当前 loop 的池复用。
#   存强引用既做了身份校验，也顺带阻止了「还在用的 loop 被回收」。
_pools: dict[
    tuple[int, str], tuple[object, AsyncEngine, async_sessionmaker[AsyncSession]]
] = {}


def _loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        # 没有运行中的 loop（模块导入期）
        return _NO_LOOP_KEY


def _current_loop() -> object:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _pool_kwargs() -> dict:
    """池参数来自配置。DB_POOL_SIZE<=0 时显式退回 NullPool（排障/对照用）。"""
    settings = get_settings()
    if settings.DB_POOL_SIZE <= 0:
        return {"poolclass": NullPool, "pool_pre_ping": True}
    return {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
        "pool_recycle": settings.DB_POOL_RECYCLE,
        # 池里的连接可能已被容器重启/网络抖动弄死，取用前 ping 一下
        "pool_pre_ping": True,
    }


def _entry(url: str, name: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """取（必要时创建）当前 loop + 该 url 专属的 engine + sessionmaker"""
    loop = _current_loop()
    key = (_loop_key(), url)
    entry = _pools.get(key)
    # ★ 身份校验：id 相同还必须是**同一个 loop 对象**。
    #   旧 loop 回收后 id 会被复用，只比 id 会把死池当成活的复用。
    if entry is not None and entry[0] is loop:
        return entry[1], entry[2]

    kwargs = _pool_kwargs()
    engine = create_async_engine(url, echo=get_settings().DB_ECHO, **kwargs)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    _pools[key] = (loop, engine, maker)

    where = "导入期（无 loop）" if key[0] == _NO_LOOP_KEY else f"loop {key[0]}"
    logger.info(f"[pool] 为{where}创建 engine[{name}]，参数={kwargs}")
    return engine, maker


def get_engine(url: str, name: str = "db") -> AsyncEngine:
    """当前 loop 专属的 engine（同一 loop 内复用，跨 loop 隔离）"""
    return _entry(url, name)[0]


def get_sessionmaker(url: str, name: str = "db") -> async_sessionmaker[AsyncSession]:
    """当前 loop 专属的 sessionmaker"""
    return _entry(url, name)[1]


async def session_scope(url: str, name: str = "db") -> AsyncGenerator[AsyncSession, None]:
    """async with 用的 session 上下文（提交/回滚语义由调用方决定）"""
    async with get_sessionmaker(url, name)() as session:
        yield session


def pooled_engine_count() -> int:
    """当前进程内已建立的池数量（测试/诊断用）"""
    return len(_pools)


async def _dispose_quietly(engine: AsyncEngine, key: tuple[int, str], *, loop_alive: bool) -> None:
    """释放一个池。

    ★ loop_alive 决定 close 与否：
      - True：在存活 loop 上正常 dispose（关掉池里连接，干净）
      - False：**必须 close=False**。池里的 asyncpg 连接绑在已关闭的 loop 上，
        真关它会抛 `Event loop is closed`；而 await 一个失败的操作会把
        这个「毒异常」带回来，异常甚至会在下次 GC 时重现。
        close=False 只丢引用、不关连接，由 PG 侧 TCP 超时回收。
        （测试场景连接数远小于 max_connections，可接受；生产 shutdown
        走 loop_alive=True 的路径，不会漏连接。）"""
    try:
        await engine.dispose(close=loop_alive)
    except Exception as e:
        logger.debug(f"[pool] dispose engine (loop {key[0]}, alive={loop_alive}) 失败: {e}")


def _loop_is_alive() -> bool:
    try:
        return not asyncio.get_running_loop().is_closed()
    except RuntimeError:
        return False


async def dispose_loop_pools() -> None:
    """清理**非当前** loop 遗留的池（旧 loop 已关闭，池里的连接也废了）。

    测试用例收尾时调用；生产进程只有一个常驻 loop，正常不需要。"""
    current = _loop_key()
    for key in [k for k in _pools if k[0] != current]:
        _, engine, _ = _pools.pop(key)
        await _dispose_quietly(engine, key, loop_alive=False)


async def dispose_all_pools() -> None:
    """关闭全部池（应用 shutdown 用）。

    shutdown 时 loop 还活着 → 正常 close，池里连接全部归还 OS；
    唯一例外是导入期建的 -1 池（它可能从没被任何 loop 用过，正常 close）。"""
    alive = _loop_is_alive()
    for key, (_, engine, _) in list(_pools.items()):
        _pools.pop(key, None)
        await _dispose_quietly(engine, key, loop_alive=alive and key[0] != _NO_LOOP_KEY)
