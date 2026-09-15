# ============================================================
# 生产加固回归测试
#
# 覆盖本轮修复：
#   - 敏感列两层防线（文本拦截 + 执行层结果列过滤，堵 SELECT *）
#   - 行级过滤 fail-closed（AST 注入失败必须拒绝）
#   - LLM 输出围栏剥离（lstrip 按字符集剥离的 bug 回归）
#   - 限流（memory 后端滑动窗口 + 429）
#   - JWT 认证模式（claims 映射 / 过期 / 验签失败 / 未配置密钥）
#   - 会话上下文按 user 隔离（跨用户串数据回归）
#   - Prometheus path 标签用路由模板（基数防爆炸）
#   - 危险函数 / SELECT INTO 拦截（合法 SELECT 形态的副作用）
#   - 敏感列 AST 检查（注释提及不再误伤；列引用仍然拒绝）
#   - JWT 强制 exp claim
#   - 请求体长度上限 + 会话上下文存量上限
#   - /metrics 可选 Bearer 鉴权（METRICS_TOKEN）
#   - Redis 限流 zadd 先于 zcard（并发突发超限回归）
# ============================================================

import time

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.deps import UserContext, _user_from_jwt, get_current_user
from src.nl2sql.security import apply_role_filter, filter_result_columns, validate_sql

HOSPITAL_RULES = {
    "default": "deny",
    "roles": {
        "admin": "all",
        "doctor": {"column": "department_id", "param": "dept_id"},
    },
}
SENSITIVE = ["patient_no", "patient_name", "patient_phone", "id_card"]


# ── 敏感列 · 文本层 ───────────────────────────────────────────

def test_sensitive_column_text_rejected():
    ok, msg = validate_sql(
        "SELECT patient_name FROM outpatient_visits",
        sensitive_columns=SENSITIVE,
    )
    assert not ok
    assert "敏感" in msg


def test_sensitive_column_case_insensitive():
    ok, _ = validate_sql(
        'SELECT "PATIENT_NAME" FROM outpatient_visits',
        sensitive_columns=SENSITIVE,
    )
    assert not ok


def test_without_sensitive_config_still_valid():
    """未配置敏感列的数据源：合法查询不受影响（向后兼容）"""
    ok, _ = validate_sql(
        "SELECT patient_name FROM outpatient_visits",
        sensitive_columns=None,
    )
    assert ok


# ── 敏感列 · 执行层（SELECT * 防线）──────────────────────────

def test_filter_result_columns_drops_sensitive():
    """SELECT * 返回的行/列里，敏感列必须被剔除（摘要 LLM 之前执行）"""
    columns = ["id", "visit_no", "patient_name", "patient_phone", "status"]
    rows = [
        {"id": 1, "visit_no": "V1", "patient_name": "张三",
         "patient_phone": "13800000000", "status": "paid"},
    ]
    kept, filtered = filter_result_columns(columns, rows, SENSITIVE)
    assert kept == ["id", "visit_no", "status"]
    assert filtered[0] == {"id": 1, "visit_no": "V1", "status": "paid"}


def test_filter_result_columns_noop_when_clean():
    columns = ["id", "status"]
    rows = [{"id": 1, "status": "paid"}]
    kept, filtered = filter_result_columns(columns, rows, SENSITIVE)
    assert kept == columns
    assert filtered == rows


def test_filter_result_columns_empty_config():
    assert filter_result_columns(["a"], [{"a": 1}], []) == (["a"], [{"a": 1}])


# ── 行级过滤 fail-closed ─────────────────────────────────────

def test_role_filter_rejects_unparseable_sql():
    """AST 注入失败必须 fail-closed 拒绝，绝不允许降级字符串拼接或原样放行"""
    ok, msg = apply_role_filter(
        "SELECT ( FROM (", role="doctor",
        role_rules=HOSPITAL_RULES, params={"dept_id": 7},
    )
    assert not ok
    assert "拒绝" in msg


