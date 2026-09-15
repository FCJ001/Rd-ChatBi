# ============================================================
# badcase 队列的真实库回归（不是 mock —— 这一层的正确性全在 SQL 里）
#
# 最重要的两条：
#   ① upsert 去重：同一条问题反复失败只累加 seen_count，不新增行
#   ② ★ 人工成果保护：已经写过 golden_sql 的记录，后续自动采集一个字都不许改
#      —— 审核是稀缺的人力投入，被机器覆盖掉等于白干
#
# 用独立的 datasource_id（负数）隔离，收尾清理，不碰真实数据。
#
# ★ 为什么不用 async fixture 提供 session：
#   pytest.ini 里 asyncio_default_fixture_loop_scope = session，异步 fixture 跑在
#   **session 作用域**的 loop 上，而每个用例跑在自己的 function loop 上；
#   asyncpg 连接绑定 loop，跨 loop 复用会报
#   「Future attached to a different loop」。
#   所以这里用一个同步的 contextmanager，把建 session 推迟到用例体内执行
#   ——  和 tests/test_production_hardening.py 里真连库用例的写法一致。
# ============================================================

from contextlib import contextmanager

import pytest
from sqlalchemy import text

from src.core.config import get_settings

# 测试专用数据源 id，不与 bi_datasources 里的真实 id 冲突
DS_ID = -987654
DS_CODE = "test_badcase_ds"


def _db_reachable() -> bool:
    """元数据库可用（且建过 chatbi_badcases 表）才跑。

    ★ 判断「表存在」而不只是「端口通」：CI 的 eval-cases job 里 PG 在监听，
      但那些库是脚本现建的；test job 里干脆没有 PG。只看端口会在
      「服务在、库/表不在」时全部报错，而不是干净地跳过。
    ★ 用 asyncpg 直连做探测，**不走 SQLAlchemy 池**：在导入期建连接会在池里
      留下一个绑在临时 loop 上的 engine，后续 loop 可能命中这个死池。
    """
    import asyncio

    import asyncpg

    s = get_settings()

    async def _probe() -> bool:
        try:
            conn = await asyncpg.connect(
                host=s.DB_HOST, port=s.DB_PORT, user=s.DB_USER,
                password=s.DB_PASSWORD, database=s.DB_NAME, timeout=2,
            )
        except Exception:
            return False
        try:
            return await conn.fetchval("SELECT to_regclass('chatbi_badcases')") is not None
        except Exception:
            return False
        finally:
            await conn.close()

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(),
    reason="元数据库不可达（需要 DB_HOST 指向一个可用的 PostgreSQL）",
)


class _Ctx:
    def __init__(self, db):
        from src.nl2sql.repositories import BadcaseRepository

        self.db = db
        self.repo = BadcaseRepository(db)

    async def add(self, question="一共有多少条记录？", sql="SELECT 1", source="api_error",
                  error_type="db_error", error_message="查询执行失败"):
        return await self.repo.upsert_badcase(
            datasource_id=DS_ID, datasource_code=DS_CODE, source=source,
            question=question, predicted_sql=sql, error_type=error_type,
            error_message=error_message,
        )


async def _cleanup(db):
    await db.execute(text("DELETE FROM chatbi_badcases WHERE datasource_id = :d"),
                     {"d": DS_ID})
    await db.commit()


def db_ctx():
    """同步 contextmanager：进入时才建 session（跑在用例自己的 loop 上）。

    ★ 用 AsyncSessionLocal_()（每次解析成**当前 loop** 的 sessionmaker），
      而不是静态的 `from src.infra.db import AsyncSessionLocal` —— 后者在导入期
      求值、绑定 key=-1 的 engine，测试里每个用例换一个 loop 就会命中死连接。
    """
    from contextlib import asynccontextmanager

    from src.infra.db import AsyncSessionLocal_

    @asynccontextmanager
    async def _cm():
        async with AsyncSessionLocal_()() as db:
            await _cleanup(db)
            try:
                yield _Ctx(db)
            finally:
                await _cleanup(db)

    return _cm()


async def test_upsert_dedupes_by_fingerprint():
    """同一问题（仅空白/标点不同）必须合并成一行"""
    async with db_ctx() as ctx:
        id1, n1 = await ctx.add(question="一共有多少条记录？")
        id2, n2 = await ctx.add(question="  一共有多少条记录 ")
        assert id1 == id2
        assert (n1, n2) == (1, 2)


async def test_different_datasource_not_merged():
    """★ 同一句话在两个数据源是两个案例，指纹必须带 datasource_id"""
    async with db_ctx() as ctx:
        id1, _ = await ctx.add(question="有多少条记录")
        id2, n2 = await ctx.repo.upsert_badcase(
            datasource_id=-1, datasource_code="other", source="api_error",
            question="有多少条记录", predicted_sql="", error_type="db_error",
        )
        assert id1 != id2 and n2 == 1
        await ctx.db.execute(text("DELETE FROM chatbi_badcases WHERE datasource_id = -1"))
        await ctx.db.commit()


