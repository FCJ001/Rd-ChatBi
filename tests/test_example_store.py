# ============================================================
# 示例库检索语义单测（P1 主线 B）
#
# 用 fake repo / fake embedding 覆盖在线检索的三条关键语义：
#  1) 精确同题剔除（防评测自泄漏）
#  2) top_k 截断与阈值外丢弃
#  3) 依赖缺失/异常时静默降级（示例库是增强不是依赖）
# 同时锁住 validate_sql 的方言参数（P0：解析/回写用同一方言）。
# ============================================================

import pytest

from src.nl2sql.example_store import find_similar_examples
from src.nl2sql.prompts import NL2SQL_SYSTEM_PROMPT
from src.nl2sql.security import validate_sql


class FakeRepo:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search(self, query_vector, top_k=3, threshold=0.6):
        self.calls.append({"top_k": top_k, "threshold": threshold})
        return self.hits


class FakeEmbedding:
    async def aembed_documents(self, texts):
        return [[0.1, 0.2, 0.3]]


class BrokenEmbedding:
    async def aembed_documents(self, texts):
        raise RuntimeError("embedding down")


def _hit(question, score=0.9, sql="SELECT 1"):
    return {"question": question, "sql": sql, "category": "", "source": "eval", "score": score}


@pytest.mark.asyncio
async def test_exclude_exact_same_question():
    """当前问题原题必须被剔除（评测自泄漏防线）"""
    hits = [_hit("上个月和上上个月相比，充电次数变化了多少次？", 0.99),
            _hit("最近 30 天新增了多少张销售订单？", 0.85),
            _hit("近 90 天试驾次数", 0.8)]
    got = await find_similar_examples(FakeRepo(hits), FakeEmbedding(), "上个月和上上个月相比，充电次数变化了多少次？")
    assert [g["question"] for g in got] == ["最近 30 天新增了多少张销售订单？", "近 90 天试驾次数"]


@pytest.mark.asyncio
async def test_exclude_exact_ignores_punctuation_diff():
    hits = [_hit("各品牌销量多少", 0.99)]
    got = await find_similar_examples(FakeRepo(hits), FakeEmbedding(), "各品牌销量多少？")
    assert got == []


@pytest.mark.asyncio
async def test_top_k_truncation():
    hits = [_hit(f"问题{i}", 0.9 - i * 0.01) for i in range(6)]
    got = await find_similar_examples(FakeRepo(hits), FakeEmbedding(), "别的问题", top_k=2)
    assert len(got) == 2
    assert "score" not in got[0]  # 对外只保留 prompt 需要的字段


@pytest.mark.asyncio
async def test_threshold_passed_through():
    repo = FakeRepo([])
    await find_similar_examples(repo, FakeEmbedding(), "问题", top_k=3, threshold=0.75)
    # 多取一条以补偿被剔除的原题
    assert repo.calls[0] == {"top_k": 4, "threshold": 0.75}


@pytest.mark.asyncio
async def test_graceful_degradation():
    """repo/embedding 缺失或异常 → 空列表，不抛异常（绝不拖垮主链路）"""
    assert await find_similar_examples(None, FakeEmbedding(), "问题") == []
    assert await find_similar_examples(FakeRepo([]), None, "问题") == []
    assert await find_similar_examples(FakeRepo([]), FakeEmbedding(), "") == []
    assert await find_similar_examples(FakeRepo([]), BrokenEmbedding(), "问题") == []


# ══ 降级必须可观测 ═══════════════════════════════════════════════════
# 背景：这里曾是裸 `except: return []`。DashScope 欠费停服期间示例召回静默
# 归零，调用方看到的是"未命中"——**与「一切正常」在监控上完全一样**。
# 这组测试锁住「降级了必须留痕」，而不是「降级不崩」（后者上面那条已覆盖）。

def _count(channel, status) -> float:
    from src.core.metrics import RETRIEVAL_REQUESTS

    return RETRIEVAL_REQUESTS.labels(channel=channel, status=status)._value.get()


