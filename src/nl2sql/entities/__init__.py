# ============================================================
# NL2SQL entity dataclasses — 节点间传递的数据对象
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ColumnInfo:
    id: str  # "{table}.{column}"
    name: str
    type: str
    role: str  # pk / fk / dimension / measure / date
    description: str = ""
    alias: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)  # ES 召回或 DB 枚举值
    table_id: str = ""

    @property
    def table_name(self) -> str:
        return self.id.split(".", 1)[0]


@dataclass
class TableInfo:
    id: str
    name: str
    role: str  # fact / dim
    description: str = ""
    columns: list[ColumnInfo] = field(default_factory=list)

    def get_pk(self) -> ColumnInfo | None:
        for c in self.columns:
            if c.role == "primary_key":
                return c
        return None

    def get_fks(self) -> list[ColumnInfo]:
        return [c for c in self.columns if c.role == "foreign_key"]

    def get_key_columns(self) -> list[ColumnInfo]:
        return [c for c in self.columns if c.role in ("primary_key", "foreign_key")]


@dataclass
class MetricInfo:
    id: str
    name: str
    description: str = ""
    relevant_columns: list[str] = field(default_factory=list)
    alias: list[str] = field(default_factory=list)


@dataclass
class ValueInfo:
    id: str  # "{column_id}.{value}"
    value: str
    column_id: str
    # 这条记录从哪来：
    #   "db"    — 从业务库 SELECT DISTINCT 捞的**真实枚举值**（合法取值，可直接写进 WHERE）
    #   "alias" — YAML 里手写的**同义词**（用户会这么叫，但不是库里的值，不能当字面量写进 SQL）
    # ★ 两者语义完全不同，下游（尤其 generate_sql 的 prompt）必须能区分：
    #   把同义词当成合法值会生成查不到数据的 SQL，把真实值当同义词又会浪费上下文。
    source: str = "alias"


# 重新导出 ColumnMetric
from src.nl2sql.entities.column_metric import ColumnMetric  # noqa: E402, F401