def test_role_filter_rejects_malformed_rule():
    """规则配置不完整（漏 param/value、空 dict）必须 fail-closed——
    配置笔误不能等价于权限全开（旧行为是静默放行整库）"""
    for bad_rule in (
        {"column": "department_id"},            # 漏 param
        {"param": "dept_id"},                    # 漏 column
        {"value": ""},                           # 空 value
        {},                                      # 空规则
    ):
        rules = {"default": "deny", "roles": {"doctor": bad_rule}}
        ok, msg = apply_role_filter(
            "SELECT id FROM outpatient_visits", role="doctor",
            role_rules=rules, params={"dept_id": 7},
        )
        assert not ok, f"坏规则应拒绝: {bad_rule}"
        assert "拒绝" in msg


# ── LLM 输出围栏剥离 ─────────────────────────────────────────

def test_strip_code_fence_lang_tag():
    from src.nl2sql.llm_text import strip_code_fence
    assert strip_code_fence("```sql\nSELECT 1\n```") == "SELECT 1"


def test_strip_code_fence_lowercase_sql_no_tag_regression():
    """lstrip("sql") 回归：```select ... 之前会被剥成 elect ..."""
    from src.nl2sql.llm_text import strip_code_fence
    assert strip_code_fence("```select count(*) from t```") == "select count(*) from t"


def test_strip_code_fence_json_null_regression():
    """lstrip("json") 回归：null 之前会被剥成 ull"""
    from src.nl2sql.llm_text import strip_code_fence
    assert strip_code_fence("```null```") == "null"


def test_strip_code_fence_no_lang_tag():
    from src.nl2sql.llm_text import strip_code_fence
    assert strip_code_fence("```\nSELECT 1\n```") == "SELECT 1"


def test_strip_code_fence_no_fence():
    from src.nl2sql.llm_text import strip_code_fence
    assert strip_code_fence("  SELECT 1  ") == "SELECT 1"


# ── 限流 ─────────────────────────────────────────────────────

def test_memory_sliding_window():
    from src.core.rate_limit import _check_memory
    key = f"test-{time.time()}"
    assert _check_memory(key, window=60, max_requests=2)
    assert _check_memory(key, window=60, max_requests=2)
    assert not _check_memory(key, window=60, max_requests=2)


def test_memory_window_slides():
    from src.core.rate_limit import _check_memory
    key = f"test-slide-{time.time()}"
    window = 0.05
    assert _check_memory(key, window=window, max_requests=1)
    assert not _check_memory(key, window=window, max_requests=1)
    time.sleep(0.06)  # 窗口滑过
    assert _check_memory(key, window=window, max_requests=1)


def test_enforce_rate_limit_429(monkeypatch):
    from fastapi import APIRouter
    from src.core.rate_limit import enforce_rate_limit

    s = get_settings()
    monkeypatch.setattr(s, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(s, "RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setattr(s, "RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(s, "RATE_LIMIT_WINDOW_SECONDS", 60)
    from src.core import rate_limit as rl
    rl._memory_buckets.clear()

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="rl-user")

    @app.post("/q", dependencies=[Depends(enforce_rate_limit)])
    async def q():
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/q").status_code == 200
    resp = client.post("/q")
    assert resp.status_code == 429


def test_rate_limit_disabled_passes(monkeypatch):
    from src.core.rate_limit import enforce_rate_limit

    s = get_settings()
    monkeypatch.setattr(s, "RATE_LIMIT_ENABLED", False)

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="rl-off")

    @app.post("/q", dependencies=[Depends(enforce_rate_limit)])
    async def q():
        return {"ok": True}

    client = TestClient(app)
    for _ in range(3):
        assert client.post("/q").status_code == 200


# ── JWT 认证模式 ─────────────────────────────────────────────

def _jwt_settings(monkeypatch, secret="test-secret"):
    s = get_settings()
    monkeypatch.setattr(s, "AUTH_MODE", "jwt")
    monkeypatch.setattr(s, "JWT_SECRET", secret)
    monkeypatch.setattr(s, "JWT_ALGORITHM", "HS256")
    return s


