# ============================================================
# Milvus 向量迁移（旧实例 → 本项目自带的实例）
#
# ★ 为什么不直接重建：build_nl2sql_meta 的 _build_column_vectors 是**逐列
#   串行**调 embed_query —— 815 列 = 815 次 HTTP，跑几分钟且中间任何一次
#   网络抖动就整个失败（实测撞了两次：ConnectionReset 和 KeyError 'request'）。
#   而向量本来就在旧库里，搬过去是纯数据搬运，不烧 embedding 也不受网络影响。
#
# 用法：
#   python scripts/migrate_milvus.py --from http://localhost:19530 --to http://localhost:19531
#   python scripts/migrate_milvus.py ... --only chatbi_auto_full   # 只迁某前缀
#
# ★ 幂等：目标 collection 已存在则先 drop 再建，可反复跑。
# ============================================================

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from pymilvus import DataType, MilvusClient  # noqa: E402

# 字段类型码（pymilvus 的 DataType 枚举值）→ 目标 schema 用
_VARCHAR, _ARRAY, _FLOAT_VECTOR = 21, 23, 101

# 需要迁移的 collection 后缀及各自的标量字段（与 build_nl2sql_meta 建的 schema 一致）
COLLECTION_SPECS: dict[str, list[tuple[str, int, int]]] = {
    # 后缀: [(字段名, 类型码, max_length)]
    "columns": [
        ("column_id", _VARCHAR, 256), ("column_name", _VARCHAR, 256),
        ("column_type", _VARCHAR, 64), ("role", _VARCHAR, 32),
        ("description", _VARCHAR, 2048), ("aliases", _ARRAY, 0),
        ("table_name", _VARCHAR, 128),
    ],
    "metrics": [
        ("metric_id", _VARCHAR, 128), ("metric_name", _VARCHAR, 256),
        ("description", _VARCHAR, 2048), ("relevant_columns", _ARRAY, 0),
        ("aliases", _ARRAY, 0),
    ],
    "examples": [
        ("example_id", _VARCHAR, 64), ("question", _VARCHAR, 1024),
        ("golden_sql", _VARCHAR, 8192), ("category", _VARCHAR, 64),
        ("source", _VARCHAR, 16),
    ],
}

_PK = {"columns": "column_id", "metrics": "metric_id", "examples": "example_id"}


def _build_schema(suffix: str):
    schema = MilvusClient.create_schema(auto_id=False)
    for name, typ, maxlen in COLLECTION_SPECS[suffix]:
        if typ == _VARCHAR:
            schema.add_field(name, DataType.VARCHAR, max_length=maxlen,
                             is_primary=(name == _PK[suffix]))
        elif typ == _ARRAY:
            schema.add_field(name, DataType.ARRAY, element_type=DataType.VARCHAR,
                             max_capacity=64, max_length=512)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=1024)
    return schema


def _build_index():
    ip = MilvusClient.prepare_index_params()
    ip.add_index(field_name="vector", metric_type="COSINE",
                 index_type="IVF_FLAT", params={"nlist": 128})
    return ip


def migrate(from_uri: str, to_uri: str, only: str | None, batch: int = 200) -> None:
    src, dst = MilvusClient(uri=from_uri), MilvusClient(uri=to_uri)
    all_cols = sorted(src.list_collections())
    print(f"源 {from_uri}: {len(all_cols)} 个 collection")

    done = 0
    for col in all_cols:
        suffix = col.rsplit("_", 1)[-1]
        if suffix not in COLLECTION_SPECS:
            continue                      # 不是本项目的（如 mem0 的长记忆）
        if only and not col.startswith(only):
            continue

        pk = _PK[suffix]
        fields = [f for f, _, _ in COLLECTION_SPECS[suffix]] + ["vector"]
        rows = src.query(col, filter=f'{pk} != ""', output_fields=fields, limit=16384)
        if not rows:
            print(f"  {col}: 空，跳过")
            continue

        if dst.has_collection(col):
            dst.drop_collection(col)
        dst.create_collection(col, schema=_build_schema(suffix), index_params=_build_index())

        for i in range(0, len(rows), batch):
            dst.insert(collection_name=col, data=rows[i:i + batch])
        dst.flush(col)
        print(f"  ✓ {col}: {len(rows)} 条")
        done += 1

    print(f"\n共迁移 {done} 个 collection")
    print(f"目标现有: {sorted(dst.list_collections())}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Milvus 向量迁移")
    ap.add_argument("--from", dest="from_uri", default="http://localhost:19530")
    ap.add_argument("--to", dest="to_uri", default="http://localhost:19531")
    ap.add_argument("--only", default=None, help="只迁此前缀的 collection")
    a = ap.parse_args()
    migrate(a.from_uri, a.to_uri, a.only)
