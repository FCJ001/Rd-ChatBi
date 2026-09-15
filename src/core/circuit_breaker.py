# ============================================================
# 熔断器 —— 三个状态：closed（正常）/ open（快速失败）/ half_open（探针）
#
# 为什么需要：LLM 供应商挂掉/限流时，每个请求都要等满 llm 的 request_timeout
# （默认 60s）才失败。9 阶段流水线里有 3~6 次 LLM 调用，一次故障会把
# 所有并发请求全部拖住、连接池占满、成本照烧。熔断后直接快速失败，
# 把「慢失败」变成「快失败」。
#
# ★ fail-fast 的失败必须让调用方**可识别**：抛 CircuitOpenError（而不是
#   混一个泛化 Exception），上层才知道这是「服务不可用」而非「这条 SQL 写错了」。
#
# ★ 半开态只放**一个**探针：不然恢复瞬间所有积压请求一起打过去，
#   故障还没好就又被压垮（雪崩的第二次）。
#
# 指标接线：CIRCUIT_BREAKER_CHANGES（core/metrics.py）在每次状态切换时 inc。
# ============================================================

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from src.core.logger import logger
from src.core.metrics import CIRCUIT_BREAKER_CHANGES

T = TypeVar("T")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """熔断打开：服务暂时不可用，请求被快速拒绝（未真正发起调用）"""


class CircuitBreaker:
    """按 target 拆分的熔断器实例（每个上游依赖一个）"""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0
        # 状态机是普通同步代码，但在协程里被并发访问（多个请求同时
        # 记录成功/失败），必须加锁 —— 不加锁会出现「两个探针同时被放行」
        self._lock = asyncio.Lock()

    # ── 状态查询 ────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failures

    def _transition(self, new_state: str) -> None:
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        CIRCUIT_BREAKER_CHANGES.labels(target=self.name, state=new_state).inc()
        logger.warning(f"[circuit_breaker] {self.name}: {old} → {new_state}")

    async def _before_call(self) -> None:
        """调用前检查。熔断打开且未到冷却期 → 直接抛（快速失败）。"""
        if self._state == OPEN:
            if time.monotonic() - self._opened_at < self.recovery_timeout:
                raise CircuitOpenError(f"{self.name} 熔断中，请稍后重试")
            async with self._lock:
                # 双重检查：可能已被其他协程切到 half_open
                if self._state == OPEN:
                    self._half_open_calls = 0
                    self._transition(HALF_OPEN)

        if self._state == HALF_OPEN:
            async with self._lock:
                if self._half_open_calls >= self.half_open_max_calls:
                    raise CircuitOpenError(f"{self.name} 半开探测中，请稍后重试")
                self._half_open_calls += 1

    async def _on_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._half_open_calls = 0
            self._transition(CLOSED)

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == HALF_OPEN:
                # 探针失败 → 立刻回到 open，重新计冷却
                self._opened_at = time.monotonic()
                self._half_open_calls = 0
                self._transition(OPEN)
            elif self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
                self._transition(OPEN)

    # ── 调用入口 ────────────────────────────────────────────────

    async def call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        await self._before_call()
        try:
            result = await fn(*args, **kwargs)
        except asyncio.CancelledError:
            # ★ 取消不是故障：客户端断连会取消整个流水线，把取消算成失败
            #   会让一次断连就把熔断打开（最坏情况下把生产打挂）。
            #   半开态的探针名额要还回去，否则名额被永久占用。
            async with self._lock:
                if self._state == HALF_OPEN and self._half_open_calls > 0:
                    self._half_open_calls -= 1
            raise
        except Exception:
            await self._on_failure()
            raise
        else:
            await self._on_success()
            return result

    async def reset(self) -> None:
        """手动复位（探活/运维接口、测试用）"""
        async with self._lock:
            self._failures = 0
            self._half_open_calls = 0
            self._opened_at = 0.0
            self._transition(CLOSED)


def build_breaker(name: str = "llm") -> CircuitBreaker:
    """按配置构造熔断器；CIRCUIT_BREAKER_ENABLED=false 时阈值拉到无穷大
    （等价于永远不熔断，保留对象不影响调用方代码路径）"""
    from src.core.config import get_settings

    s = get_settings()
    if not s.CIRCUIT_BREAKER_ENABLED:
        return CircuitBreaker(name, failure_threshold=10**9, recovery_timeout=0)
    return CircuitBreaker(
        name,
        failure_threshold=s.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_timeout=s.CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
        half_open_max_calls=s.CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS,
    )


# 模块级单例（LLM 上游一个）
_llm_breaker: CircuitBreaker | None = None


def get_llm_breaker() -> CircuitBreaker:
    global _llm_breaker
    if _llm_breaker is None:
        _llm_breaker = build_breaker("llm")
    return _llm_breaker