def _jwt_claims(**extra) -> dict:
    """带 exp 的基础 claims（exp 已强制要求，测试 token 一律带上）"""
    return {"exp": int(time.time()) + 300, **extra}


def test_jwt_valid_claims_mapped(monkeypatch):
    s = _jwt_settings(monkeypatch)
    token = pyjwt.encode(
        _jwt_claims(sub="42", role="doctor", dept_id=3, project_id="hospital_demo"),
        "test-secret", algorithm="HS256",
    )
    user = _user_from_jwt(f"Bearer {token}", s)
    assert user.user_id == "42"
    assert user.role == "doctor"
    assert user.dept_id == 3
    assert user.project_id == "hospital_demo"


def test_jwt_numeric_string_claims_coerced(monkeypatch):
    """claims 里 dept_id="3"（数字字符串）必须归一为 int——
    行级过滤拿它生成 bigint 等值条件，asyncpg 对 text 参数直接报类型错"""
    s = _jwt_settings(monkeypatch)
    token = pyjwt.encode(
        _jwt_claims(sub="42", dept_id="3", owner_domain_id="7"),
        "test-secret", algorithm="HS256",
    )
    user = _user_from_jwt(f"Bearer {token}", s)
    assert user.dept_id == 3
    assert user.owner_domain_id == 7


def test_jwt_garbage_numeric_claim_is_none(monkeypatch):
    s = _jwt_settings(monkeypatch)
    token = pyjwt.encode(_jwt_claims(sub="42", dept_id="abc"), "test-secret", algorithm="HS256")
    user = _user_from_jwt(f"Bearer {token}", s)
    assert user.dept_id is None


def test_jwt_expired_rejected(monkeypatch):
    s = _jwt_settings(monkeypatch)
    token = pyjwt.encode(
        {"sub": "42", "exp": int(time.time()) - 100},
        "test-secret", algorithm="HS256",
    )
    with pytest.raises(Exception) as ei:
        _user_from_jwt(f"Bearer {token}", s)
    assert getattr(ei.value, "status_code", None) == 401


def test_jwt_bad_signature_rejected(monkeypatch):
    s = _jwt_settings(monkeypatch, secret="right-secret")
    token = pyjwt.encode({"sub": "42"}, "wrong-secret", algorithm="HS256")
    with pytest.raises(Exception) as ei:
        _user_from_jwt(f"Bearer {token}", s)
    assert getattr(ei.value, "status_code", None) == 401


def test_jwt_missing_token_rejected(monkeypatch):
    s = _jwt_settings(monkeypatch)
    with pytest.raises(Exception) as ei:
        _user_from_jwt("", s)
    assert getattr(ei.value, "status_code", None) == 401


def test_jwt_unconfigured_secret_fails_closed(monkeypatch):
    """JWT 模式但没配密钥：拒绝服务而不是放行"""
    s = _jwt_settings(monkeypatch, secret="")
    with pytest.raises(Exception) as ei:
        _user_from_jwt("Bearer abc", s)
    assert getattr(ei.value, "status_code", None) == 503


# ── 会话上下文 user 隔离 ─────────────────────────────────────

def test_ctx_key_isolated_by_user():
    """同项目同 session_id，不同用户必须落到不同上下文（跨用户串数据回归）"""
    from src.nl2sql.ctx_store import _ctx_key
    assert _ctx_key("alice", "p", "default") != _ctx_key("bob", "p", "default")
    # session_id 是用户输入且默认 "default"，project 不同也必须隔离
    assert _ctx_key("alice", "p1", "default") != _ctx_key("alice", "p2", "default")


# ── 危险函数 / SELECT INTO（合法 SELECT 形态的副作用）─────────

def test_dangerous_functions_rejected():
    """pg_read_file/set_config/dblink/lo_* 等都是语句级 SELECT-only 拦不住的
    副作用函数；set_config 甚至能尝试关掉 default_transaction_read_only"""
    from src.nl2sql.security import validate_sql
    for bad in [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_read_binary_file('/etc/passwd')",
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        "SELECT pg_sleep(100)",
        "SELECT dblink_connect('host=evil')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT pg_advisory_lock(123)",
        "SELECT * FROM t WHERE pg_ls_dir('/') IS NOT NULL",
    ]:
        ok, msg = validate_sql(bad)
        assert not ok, f"{bad} 应被拦截"
        assert "函数" in msg


