# ============================================================
# Elasticsearch 列值全文检索 — IK 中文分词
# ============================================================

from elasticsearch import AsyncElasticsearch

from src.nl2sql.entities import ValueInfo

VALUE_INDEX = "chatbi_values"  # 默认 index（兼容常量引用）


class ESValueRepository:
    """ES 全文检索列值枚举

    ★ 多数据源：prefix 参数决定 index（chatbi_{prefix}_values）。"""

    def __init__(self, client: AsyncElasticsearch, prefix: str = ""):
        self.client = client
        self.index = f"chatbi_{prefix}_values" if prefix else VALUE_INDEX

    async def search(self, keyword: str, size: int = 10) -> list[ValueInfo]:
        if not await self._index_exists():
            return []

        result = await self.client.search(
            index=self.index,
            body={
                "query": {
                    "match": {
                        "value": {
                            "query": keyword,
                            "operator": "or",
                        }
                    }
                },
                "size": size,
            },
        )

        values: dict[str, ValueInfo] = {}
        for hit in result["hits"]["hits"]:
            src = hit["_source"]
            vid = src.get("id", "")
            if vid not in values:
                values[vid] = ValueInfo(
                    id=vid,
                    value=src.get("value", ""),
                    column_id=src.get("column_id", ""),
                )
        return list(values.values())

    async def _index_exists(self) -> bool:
        try:
            return await self.client.indices.exists(index=self.index)
        except Exception:
            return False
