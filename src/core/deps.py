# ============================================================
# FastAPI 依赖：身份注入 + 数据源路由
#
# 开发期：直接从 HTTP header 取身份信息（由网关透传）。
# 生产环境：验 JWT 后解 claims，替换 get_current_user 函数体即可。
#
# 调试：
#   curl -H "X-User-Id: 1" -H "X-User-Role: doctor" -H "X-Dept-Id: 1" \
#        -H "X-Project-Id: hospital_demo" ...
# ============================================================

from dataclasses import dataclass

from fastapi import Header


@dataclass
class UserContext:
    """
    全链路身份上下文。
    同时喂给 NL2SQL 行级过滤（role_rules 数据驱动）。
    """
    user_id: str
    session_id: str = ""
    project_id: str = "hospital_demo"   # 数据源编码（bi_datasources.code）
    role: str = "patient"               # 角色编码，规则见 bi_datasources.role_rules
    dept_id: int | None = None          # 医院场景：doctor 所属科室
    owner_domain_id: int | None = None  # ALM 场景：engineer 所属责任域
    business_line: str | None = None    # ALM 场景：business 所属业务线


async def get_current_user(
    x_user_id: str = Header("", alias="X-User-Id"),
    x_session_id: str = Header("", alias="X-Session-Id"),
    x_project_id: str = Header("hospital_demo", alias="X-Project-Id"),
    x_user_role: str = Header("patient", alias="X-User-Role"),
    x_dept_id: int | None = Header(None, alias="X-Dept-Id"),
    x_owner_domain_id: int | None = Header(None, alias="X-Owner-Domain-Id"),
    x_business_line: str | None = Header(None, alias="X-Business-Line"),
) -> UserContext:
    """
    开发期实现：直接从 header 取身份，不查表不验签。
    真实环境替换为 verify_jwt(token) 解 claims。
    """
    return UserContext(
        user_id=x_user_id,
        session_id=x_session_id,
        project_id=x_project_id,
        role=x_user_role,
        dept_id=x_dept_id,
        owner_domain_id=x_owner_domain_id,
        business_line=x_business_line,
    )
