# ============================================================
# FastAPI 依赖：身份注入 + 数据源路由
#
# 两种认证模式（AUTH_MODE，见 .env.example）：
#   header：从 X-User-* 头取身份（由网关透传）。仅限开发/内网使用，
#           ★ 网关必须剥离外部请求携带的 X-User-* 头，否则任何人可伪造角色。
#   jwt：   验签 Authorization: Bearer <token>（HS256），身份只信 token claims。
#           生产环境推荐；JWT_SECRET 未配置时直接拒绝（fail-closed）。
#
# 调试（header 模式）：
#   curl -H "X-User-Id: 1" -H "X-User-Role: doctor" -H "X-Dept-Id: 1" \
#        -H "X-Project-Id: rd_agent" ...
# ============================================================

from dataclasses import dataclass

import jwt as pyjwt
from fastapi import Header, HTTPException

from src.core.config import get_settings


@dataclass
class UserContext:
    """
    全链路身份上下文。
    同时喂给 NL2SQL 行级过滤（role_rules 数据驱动）。
    """
    user_id: str
    session_id: str = ""
    project_id: str = "auto_full"       # 数据源编码（bi_datasources.code）；默认汽车全域百表库
    role: str = "patient"               # 角色编码，规则见 bi_datasources.role_rules
    dept_id: int | None = None          # 医院场景：doctor 所属科室
    owner_domain_id: int | None = None  # ALM 场景：engineer 所属责任域
    business_line: str | None = None    # ALM 场景：business 所属业务线


async def get_current_user(
    authorization: str = Header("", alias="Authorization"),
    x_user_id: str = Header("", alias="X-User-Id"),
    x_session_id: str = Header("", alias="X-Session-Id"),
    x_project_id: str = Header("auto_full", alias="X-Project-Id"),
    x_user_role: str = Header("patient", alias="X-User-Role"),
    x_dept_id: int | None = Header(None, alias="X-Dept-Id"),
    x_owner_domain_id: int | None = Header(None, alias="X-Owner-Domain-Id"),
    x_business_line: str | None = Header(None, alias="X-Business-Line"),
) -> UserContext:
    settings = get_settings()
    if settings.AUTH_MODE == "jwt":
        return _user_from_jwt(authorization, settings)
    return UserContext(
        user_id=x_user_id,
        session_id=x_session_id,
        project_id=x_project_id,
        role=x_user_role,
        dept_id=x_dept_id,
        owner_domain_id=x_owner_domain_id,
        business_line=x_business_line,
    )


def _user_from_jwt(authorization: str, settings) -> UserContext:
    """验签 Bearer token 并映射 claims → UserContext。
    缺 token / 过期 / 验签失败一律 401；服务未配置密钥按 503 拒绝（fail-closed）。"""
    if not settings.JWT_SECRET:
        raise HTTPException(status_code=503, detail="服务未配置 JWT_SECRET，拒绝认证")
    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="缺少认证凭证")
    try:
        claims = pyjwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            # 强制要求 exp：没有过期时间的 token 永不失效，等于给了永久凭证
            options={"require": ["exp"]},
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="凭证已过期，请重新登录")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效凭证")

    return UserContext(
        user_id=str(claims.get("sub") or claims.get("user_id") or ""),
        session_id=str(claims.get("sid", "")),
        project_id=claims.get("project_id") or "auto_full",
        role=claims.get("role", "patient"),
        dept_id=_claim_int(claims, "dept_id"),
        owner_domain_id=_claim_int(claims, "owner_domain_id"),
        business_line=claims.get("business_line"),
    )


def _claim_int(claims: dict, key: str) -> int | None:
    """claims 里的数值字段可能是数字字符串（"3"）。必须归一为 int：
    行级过滤用它生成等值条件，asyncpg 对 bigint = text 参数会直接报类型错。"""
    v = claims.get(key)
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None