async def test_human_review_survives_recapture():
    """★★ 本文件最重要的一条：自动采集绝不能覆盖人工成果。

    场景：审核人写好 golden_sql 并 approve → 同一问题又被线上踩到
    → seen_count +1，但 golden_sql / category / difficulty / status 原封不动。"""
    from src.nl2sql.repositories import STATUS_APPROVED

    async with db_ctx() as ctx:
        cid, _ = await ctx.add()
        await ctx.repo.update_review(
            cid, status=STATUS_APPROVED,
            golden_sql="SELECT COUNT(*) AS cnt FROM alm_issues WHERE status = 'open'",
            category="单表聚合", difficulty="easy", reviewer="bearer-admin",
        )

        _, seen = await ctx.add(sql="SELECT totally_different FROM nowhere",
                                error_type="timeout", error_message="查询超时（10秒）")
        assert seen == 2

        rec = await ctx.repo.get_case(cid)
        assert rec.golden_sql == "SELECT COUNT(*) AS cnt FROM alm_issues WHERE status = 'open'"
        assert rec.category == "单表聚合"
        assert rec.difficulty == "easy"
        assert rec.status == STATUS_APPROVED
        # 机器产出的字段在已审核的行上同样不该被改
        assert rec.predicted_sql == "SELECT 1"
        assert rec.error_type == "db_error"


async def test_pending_row_refreshes_machine_fields():
    """未审核的行则相反：机器产出应当刷新成最新的现场"""
    async with db_ctx() as ctx:
        cid, _ = await ctx.add(sql="SELECT old", error_type="db_error")
        await ctx.add(sql="SELECT new", error_type="timeout")
        rec = await ctx.repo.get_case(cid)
        assert rec.predicted_sql == "SELECT new"
        assert rec.error_type == "timeout"


async def test_source_accumulates_across_capture_paths():
    """★ 同一条问题被多条采集路径命中时，来源要累积而不是被首个占住 ——
    否则另外几条路径会悄无声息地消失（看表的人以为它们没跑）"""
    async with db_ctx() as ctx:
        cid, _ = await ctx.add(source="api_error")
        await ctx.add(source="history")
        await ctx.add(source="manual")
        rec = await ctx.repo.get_case(cid)
        assert "api_error" in rec.source
        assert "history" in rec.source
        assert "manual" in rec.source


async def test_source_not_duplicated():
    """同一条路径反复命中不该写成 api_error+api_error+..."""
    async with db_ctx() as ctx:
        cid, _ = await ctx.add(source="api_error")
        await ctx.add(source="api_error")
        assert (await ctx.repo.get_case(cid)).source == "api_error"


async def test_approve_validates_inside_transaction():
    """★ approve 是进评测集的唯一闸口，校验必须在写状态之前发生，
    且失败时不能留下半截状态"""
    from src.nl2sql.badcase_store import CaseValidationError
    from src.nl2sql.repositories import STATUS_APPROVED

    async with db_ctx() as ctx:
        cid, _ = await ctx.add()
        with pytest.raises(CaseValidationError):
            await ctx.repo.update_review(cid, status=STATUS_APPROVED, golden_sql="",
                                         category="单表聚合", difficulty="easy")
        assert (await ctx.repo.get_case(cid)).status == "pending", "校验失败却改了状态"


async def test_approve_rejects_junk_metadata():
    from src.nl2sql.badcase_store import CaseValidationError
    from src.nl2sql.repositories import STATUS_APPROVED

    async with db_ctx() as ctx:
        cid, _ = await ctx.add()
        for bad in [
            dict(golden_sql="SELECT 1", category="不存在的类别", difficulty="easy"),
            dict(golden_sql="SELECT 1", category="单表聚合", difficulty="nope"),
            dict(golden_sql="DELETE FROM t", category="单表聚合", difficulty="easy"),
        ]:
            with pytest.raises(CaseValidationError):
                await ctx.repo.update_review(cid, status=STATUS_APPROVED, **bad)
        assert (await ctx.repo.get_case(cid)).status == "pending"


async def test_datasource_isolation_in_queries():
    """带 datasource_id 的仓库查不到别的数据源的行"""
    from src.nl2sql.repositories import BadcaseRepository

    async with db_ctx() as ctx:
        cid, _ = await ctx.add()
        other = BadcaseRepository(ctx.db, -1)
        assert await other.get_case(cid) is None
        assert await other.update_review(cid, status="rejected") is None
        assert (await ctx.repo.get_case(cid)).status == "pending"


async def test_stats_counts():
    from src.nl2sql.repositories import STATUS_APPROVED, BadcaseRepository

    async with db_ctx() as ctx:
        cid, _ = await ctx.add()
        await ctx.add(question="另一个问题？")
        await ctx.repo.update_review(
            cid, status=STATUS_APPROVED, golden_sql="SELECT COUNT(*) FROM alm_issues",
            category="单表聚合", difficulty="easy")

        stats = await BadcaseRepository(ctx.db, DS_ID).stats()
        assert stats["total"] == 2
        assert stats["by_status"].get(STATUS_APPROVED) == 1
        assert stats["by_status"].get("pending") == 1


async def test_daily_cap_guard_exists():
    """采集侧必须有当日上限闸门：header 模式下 user_id 可伪造，
    无上限等于对外开一个能写 PG 的口子"""
    from src.nl2sql.badcase_capture import _within_daily_cap

    s = get_settings()
    old = s.BADCASE_DAILY_CAP
    try:
        s.BADCASE_DAILY_CAP = 0        # 0 表示不限
        assert await _within_daily_cap(DS_ID) is True
    finally:
        s.BADCASE_DAILY_CAP = old