def test_safe_builtin_functions_still_allowed():
    """黑名单不得误伤常规聚合/日期/条件函数"""
    from src.nl2sql.security import validate_sql
    ok, _ = validate_sql(
        "SELECT department_id, COUNT(*), SUM(registration_fee), "
        "COALESCE(AVG(stay_days), 0), DATE_TRUNC('month', visit_date) "
        "FROM outpatient_visits GROUP BY department_id"
    )
    assert ok


def test_select_into_rejected():
    """SELECT * INTO new_t 会建表，不属于只读查询"""
    from src.nl2sql.security import validate_sql
    ok, msg = validate_sql("SELECT * INTO new_t FROM outpatient_visits")
    assert not ok
    assert "SELECT" in msg


def test_dangerous_function_in_subquery_rejected():
    from src.nl2sql.security import validate_sql
    ok, _ = validate_sql("SELECT * FROM (SELECT set_config('a','b',true)) AS sub")
    assert not ok


# ── 敏感列 AST 检查（文本层的精化）───────────────────────────

def test_sensitive_column_in_comment_not_false_positive():
    """注释里提到敏感列名不应误伤合法查询（旧文本层会拒）"""
    from src.nl2sql.security import validate_sql
    ok, _ = validate_sql(
        "SELECT id FROM outpatient_visits -- patient_name 已在元数据层裁剪",
        sensitive_columns=SENSITIVE,
    )
    assert ok


def test_forbidden_pattern_in_comment_not_false_positive():
    from src.nl2sql.security import validate_sql
    ok, _ = validate_sql("SELECT id FROM departments -- 不要 DROP 这张表")
    assert ok


def test_sensitive_column_block_comment_between_tokens():
    """块注释分隔 token 不影响敏感列拦截（PG 里注释可出现在任意 token 间）"""
    from src.nl2sql.security import validate_sql
    ok, _ = validate_sql(
        "SELECT /**/ patient_name /**/ FROM outpatient_visits",
        sensitive_columns=SENSITIVE,
    )
    assert not ok


def test_sensitive_column_alias_with_comment_still_blocked():
    """注释混淆 + 别名组合：文本层可能漏，AST 层必须拦住
    （结果列名叫 n，执行层 filter_result_columns 也认不出它）"""
    from src.nl2sql.security import validate_sql
    ok, _ = validate_sql(
        "SELECT patient_name /**/ AS n FROM outpatient_visits",
        sensitive_columns=SENSITIVE,
    )
    assert not ok


# ── JWT 强制 exp ─────────────────────────────────────────────

def test_jwt_missing_exp_rejected(monkeypatch):
    """无 exp 的 token 永不过期，等于永久凭证 —— 必须拒绝"""
    s = _jwt_settings(monkeypatch)
    token = pyjwt.encode({"sub": "42"}, "test-secret", algorithm="HS256")
    with pytest.raises(Exception) as ei:
        _user_from_jwt(f"Bearer {token}", s)
    assert getattr(ei.value, "status_code", None) == 401


# ── 请求体长度上限 ────────────────────────────────────────────

def test_request_length_capped():
    """question 直接进 prompt、session_id 是内存 key 的一部分，必须限长"""
    import pydantic
    from src.nl2sql.router import BIQueryRequest

    with pytest.raises(pydantic.ValidationError):
        BIQueryRequest(question="查" * 2001)
    with pytest.raises(pydantic.ValidationError):
        BIQueryRequest(question="ok", session_id="s" * 129)
    assert BIQueryRequest(question="查" * 2000).question == "查" * 2000


# ── 会话上下文存量上限 ────────────────────────────────────────

