from sqlalchemy import Column, String, BigInteger, Text
from src.core.base_model import BaseModel


class Nl2sqlTable(BaseModel):
    __tablename__ = "nl2sql_tables"

    datasource_id = Column(BigInteger, nullable=False, index=True, comment="数据源ID（bi_datasources.id）")
    table_name = Column(String(100), nullable=False, comment="表名")
    role = Column(String(20), nullable=False, comment="fact / dim")
    description = Column(Text, default="", comment="表说明")
