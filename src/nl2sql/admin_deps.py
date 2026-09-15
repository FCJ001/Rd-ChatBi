# ============================================================
# 题库审核鉴权 —— 改评测集 = 写权限，与读指标不是一个信任级别
#
# ★ 为什么不复用 METRICS_TOKEN：能抓 Prometheus 的人不该等于能改 golden SQL。
#   一个 token 泄了，影响面从「读到内部拓扑」变成「往评测集里塞脏数据」。
#
# ★ 为什么不只看 role == "admin"：角色模型是数据源驱动的
#   （见 bi_datasources.role_rules，医院数据源根本没有 admin 这个角色），
#   而且 header 认证模式下 X-User-Role 由网关注入、请求方可伪造 ——
#   拿它当管理凭据等于把审核接口对外敞开。
#
# 两条可用路径：
#   ① ADMIN_TOKEN：Authorization: Bearer <token>（与 /metrics 同一范式，
#      compare_digest 防时序侧信道）。配置了就只认它，不退回角色判断。
#   ② jwt 模式：验签后 role 命中 role_rules 里值为 "all" 的角色。
#      ★ 不用角色名判断而用「值是否为 all」：新数据源零改动。
# ============================================================

import secrets

from fastapi import Header, HTTPException

from src.core.config import get_settings


async def _admin_roles(project_id: str) -> set[str]:
    """该数据源里拥有全量权限的角色名（role_rules 中值为 "all" 的键）"""
    from src.infra.datasources import get_datasource

    ds = await get_datasource(project_id)
    if ds is None:
        return set()
    roles = (ds.role_rules or {}).get("roles") or {}
    return {name for name, rule in roles.items() if rule == "all"}


async def require_badcase_admin(
    authorization: str = Header("", alias="Authorization"),
    x_project_id: str = Header("rd_agent", alias="X-Project-Id"),
) -> str:
    """返回审核人标识（落 reviewer 字段）。无权/未配置一律拒绝。"""
    settings = get_settings()

    if settings.ADMIN_TOKEN:
        supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        if secrets.compare_digest(supplied, settings.ADMIN_TOKEN):
            return "bearer-admin"
        # 配了 token 就只认 token：退回角色判断等于留一条更难审计的旁路
        raise HTTPException(status_code=401, detail="管理鉴权失败")

    if settings.AUTH_MODE == "jwt":
        from src.core.deps import get_current_user

        user = await get_current_user(authorization=authorization)
        if user.role in await _admin_roles(user.project_id or x_project_id):
            return f"jwt:{user.role}"
        raise HTTPException(status_code=403, detail="无权访问题库审核")

    # header 模式：X-User-* 可伪造，未配 token 时默认拒绝（fail-closed）
    if settings.BADCASE_ADMIN_FAIL_CLOSED:
        raise HTTPException(
            status_code=503,
            detail="未配置 ADMIN_TOKEN，拒绝对外提供题库审核接口",
        )
    return "insecure-dev"
