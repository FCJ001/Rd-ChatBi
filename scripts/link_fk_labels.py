# ============================================================
# 外键枚举值与可读名的关联修复
#
# 问题：行级权限的参数列大多是**外键**（alm_issues.owner_domain_id 存 1..9），
#   而人只认得名字（电池系统域）。两类信息天然在两列里：
#     alm_issues.owner_domain_id  → 1, 2, 3 …
#     owner_domains.name          → 电池系统域, 电驱系统域 …
#   不把「ID ↔ 名称」绑在一起，界面上就只能让人填裸数字 ID，模型也无从判断
#   用户说的「电池系统域」该对应哪个 ID。
#
# 做法：把维表那列（owner_domains.name）的每条记录补上 enum_label=对应主键。
#   于是同一条文档同时带着「名字」和「它的 ID」，两侧都能用：
#     - 模型看到 owner_domains.name 的示例值是域名（写 WHERE 时用得上）
#     - 前端/权限侧读 enum_label 拿到 ID（注入行级过滤时用得上）
#
# ★ 为什么单独成脚本而不是塞进 build_nl2sql_meta：那条构建链路已经很长，
#   外键补全是幂等且可独立重跑的收尾步骤，单独放更好排障。
#
# 用法：
#   python scripts/link_fk_labels.py --datasource rd_agent
#   python scripts/link_fk_labels.py --datasource rd_agent --dry-run
# ============================================================

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from elasticsearch import AsyncElasticsearch
from elasticsearch import NotFoundError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import get_settings
from src.nl2sql.conf.meta_config import MetaConfig


def _dim_target(meta: MetaConfig, fk_table: str, desc: str) -> tuple[str, str] | None:
    """从外键列的描述里解析出维表与可读列。

    描述是自然语言（「维表 owner_domains.id → owner_domains.name」），
    但格式高度一致，用正则取两端的「表.列」即可；取不到就跳过该列。
    另外要求维表在元数据里存在、且那列标了 sync（否则 ES 里没有它的值）。
    """
    import re

    # 「维表 owner_domains.id → owner_domains.name」
    m = re.search(r"维表\s+([\w.]+)\s*→\s*([\w.]+)", desc)
    if not m:
        return None
    dim_source, readable = m.group(1), m.group(2)   # owner_domains.id / owner_domains.name
    dim_table = dim_source.split(".")[0]
    if "." in readable:
        read_table, read_col = readable.split(".", 1)
    else:
        # 「→ vin（车架号）」这种省略表前缀的写法：维表就是目标表
        read_table, read_col = dim_table, readable

    t = next((x for x in meta.tables if x.name == read_table), None)
    if t is None:
        return None
    col = next((c for c in t.columns if c.name == read_col), None)
    if col is None or not col.sync:
        return None
    return read_table, read_col


async def main(datasource: str, dry_run: bool = False) -> int:
    settings = get_settings()
    yaml_path = Path(__file__).parents[1] / "conf" / "projects" / f"{datasource}.yaml"
    meta = MetaConfig.from_yaml(yaml_path)
    if meta.datasource is None:
        raise SystemExit(f"{yaml_path} 缺少 datasource 段")
    ds = meta.datasource
    index = f"chatbi_{ds.es_prefix}_values"

    # 收集所有「外键列 → 维表可读列」的映射
    pairs: list[tuple[str, str, str]] = []      # (fk_表, fk_列, 维表可读列)
    for t in meta.tables:
        pk = next((c.name for c in t.columns if c.role == "primary_key"), None)
        if pk is None:
            continue    # 没有单列主键，无法建立 ID 映射
        for c in t.columns:
            if c.role != "foreign_key":
                continue
            target = _dim_target(meta, t.name, c.description or "")
            if target:
                pairs.append((t.name, c.name, f"{target[0]}.{target[1]}", pk))

    if not pairs:
        print("没有找到可补全的外键关系")
        return 0

    print(f"数据源: {datasource}  索引: {index}")
    for fk_t, fk_c, dim_col, pk in pairs:
        print(f"  {fk_t}.{fk_c} → {dim_col}（用维表主键 {pk}）")

    if dry_run:
        return 0

    engine = create_async_engine(ds.dsn)
    es = AsyncElasticsearch(f"http://{settings.ES_HOST}:{settings.ES_PORT}")
    total = 0
    skipped = 0
    try:
        for fk_t, fk_c, dim_col, _pk in pairs:
            dim_table, dim_colname = dim_col.split(".", 1)
            # ★ 在业务库里做真正的 JOIN —— 这才是「ID ↔ 名称」的权威对应，
            #   比按序号对齐可靠（事实表可能只用到维表的一部分值）
            sql = (
                f'SELECT d."{dim_colname}" AS label, d.id AS id '
                f'FROM "{dim_table}" d WHERE d."{dim_colname}" IS NOT NULL'
            )
            async with engine.connect() as conn:
                rows = (await conn.execute(text(sql))).mappings().all()

            for r in rows:
                label = str(r["label"]).strip()
                doc_id = f"{dim_table}.{dim_colname}.db.{label}"
                try:
                    await es.update(
                        index=index, id=doc_id,
                        body={"doc": {"enum_label": str(r["id"]), "fk_column": f"{fk_t}.{fk_c}"}},
                        refresh=False,
                    )
                    total += 1
                except NotFoundError:
                    # 该值未入 ES（如维表 distinct 值超过 200 护栏被整列跳过）——跳过
                    skipped += 1
        await es.indices.refresh(index=index)
    finally:
        await engine.dispose()
        await es.close()

    print(f"\n已为 {total} 条维表记录补上 enum_label（ID），{skipped} 条因未入 ES 跳过")
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="补全外键枚举值的 ID ↔ 名称关联")
    ap.add_argument("--datasource", default="auto_full")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.datasource, args.dry_run))
