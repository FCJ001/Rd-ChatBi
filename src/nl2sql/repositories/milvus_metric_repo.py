# ============================================================
# Milvus 指标向量检索 — 语义搜索相关指标
# ============================================================

from pymilvus import MilvusClient

from src.nl2sql.entities import MetricInfo

METRIC_COLLECTION = "chatbi_metrics"  # 默认 collection（兼容常量引用）


class MilvusMetricRepository:
    """Milvus 向量检索 metric 元数据

    ★ 多数据源：prefix 参数决定 collection（chatbi_{prefix}_metrics）。"""

    def __init__(self, client: MilvusClient, prefix: str = ""):
        self.client = client
        self.collection = f"chatbi_{prefix}_metrics" if prefix else METRIC_COLLECTION

    def search(
        self, query_vector: list[float], top_k: int = 5, threshold: float = 0.6
    ) -> list[MetricInfo]:
        if not self._collection_exists():
            return []

        results = self.client.search(
            collection_name=self.collection,
            data=[query_vector],
            limit=top_k,
            output_fields=["metric_id", "metric_name", "description",
                           "relevant_columns", "aliases"],
        )

        metrics: dict[str, MetricInfo] = {}
        for hit in results[0]:
            if hit["distance"] < threshold:
                continue
            entity = hit["entity"]
            mid = entity.get("metric_id", "")
            if mid not in metrics:
                metrics[mid] = MetricInfo(
                    id=mid,
                    name=entity.get("metric_name", ""),
                    description=entity.get("description", ""),
                    relevant_columns=entity.get("relevant_columns", []),
                    alias=entity.get("aliases", []),
                )
        return list(metrics.values())

    def _collection_exists(self) -> bool:
        try:
            return self.collection in self.client.list_collections()
        except Exception:
            return False
