# ============================================================
# NL2SQL 元数据配置 dataclass — 解析 conf/nl2sql_meta.yaml
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ColumnMeta:
    name: str
    type: str
    role: str  # primary_key / foreign_key / dimension / measure / date
    description: str = ""
    alias: list[str] = field(default_factory=list)
    sync: bool = False  # 是否需要从 DB 拉取枚举值写入 ES
    # 外键列的取值来源（如 owner_domain_id 的 「1→电池系统域」 映射）。
    # ★ 不填的话，sync 会去拉这一列本身的值 —— 但外键列存的是数字 ID
    #   （1/2/3…），对模型和配置行级权限的人都毫无信息量。
    #   填了 "owner_domains.name"，就把「域ID → 域名」的映射灌进 ES，
    #   键仍是本列（owner_domain_id），值取维表的可读名。
    enum_source: str | None = None


@dataclass
class TableMeta:
    name: str
    role: str  # fact / dim
    description: str = ""
    columns: list[ColumnMeta] = field(default_factory=list)


@dataclass
class MetricMeta:
    name: str
    description: str
    relevant_columns: list[str] = field(default_factory=list)
    alias: list[str] = field(default_factory=list)


@dataclass
class DatasourceMeta:
    """项目 yaml 顶部的 datasource 段 — 注册 bi_datasources 用"""
    code: str
    name: str
    dsn: str
    milvus_prefix: str = "chatbi"
    es_prefix: str = "chatbi"
    role_rules: dict = field(default_factory=dict)
    # 物理存在但元数据故意隐藏的敏感列（SQL 文本拦截 + 执行层结果列过滤）
    sensitive_columns: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class MetaConfig:
    tables: list[TableMeta] = field(default_factory=list)
    metrics: list[MetricMeta] = field(default_factory=list)
    datasource: DatasourceMeta | None = None

    @classmethod
    def from_yaml(cls, path: str) -> "MetaConfig":
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        datasource = None
        ds = raw.get("datasource")
        if ds:
            datasource = DatasourceMeta(
                code=ds["code"],
                name=ds["name"],
                dsn=ds["dsn"],
                milvus_prefix=ds.get("milvus_prefix", "chatbi"),
                es_prefix=ds.get("es_prefix", "chatbi"),
                role_rules=ds.get("role_rules") or {},
                sensitive_columns=ds.get("sensitive_columns") or [],
                description=ds.get("description", ""),
            )

        tables = []
        for t in raw.get("tables", []):
            cols = []
            for c in t.get("columns", []):
                cols.append(ColumnMeta(
                    name=c["name"],
                    type=c.get("type", ""),
                    role=c.get("role", ""),
                    description=c.get("description", ""),
                    alias=c.get("alias", []),
                    sync=c.get("sync", False),
                    enum_source=c.get("enum_source"),
                ))
            tables.append(TableMeta(
                name=t["name"],
                role=t.get("role", ""),
                description=t.get("description", ""),
                columns=cols,
            ))

        metrics = []
        for m in raw.get("metrics", []):
            metrics.append(MetricMeta(
                name=m["name"],
                description=m.get("description", ""),
                relevant_columns=m.get("relevant_columns", []),
                alias=m.get("alias", []),
            ))

        return cls(tables=tables, metrics=metrics, datasource=datasource)

    @property
    def all_columns(self) -> list[tuple[TableMeta, ColumnMeta]]:
        """展开所有列，返回 (table, column) 对"""
        result = []
        for t in self.tables:
            for c in t.columns:
                result.append((t, c))
        return result