@pytest.mark.asyncio
async def test_embedding_failure_is_recorded_as_failed():
    """embedding 供应商故障 → status=failed（且**不是** ok/empty）"""
    before = _count("examples", "failed")
    assert await find_similar_examples(FakeRepo([]), BrokenEmbedding(), "问题") == []
    assert _count("examples", "failed") == before + 1


@pytest.mark.asyncio
async def test_milvus_failure_is_recorded_as_failed():
    """Milvus 故障与 embedding 故障分开记 —— 排查时要能分清是哪一侧挂了"""

    class BrokenRepo:
        def search(self, *a, **kw):
            raise RuntimeError("milvus down")

    before = _count("examples", "failed")
    assert await find_similar_examples(BrokenRepo(), FakeEmbedding(), "问题") == []
    assert _count("examples", "failed") == before + 1


@pytest.mark.asyncio
async def test_empty_result_is_recorded_as_empty_not_failed():
    """★ 正常召回但没命中（empty）与故障（failed）必须分开：
    把"没命中"记成故障会让告警天天响，把故障记成 empty 则等于没有告警。"""
    before_empty, before_failed = _count("examples", "empty"), _count("examples", "failed")
    assert await find_similar_examples(FakeRepo([]), FakeEmbedding(), "问题") == []
    assert _count("examples", "empty") == before_empty + 1
    assert _count("examples", "failed") == before_failed


@pytest.mark.asyncio
async def test_hit_is_recorded_as_ok():
    before = _count("examples", "ok")
    got = await find_similar_examples(FakeRepo([_hit("别的问题", 0.9)]), FakeEmbedding(), "问题")
    assert got
    assert _count("examples", "ok") == before + 1


def test_validate_sql_dialect_param():
    """P0：解析/回写方言跟随数据源 —— MySQL 反引号在 mysql 方言下合法且保留"""
    ok, out = validate_sql("SELECT `order_no` FROM `sal_sales_orders` LIMIT 5", dialect="mysql")
    assert ok and "`order_no`" in out
    # 同一语句用 postgres 方言解析（默认）会失败——证明方言参数确实起作用
    ok_pg, _ = validate_sql("SELECT `order_no` FROM `sal_sales_orders` LIMIT 5")
    assert not ok_pg


def test_system_prompt_dialect_substitution():
    rendered = NL2SQL_SYSTEM_PROMPT.format(schema="T(id INT)", dialect="MySQL")
    assert "MySQL" in rendered and "PostgreSQL" not in rendered
    assert "{dialect}" not in rendered and "{schema}" not in rendered


def test_dialect_forbidden_functions():
    """方言黑名单叠加：MySQL/duckdb/tsql 各自的'合法 SELECT 副作用函数'被拦，PG 黑名单不删减"""
    # mysql：LOAD_FILE / SLEEP / BENCHMARK
    for fn in ("SELECT LOAD_FILE('/etc/passwd') AS x", "SELECT SLEEP(5) AS x",
               "SELECT BENCHMARK(1e7, MD5('a')) AS x"):
        ok, msg = validate_sql(fn, dialect="mysql")
        assert not ok, fn
    # duckdb：READ_PARQUET 任意文件读
    ok, _ = validate_sql("SELECT * FROM READ_PARQUET('/etc/passwd') AS t LIMIT 1", dialect="duckdb")
    assert not ok
    # tsql：xp_cmdshell 前缀族
    ok, _ = validate_sql("SELECT XP_CMDSHELL('ls') AS x", dialect="tsql")
    assert not ok
    # ★ PG 原有黑名单在其它方言下仍生效（只叠加不删减）
    ok, _ = validate_sql("SELECT pg_read_file('/etc/passwd') AS x", dialect="mysql")
    assert not ok
    # 常规分析函数不被误伤
    ok, out = validate_sql("SELECT COUNT(*), DATE_FORMAT(NOW(), '%Y-%m') AS d FROM t", dialect="mysql")
    assert ok and "DATE_FORMAT" in out
