# ============================================================
# 数据源注册表 — 多项目接入的核心配置
#
# 每接入一个项目 = 注册一行 + 一份元数据 yaml，代码零改动。
# 详见 conf/projects/{code}.yaml 与 scripts/build_nl2sql_meta.py
# ============================================================

from sqlalchemy import JSON, Boolean, Column, String, Text

from src.core.base_model import BaseModel


class BiDatasource(BaseModel):
    __tablename__ = "bi_datasources"

    code = Column(String(50), unique=True, nullable=False, comment="项目编码，如 rd_agent / hospital_demo")
    name = Column(String(100), nullable=False, comment="项目显示名")
    dsn = Column(Text, nullable=False, comment="业务库只读连接串（postgresql+asyncpg://user:pass@host:port/db）")
    milvus_prefix = Column(String(50), nullable=False, default="chatbi", comment="Milvus collection 前缀")
    es_prefix = Column(String(50), nullable=False, default="chatbi", comment="ES index 前缀")
    role_rules = Column(JSON, nullable=True, comment="角色行级过滤规则（数据驱动，见 security.apply_role_filter）")
    sensitive_columns = Column(JSON, nullable=True, comment="敏感列名列表（物理存在但元数据隐藏）：SQL 文本拦截 + 执行层结果列过滤")
    description = Column(String(500), default="", comment="项目说明")
    enabled = Column(Boolean, nullable=False, default=True, comment="是否启用")
