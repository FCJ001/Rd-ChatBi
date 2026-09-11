# ============================================================
# LangGraph astream × FastAPI StreamingResponse 回归测试
#
# 背景：pipeline.py 曾注释「不使用 LangGraph astream，因为它在
# FastAPI StreamingResponse 里会 hang」。经实测（langgraph 1.1.6 +
# starlette 1.6.0）该问题不存在——astream 是普通 async generator，
# 可直接由 StreamingResponse 在请求事件循环上消费。
# 真正的坑是 LangGraph 1.x 不再给节点注入第二位置参数 ctx，直接
# 注册 (state, ctx) 签名的节点会 TypeError，当年被误判为 hang。
#
# 本文件锁定两点：
#   1. 并行图 astream 经真实 ASGI 往返流出全部事件（防 hang 回归）
#   2. 项目真实 graph.py 的结构与条件路由正确
# ============================================================

import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

STREAM_TIMEOUT = 30.0  # 秒；超过即视为 hang


# ── 1. astream 在 StreamingResponse 中不 hang ────────────────────

class St(TypedDict, total=False):
    q: str
    v: int
    a: dict
    b: dict
    error: str
    done: bool


class Ctx(TypedDict, total=False):
    tag: str


async def _n_start(state: St, runtime) -> dict:
    assert dict(runtime.context) == {"tag": "req-1"}, "context 未透传到节点"
    await asyncio.sleep(0.02)
    return {"v": 1}


async def _n_branch_a(state: St) -> dict:
    await asyncio.sleep(0.05)
    return {"a": {"ok": True}}


async def _n_branch_b(state: St) -> dict:
    await asyncio.sleep(0.01)
    return {"b": {"ok": True}}


async def _n_join(state: St) -> dict:
    return {"error": ""}


async def _n_execute(state: St) -> dict:
    return {"done": True}


def _build_fake_graph():
    g = StateGraph(St, context_schema=Ctx)
    g.add_node("start", _n_start)
    g.add_node("branch_a", _n_branch_a)
    g.add_node("branch_b", _n_branch_b)
    g.add_node("join", _n_join)
    g.add_node("execute", _n_execute)
    g.add_edge(START, "start")
    g.add_edge("start", "branch_a")
    g.add_edge("start", "branch_b")
    g.add_edge("branch_a", "join")
    g.add_edge("branch_b", "join")
    g.add_edge("join", "execute")
    g.add_edge("execute", END)
    return g.compile()


@pytest.fixture
def fake_app():
    graph = _build_fake_graph()
    app = FastAPI()

    @app.post("/stream")
    async def stream():
        async def gen():
            async for ev in graph.astream(
                {"q": "hello"}, context={"tag": "req-1"}, stream_mode="updates"
            ):
                yield f"data: {json.dumps(ev, default=str)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


async def test_astream_streams_through_streaming_response(fake_app):
    """并行图 astream 经真实 ASGI 往返流出全部节点事件（hang 回归）。"""
    t0 = time.perf_counter()
    events = []

    async def consume():
        async with AsyncClient(
            transport=ASGITransport(app=fake_app), base_url="http://t"
        ) as client:
            async with client.stream("POST", "/stream") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        events.append(json.loads(line[5:]))

    await asyncio.wait_for(consume(), timeout=STREAM_TIMEOUT)

    names = [next(iter(ev)) for ev in events]
    assert names == ["start", "branch_b", "branch_a", "join", "execute"]
    # 并行分支各自独立出事件，返回值即 updates
    assert events[1] == {"branch_b": {"b": {"ok": True}}}
    assert events[2] == {"branch_a": {"a": {"ok": True}}}
    assert time.perf_counter() - t0 < STREAM_TIMEOUT


async def test_client_disconnect_midstream_cleans_up(fake_app):
    """客户端读到一半断开：astream 被取消后不留悬挂任务，事件循环仍健康。

    这是「astream 在 StreamingResponse 里 hang」观感的经典来源——
    若取消后残留节点任务或锁，下一个请求会卡死或 loop 关闭报错。"""
    async with AsyncClient(
        transport=ASGITransport(app=fake_app), base_url="http://t"
    ) as client:
        async with client.stream("POST", "/stream") as resp:
            seen = 0
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    seen += 1
                if seen >= 2:
                    break  # 提前断开，触发响应侧取消
            assert seen == 2

        # 断开后同一 loop 再来一个完整请求，必须照常流完
        events = []
        async with client.stream("POST", "/stream") as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
        assert [next(iter(ev)) for ev in events] == [
            "start", "branch_b", "branch_a", "join", "execute",
        ]


# ── 2. 项目真实图：结构与条件路由 ─────────────────────────────────

EXPECTED_NODES = {
    "extract_keywords",
    "recall_columns",
    "recall_values",
    "recall_metrics",
    "merge_info",
    "filter_tables",
    "filter_metrics",
    "add_context",
    "generate_sql",
    "validate_sql",
    "correct_sql",
    "execute_sql",
}


def test_graph_structure():
    from src.nl2sql.graph import graph

    assert EXPECTED_NODES <= set(graph.get_graph().nodes)
    edges = {e for e in graph.builder.edges if isinstance(e[0], str) and isinstance(e[1], str)}
    # ① → 三路并行召回
    for name in ("recall_columns", "recall_values", "recall_metrics"):
        assert ("extract_keywords", name) in edges
        assert (name, "merge_info") in edges
    # ③ → 两路并行过滤
    for name in ("filter_tables", "filter_metrics"):
        assert ("merge_info", name) in edges
        assert (name, "add_context") in edges
    # ⑤⑥⑦ 主干；★ 纠错产物必须回 validate_sql 复检，不允许直通执行
    assert ("add_context", "generate_sql") in edges
    assert ("generate_sql", "validate_sql") in edges
    assert ("correct_sql", "validate_sql") in edges
    assert ("correct_sql", "execute_sql") not in edges
    assert ("execute_sql", END) in edges


def test_route_after_validate():
    from src.nl2sql.graph import MAX_SQL_FIX_ROUNDS, _route_after_validate

    assert _route_after_validate({"error": "syntax error at ..."}) == "correct"
    assert _route_after_validate({"error": None}) == "ok"
    assert _route_after_validate({}) == "ok"
    # 纠错预算用尽仍报错 → 拒绝执行（结束），而不是带错执行
    assert _route_after_validate(
        {"error": "x", "sql_fix_rounds": MAX_SQL_FIX_ROUNDS}
    ) == "__end__"


async def test_run_pipeline_is_async_generator():
    from inspect import isasyncgenfunction

    from src.nl2sql.pipeline import run_pipeline

    assert isasyncgenfunction(run_pipeline)
