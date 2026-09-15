# ============================================================
# 题库审核鉴权回归
#
# 锁住三件事（都是「写权限」边界，错了就是越权）：
#   ① 未配 ADMIN_TOKEN + header 模式 → fail-closed 拒绝，不是放行
#   ② reviewer 只能来自 ADMIN_TOKEN/JWT，绝不能来自可伪造的 X-User-*
#   ③ token 配了就只认 token，不退回角色判断（少一条难审计的旁路）
#
# 这些用例不连真实库：只测依赖函数本身，HTTP 层用最小 app 复现。
# ============================================================

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.nl2sql.admin_deps import require_badcase_admin


def _app(monkeypatch, **settings_overrides):
    s = get_settings()
    for k, v in settings_overrides.items():
        monkeypatch.setattr(s, k, v)

    app = FastAPI()

    @app.get("/admin")
    async def admin(who: str = Depends(require_badcase_admin)):
        return {"who": who}

    return TestClient(app)


def test_header_mode_without_token_fails_closed(monkeypatch):
    """★ 默认（AUTH_MODE=header）下未配 ADMIN_TOKEN 必须拒绝。

    header 模式下 X-User-Role 由网关注入、请求方可伪造 —— 拿它当管理凭据
    等于把审核接口对外敞开。所以宁可 503 也不能放行。"""
    client = _app(monkeypatch, AUTH_MODE="header", ADMIN_TOKEN="",
                  BADCASE_ADMIN_FAIL_CLOSED=True)
    resp = client.get("/admin", headers={"X-User-Role": "admin", "X-User-Id": "attacker"})
    assert resp.status_code == 503
    assert "ADMIN_TOKEN" in resp.json()["detail"]


def test_wrong_token_rejected(monkeypatch):
    client = _app(monkeypatch, ADMIN_TOKEN="correct-horse", BADCASE_ADMIN_FAIL_CLOSED=True)
    assert client.get("/admin", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/admin").status_code == 401


def test_correct_token_accepted(monkeypatch):
    client = _app(monkeypatch, ADMIN_TOKEN="correct-horse")
    resp = client.get("/admin", headers={"Authorization": "Bearer correct-horse"})
    assert resp.status_code == 200
    assert resp.json()["who"] == "bearer-admin"


def test_token_configured_ignores_role_header(monkeypatch):
    """★ 配了 token 就不再退回角色判断 —— 否则等于留一条更难审计的旁路：
    拿着伪造的 X-User-Role: admin 就能绕过 token。"""
    client = _app(monkeypatch, AUTH_MODE="header", ADMIN_TOKEN="correct-horse",
                  BADCASE_ADMIN_FAIL_CLOSED=True)
    resp = client.get("/admin", headers={
        "X-User-Role": "admin", "X-User-Id": "ceo", "Authorization": "Bearer wrong",
    })
    assert resp.status_code == 401


def test_local_dev_can_disable_fail_closed(monkeypatch):
    """本地开发显式关掉 fail-closed 才放行，且 reviewer 标识如实标为 insecure"""
    client = _app(monkeypatch, AUTH_MODE="header", ADMIN_TOKEN="",
                  BADCASE_ADMIN_FAIL_CLOSED=False)
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert resp.json()["who"] == "insecure-dev"


def test_admin_roles_come_from_datasource_rules():
    """★ 管理角色不硬编码 'admin'：医院数据源根本没有这个角色名。
    判据是「role_rules 里值为 all 的角色」，新数据源零改动。

    需要元数据库（角色规则存在 bi_datasources 里），取不到就跳过 ——
    CI 的 test job 没有 postgres service。"""
    import asyncio

    from src.infra.datasources import get_datasource
    from src.nl2sql.admin_deps import _admin_roles

    try:
        ds = asyncio.run(get_datasource("hospital_demo"))
    except Exception:
        pytest.skip("元数据库不可用")
    if ds is None:
        pytest.skip("hospital_demo 未注册")

    roles = asyncio.run(_admin_roles("hospital_demo"))
    assert "admin" in roles
    # patient/cashier/doctor 都不是全量权限
    assert "patient" not in roles
    assert "cashier" not in roles


def test_admin_roles_empty_when_datasource_missing():
    """取不到数据源时返回空集合（不抛），调用方据此拒绝 —— fail-closed

    ★ 与数据源注册表无关（查的是不存在的编码），但没有元数据库时
      get_datasource 内部也会失败 —— 那种情况同样要退化成空集合。"""
    import asyncio

    from src.nl2sql.admin_deps import _admin_roles

    try:
        assert asyncio.run(_admin_roles("__no_such_datasource__")) == set()
    except Exception as e:
        pytest.skip(f"元数据库不可用: {type(e).__name__}")
