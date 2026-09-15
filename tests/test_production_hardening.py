# ============================================================
# 三项生产加固的回归测试
#
#   ① 会话历史存储（redis 跨 worker / memory 降级）
#   ② 按 event loop 分区的连接池（不再 NullPool 每请求新建）
#   ③ LLM 熔断器（closed/open/half_open + 指标接线）
#
# 用例按文件路径直接跑，仓库根由 tests/conftest.py 注入 sys.path。
# ============================================================

import asyncio
import json

import pytest
from sqlalchemy import text

from src.core.config import get_settings
from tests.conftest import requires_db

# ════════════════════════════════════════════════════════════════
# ① 会话历史存储
# ════════════════════════════════════════════════════════════════

def _qr(question="q", sql="SELECT 1", rows=3):
    from src.nl2sql.engine import QueryResult

    return QueryResult(question=question, sql=sql, row_count=rows, summary="摘要")


def test_conversation_payload_roundtrip():
    """序列化往返：question/sql/summary 必须保真（多轮改写依赖它们）"""
    from src.nl2sql.engine import ConversationContext

    ctx = ConversationContext()
    ctx.add(_qr("上个月门诊量", "SELECT count(*) FROM t", 1))
    payload = ctx.to_payload()
    back = ConversationContext.from_payload(payload)
    assert back.last_result.question == "上个月门诊量"
    assert back.last_result.sql == "SELECT count(*) FROM t"


def test_conversation_payload_excludes_row_data():
    """★ 历史里不能存结果行：每轮最多 100 行，存进 Redis 是纯浪费"""
    from src.nl2sql.engine import ConversationContext, QueryResult

    ctx = ConversationContext()
    ctx.add(QueryResult(question="q", sql="s", data=[{"a": 1}] * 100,
                        columns=["a"], summary="x"))
    payload = ctx.to_payload()
    assert "data" not in payload[0]
    assert "columns" not in payload[0]


def test_conversation_from_payload_tolerates_garbage():
    """Redis 里读到脏数据只能退化成空历史，不能抛异常把请求打挂"""
    from src.nl2sql.engine import ConversationContext

    assert ConversationContext.from_payload(None).history == []
    assert ConversationContext.from_payload("not a list").history == []
    ctx = ConversationContext.from_payload([{"question": None}, "junk", {"sql": "s"}])
    assert len(ctx.history) == 2
    assert ctx.history[0].question == ""


def test_conversation_history_capped():
    """历史只保留最近 N 轮（旧实现硬编码 10，抽成常量后语义不变）"""
    from src.nl2sql.engine import MAX_HISTORY_TURNS, ConversationContext

    ctx = ConversationContext()
    for i in range(MAX_HISTORY_TURNS + 5):
        ctx.add(_qr(f"q{i}"))
    assert len(ctx.history) == MAX_HISTORY_TURNS
    assert ctx.last_result.question == f"q{MAX_HISTORY_TURNS + 4}"


