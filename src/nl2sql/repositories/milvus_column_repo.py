# ============================================================
# Milvus 列向量检索 — 语义搜索相关列
# ============================================================

from pymilvus import MilvusClient

from src.nl2sql.entities import ColumnInfo

COLUMN_COLLECTION = "chatbi_columns"  # 默认 collection（兼容常量引用）


class MilvusColumnRepository:
    """Milvus 向量检索 column 元数据

    ★ 多数据源：prefix 参数决定 collection（chatbi_{prefix}_columns），
    不同项目元数据物理隔离，共享同一 Milvus 实例。"""

    def __init__(self, client: MilvusClient, prefix: str = ""):
        self.client = client
        self.collection = f"chatbi_{prefix}_columns" if prefix else COLUMN_COLLECTION

    def search(
        self, query_vector: list[float], top_k: int = 5, threshold: float = 0.6
    ) -> list[ColumnInfo]:
        """向量相似度检索候选列，threshold 以下的结果被丢弃"""
        if not self._collection_exists():
            return []

        results = self.client.search(
            collection_name=self.collection,
            data=[query_vector],
            limit=top_k,
            output_fields=["column_id", "column_name", "column_type", "role",
                           "description", "aliases", "table_name"],
        )

        columns: dict[str, ColumnInfo] = {}
        for hit in results[0]:
            if hit["distance"] < threshold:
                continue
            entity = hit["entity"]
            col_id = entity.get("column_id", "")
            if col_id not in columns:
                columns[col_id] = ColumnInfo(
                    id=col_id,
                    name=entity.get("column_name", ""),
                    type=entity.get("column_type", ""),
                    role=entity.get("role", ""),
                    description=entity.get("description", ""),
                    alias=entity.get("aliases", []),
                )
        return list(columns.values())

    def _collection_exists(self) -> bool:
        try:
            collections = self.client.list_collections()
            return self.collection in collections
        except Exception:
            return False