def test_ctx_store_evicts_when_full(monkeypatch):
    """header 模式 user_id 可伪造，ctx 存量必须有上限（防内存打爆）

    迁移到 ctx_store 后（支持 redis backend）上限语义不变：memory 后端
    超限时淘汰最早的一半。"""
    from src.nl2sql import ctx_store

    monkeypatch.setattr(ctx_store, "_MEMORY_STORE_CAP", 4)
    ctx_store.clear_memory_store()

    for i in range(6):
        ctx_store._memory_get(f"u{i}:p:default")

    assert ctx_store.memory_store_size() <= 4


# ── 限流桶逐出策略 ───────────────────────────────────────────

def test_memory_bucket_eviction_sweeps_expired_only(monkeypatch):
    """桶满时先清过期桶；活跃桶保留 —— 全清会把所有人的限流计数清零"""
    from src.core import rate_limit as rl

    monkeypatch.setattr(rl, "_MEMORY_BUCKET_CAP", 3)
    rl._memory_buckets.clear()
    try:
        from collections import deque

        now = time.perf_counter()
        stale_key, active_key, active_key2 = "evict-stale", "evict-active", "evict-active2"
        rl._memory_buckets[stale_key] = deque([now - 999])
        rl._memory_buckets[active_key] = deque([now])
        rl._memory_buckets[active_key2] = deque([now])
        # 已有 3 桶触顶，新 key 触发逐出：过期桶被清，活跃桶幸存
        assert rl._check_memory("evict-new", window=60, max_requests=5)
        assert stale_key not in rl._memory_buckets
        assert active_key in rl._memory_buckets
        assert active_key2 in rl._memory_buckets
        assert "evict-new" in rl._memory_buckets
    finally:
        rl._memory_buckets.clear()


# ── /metrics 鉴权 ────────────────────────────────────────────

def test_metrics_token_enforced(monkeypatch):
    """METRICS_TOKEN 配置后：无凭证/错凭证 401，正确 Bearer 放行；
    未配置保持开放（向后兼容，内网抓取零配置）"""
    from src.main import app

    s = get_settings()
    monkeypatch.setattr(s, "METRICS_TOKEN", "scraper-secret")
    client = TestClient(app)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer scraper-secret"}).status_code == 200

    monkeypatch.setattr(s, "METRICS_TOKEN", "")
    assert client.get("/metrics").status_code == 200


# ── Redis 限流原子顺序 ───────────────────────────────────────

def test_check_redis_adds_before_counts(monkeypatch):
    """并发修复回归：zadd 必须先于 zcard（同一 MULTI/EXEC 内）。
    旧顺序先 zcard 后 zadd，N 个并发请求都看到旧计数，集体放行超限。"""
    import asyncio

    import src.infra.redis_client as rc
    from src.core.rate_limit import _check_redis

    cmds: list[str] = []

    class FakePipe:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def zremrangebyscore(self, *a):
            cmds.append("zremrangebyscore")

        def zadd(self, *a, **k):
            cmds.append("zadd")

        def zcard(self, *a):
            cmds.append("zcard")

        def expire(self, *a, **k):
            cmds.append("expire")

        async def execute(self):
            return [0, 1, 5, True]  # zcard=5 > max 4 → 拒绝

    class FakeRedis:
        def pipeline(self, transaction=True):
            assert transaction is True, "必须 MULTI/EXEC，否则命令会被其他请求交错"
            cmds.append("pipeline")
            return FakePipe()

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(rc, "get_redis", fake_get_redis)
    allowed = asyncio.run(_check_redis("k", window=60, max_requests=4))
    assert allowed is False
    assert cmds.index("zadd") < cmds.index("zcard")


# ── Prometheus path 标签 ─────────────────────────────────────

def test_metric_path_uses_route_template():
    """path 标签必须是路由模板，参数化的实际 URL 不进指标（基数防爆炸）"""
    from prometheus_client import generate_latest

    from src.core.metrics import PrometheusMiddleware

    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)

    @app.get("/_metric_probe/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    TestClient(app).get("/_metric_probe/3")
    body = generate_latest().decode()
    assert 'path="/_metric_probe/{item_id}"' in body
    assert 'path="/_metric_probe/3"' not in body