class _FakeRedisCtxStore:
    """只实现 ctx_store 用到的那几个命令"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.get_calls = 0
        self.fail = False

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.get_calls += 1
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = value
        self.ttls[key] = ex

    async def delete(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.store.pop(key, None)


@pytest.fixture
def fake_redis_ctx(monkeypatch):
    import src.infra.redis_client as rc
    from src.nl2sql import ctx_store

    fake = _FakeRedisCtxStore()

    async def _get():
        return fake

    monkeypatch.setattr(rc, "get_redis", _get)
    monkeypatch.setattr(get_settings(), "CONVERSATION_BACKEND", "redis")
    ctx_store.clear_memory_store()
    return fake


async def test_ctx_store_redis_roundtrip(fake_redis_ctx):
    """redis 后端：写一轮 → 读回来（这就是「跨 worker 共享」的最小验证）"""
    from src.nl2sql import ctx_store

    await ctx_store.add_turn("alice", "p", "s1", _qr("问题一"))
    ctx = await ctx_store.get_context("alice", "p", "s1")
    assert [r.question for r in ctx.history] == ["问题一"]


async def test_ctx_store_redis_sets_ttl(fake_redis_ctx):
    """★ 必须带 TTL：header 模式 user_id 可伪造，无 TTL 等于开放无上限写入"""
    from src.nl2sql import ctx_store

    await ctx_store.add_turn("alice", "p", "s1", _qr())
    (key, ttl), = fake_redis_ctx.ttls.items()
    assert ttl == get_settings().CONVERSATION_MAX_AGE_SECONDS
    assert key.startswith("chatbi:ctx:")


async def test_ctx_store_isolated_by_user(fake_redis_ctx):
    """同项目同 session、不同 user 必须互不可见（跨用户串数据回归）"""
    from src.nl2sql import ctx_store

    await ctx_store.add_turn("alice", "p", "default", _qr("alice 的问题"))
    await ctx_store.add_turn("bob", "p", "default", _qr("bob 的问题"))

    alice = await ctx_store.get_context("alice", "p", "default")
    assert [r.question for r in alice.history] == ["alice 的问题"]


async def test_ctx_store_clear(fake_redis_ctx):
    from src.nl2sql import ctx_store

    await ctx_store.add_turn("alice", "p", "s1", _qr())
    await ctx_store.clear("alice", "p", "s1")
    assert (await ctx_store.get_context("alice", "p", "s1")).history == []
    assert fake_redis_ctx.store == {}


async def test_ctx_store_redis_down_falls_back_to_memory(fake_redis_ctx):
    """★ fail-open：Redis 挂了退化成进程内历史，用户查询照常进行"""
    from src.nl2sql import ctx_store

    fake_redis_ctx.fail = True
    await ctx_store.add_turn("alice", "p", "s1", _qr("降级也能记"))
    ctx = await ctx_store.get_context("alice", "p", "s1")
    assert [r.question for r in ctx.history] == ["降级也能记"]


async def test_ctx_store_empty_session_not_written(fake_redis_ctx):
    """★ 只读不写：新会话不落 Redis —— 否则伪造 user_id 的请求能刷出海量空 key"""
    from src.nl2sql import ctx_store

    await ctx_store.get_context("attacker", "p", "s1")
    assert fake_redis_ctx.store == {}


# ════════════════════════════════════════════════════════════════
# ② 按 event loop 分区的连接池
# ════════════════════════════════════════════════════════════════

async def test_pool_is_real_pool_not_nullpool():
    """★ 回归：不再是 NullPool（每请求新建连接），而是真池化"""
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    from src.infra import pool

    engine = pool.get_engine(get_settings().DATABASE_URL, "test")
    assert isinstance(engine.pool, AsyncAdaptedQueuePool)
    assert pool._pool_kwargs()["pool_size"] == get_settings().DB_POOL_SIZE


async def test_pool_reused_within_same_loop():
    """同一 loop 内多次取 → 同一个 engine（这才叫池化）"""
    from src.infra import pool

    url = get_settings().DATABASE_URL
    assert pool.get_engine(url, "test") is pool.get_engine(url, "test")
    assert pool.pooled_engine_count() == 1


def test_pool_keyed_by_url_not_just_loop():
    """★ 池必须按 (loop, url) 分区，不能只按 loop。

    只按 loop 分区的话，**同一个 loop 里第一个创建的 engine 会被后续所有
    数据源复用**：先建了元数据库（rd_chatbi）的 engine，再查 hospital_demo
    就会连着 rd_chatbi 跑，报 `relation "departments" does not exist`。
    实测确认过这个故障 —— 多数据源是本项目的核心卖点，这个 bug 会让除第一个
    之外的数据源全部不可用，且只在「两个数据源都被用过」时才暴露。

    ★ 本用例只比较 engine 对象与 dsn，**不连库**，所以无需数据库即可跑。"""
    from src.infra import pool

    url_a = get_settings().DATABASE_URL
    url_b = get_settings().DEMO_DATABASE_URL
    assert url_a != url_b

    eng_a = pool.get_engine(url_a, "meta")
    eng_b = pool.get_engine(url_b, "dw")

    assert eng_a is not eng_b, "不同 url 复用了同一个 engine"
    assert eng_a.url.database != eng_b.url.database
    # 各自再取一次仍应命中自己那个
    assert pool.get_engine(url_a, "meta") is eng_a
    assert pool.get_engine(url_b, "dw") is eng_b


@requires_db
def test_pool_isolated_across_loops():
    """★ 跨 loop 必须各拿一个池：asyncpg 连接绑 loop，
    共用会抛 `Future attached to a different loop`（实测确认）

    同步用例：自己起两个 asyncio.run（这正是「两个 loop」的最小复现）。"""
    from src.infra import pool

    url = get_settings().DATABASE_URL
    results: list[int] = []

    def _run():
        async def _inner():
            # 真的连一次库，确认池可用
            async with pool.get_sessionmaker(url, "test")() as s:
                await s.execute(text("SELECT 1"))
            results.append(id(pool.get_engine(url, "test")))

        asyncio.run(_inner())

    _run()
    _run()  # 第二个 loop
    assert len(results) == 2
    assert results[0] != results[1], "两个 loop 不应共用同一个 engine"
    asyncio.run(pool.dispose_all_pools())


@requires_db
async def test_pool_works_without_running_loop_at_import():
    """★ 导入期（无运行中的 loop）也能建 engine ——
    `from src.infra.db import AsyncSessionLocal` 会真的求值这个名字。

    实测确认：建 engine 本身与 loop 无关，只有**连接**绑 loop。
    所以这里 key 用 -1，之后在真实 loop 里用也没问题。

    ★ 子进程执行，所以需要数据库 —— 没有就跳过（子进程的失败原因在
      CI 无 PG 时会是 InvalidCatalogName，与这里要锁的语义无关）。"""
    import subprocess
    import sys

    code = (
        "import asyncio\n"
        "from sqlalchemy import text\n"
        "from src.infra.db import AsyncSessionLocal\n"
        "from src.core.config import get_settings\n"
        "async def main():\n"
        "    async with AsyncSessionLocal() as s:\n"
        "        print('OK', (await s.execute(text('SELECT 1'))).scalar())\n"
        "asyncio.run(main())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=90,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK 1" in proc.stdout


# ════════════════════════════════════════════════════════════════
# ③ LLM 熔断器
# ════════════════════════════════════════════════════════════════

async def _boom():
    raise RuntimeError("LLM 挂了")


async def _ok(v=42):
    return v


def test_breaker_opens_after_threshold():
    """连续失败到阈值 → 打开；之后**不再真正调用**（快速失败）"""
    from src.core.circuit_breaker import CLOSED, OPEN, CircuitBreaker, CircuitOpenError

    br = CircuitBreaker("t", failure_threshold=3, recovery_timeout=60)
    calls = 0

    async def _counted():
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    async def _run():
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await br.call(_counted)
        assert br.state == OPEN
        # 打开后：抛 CircuitOpenError 且不再发起调用
        with pytest.raises(CircuitOpenError):
            await br.call(_counted)
        return calls

    assert asyncio.run(_run()) == 3
    assert br.state == OPEN


def test_breaker_half_open_single_probe():
    """★ 冷却后只放**一个**探针：否则恢复瞬间积压请求一起打过去把上游再压垮"""
    from src.core.circuit_breaker import HALF_OPEN, CircuitBreaker, CircuitOpenError

    br = CircuitBreaker("t", failure_threshold=1, recovery_timeout=0)

    async def _run():
        with pytest.raises(RuntimeError):
            await br.call(_boom)
        # recovery_timeout=0 → 立刻允许探针
        await br.call(_ok)      # 探针成功 → 关闭
        assert br.state == "closed"

        # 再制造一次打开，然后并发抢两个探针名额（默认 1）
        with pytest.raises(RuntimeError):
            await br.call(_boom)

        gate = asyncio.Event()

        async def _slow_probe():
            await gate.wait()
            return "probe"

        t = asyncio.ensure_future(br.call(_slow_probe))
        await asyncio.sleep(0)          # 让探针进入 half_open 并占掉名额
        assert br.state == HALF_OPEN
        with pytest.raises(CircuitOpenError):
            await br.call(_ok)          # 第二个探针被拒
        gate.set()
        assert await t == "probe"

    asyncio.run(_run())


def test_breaker_success_resets_failures():
    """偶发失败不该累积到熔断（成功要清零计数）"""
    from src.core.circuit_breaker import CLOSED, CircuitBreaker

    br = CircuitBreaker("t", failure_threshold=3, recovery_timeout=60)

    async def _run():
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await br.call(_boom)
            await br.call(_ok)
        return br.state

    assert asyncio.run(_run()) == CLOSED


def test_breaker_cancellation_is_not_failure():
    """★ 取消（客户端断连）不是故障：一次断连不该把熔断打开"""
    from src.core.circuit_breaker import CLOSED, CircuitBreaker

    br = CircuitBreaker("t", failure_threshold=2, recovery_timeout=60)

    async def _slow():
        await asyncio.sleep(10)

    async def _run():
        for _ in range(5):
            t = asyncio.ensure_future(br.call(_slow))
            await asyncio.sleep(0)
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
        return br.state, br.failure_count

    state, failures = asyncio.run(_run())
    assert state == CLOSED
    assert failures == 0


def test_breaker_metrics_wired():
    """★ 指标接线：metrics 里定义了却没人 inc 的 CIRCUIT_BREAKER_CHANGES
    现在必须真的被写入（否则告警规则永远不触发）"""
    from prometheus_client import generate_latest

    from src.core.circuit_breaker import CircuitBreaker

    br = CircuitBreaker("wired-test", failure_threshold=1, recovery_timeout=0)

    async def _run():
        with pytest.raises(RuntimeError):
            await br.call(_boom)

    asyncio.run(_run())
    body = generate_latest().decode()
    assert 'circuit_breaker_state_changes_total{state="open",target="wired-test"}' in body


def test_safe_ainvoke_records_metrics_and_opens():
    """safe_ainvoke：记录 token/调用指标，且失败会被熔断器计数"""
    from langchain_core.messages import HumanMessage, SystemMessage
    from prometheus_client import generate_latest

    from src.core import circuit_breaker as cb
    from src.nl2sql.llm_text import safe_ainvoke

    class _Resp:
        content = "SELECT 1"
        response_metadata: dict = {}

    class _FakeLLM:
        model_name = "fake-model"

        def __init__(self, fail=False):
            self.fail = fail
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            if self.fail:
                raise RuntimeError("boom")
            return _Resp()

    # 独立熔断器实例，避免污染全局单例
    br = cb.build_breaker("llm-test")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cb, "_llm_breaker", br)
    try:
        llm = _FakeLLM()
        out = asyncio.run(safe_ainvoke(llm, [SystemMessage(content="s"), HumanMessage(content="h")]))
        assert out.content == "SELECT 1"

        body = generate_latest().decode()
        assert 'llm_calls_total{model="fake-model"}' in body
        assert 'llm_tokens_total{kind="input",model="fake-model"}' in body

        # 失败路径：计数进熔断器
        bad = _FakeLLM(fail=True)
        with pytest.raises(RuntimeError):
            asyncio.run(safe_ainvoke(bad, [HumanMessage(content="h")]))
        assert br.failure_count == 1
    finally:
        monkey.undo()


def test_llm_calls_go_through_breaker():
    """★ 收口检查：流水线里不能再有裸 llm.ainvoke（漏一处就是雪崩入口）"""
    import subprocess

    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["grep", "-rn", r"llm\.ainvoke(", str(repo / "src")],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    # 唯一允许的直调点：llm_text.safe_ainvoke 内部
    others = [l for l in out if "llm_text.py" not in l]
    assert others == [], f"发现绕过熔断器的 LLM 调用: {others}"
