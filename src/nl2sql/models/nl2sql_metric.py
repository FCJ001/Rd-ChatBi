from sqlalchemy import BigInteger, Column, String, Text, JSON
from src.core.base_model import BaseModel


class Nl2sqlMetric(BaseModel):
    __tablename__ = "nl2sql_metrics"

    datasource_id = Column(BigInteger, nullable=False, index=True, comment="数据源ID（bi_datasources.id）")
    metric_name = Column(String(200), nullable=False, comment="指标名称")
    description = Column(Text, default="", comment="指标说明")
    relevant_columns = Column(JSON, default=list, comment="关联列")
    aliases = Column(JSON, default=list, comment="别名列表")
