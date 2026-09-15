# ============================================================
# pytest 全局 fixture
#
# 1. 把仓库根加进 sys.path（有测试是直接按文件路径跑的）
# 2. 每个用例收尾清理「已关闭 loop」遗留的数据库连接池 ——
#    src/infra/pool.py 按 (loop, dsn) 分区池化，pytest-asyncio 每个用例新建
#    loop，不清理的话每个用例都会留一个池。
# 3. 提供「测试库可用」的判定：CI 的 test job 没有 postgres service，
#    真连库的用例必须能干净跳过，否则 pytest 非零退出、后面的离线评测门禁
#    那一步根本不会执行（step 失败即中止 job）—— 门禁形同虚设。
# ============================================================

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
async def _cleanup_pools():
    from src.infra import pool

    await pool.dispose_loop_pools()
    yield
    await pool.dispose_loop_pools()


def db_available() -> bool:
    """元数据库可用（端口通 + 库存在）才为 True。

    ★ 用 asyncpg 直连探测，**不走 SQLAlchemy 池**：导入期建连接会在池里留下
      一个绑在临时 loop 上的 engine，后续 loop 可能命中这个死池，报
      「Future attached to a different loop」——探测本身会污染被测对象。
    ★ 判「库存在」而不只是「端口通」：CI 的 eval-cases job 里 PG 在监听，
      但那些库是脚本现建的；test job 里干脆没有 PG。
    """
    import asyncio

    import asyncpg

    from src.core.config import get_settings

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
            await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False
        finally:
            await conn.close()

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not db_available(),
    reason="元数据库不可用（CI 的 test job 无 postgres service）",
)
