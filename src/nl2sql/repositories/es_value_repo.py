# ============================================================
# Elasticsearch 列值检索 — 真实枚举值 + 同义词，两类记录共用一个索引
#
# 索引里的每条记录有个 source 字段区分来源：
#   source="db"    → 业务库 SELECT DISTINCT 捞出来的**真实枚举值**
#                    （departments.name 的「内科」「外科」）—— 可直接写进 WHERE
#   source="alias" → YAML 里手写的**同义词**
#                    （用户会说「科室」，但库里没有「科室」这个值）—— 不可当字面量
#
# ★ 为什么两类都要：同义词负责「用户的话 → 字段」这一段匹配，
#   真实值负责「告诉模型合法取值集合」。缺了真实值，模型会编造
#   WHERE departments.name = '心内科' 这种查不到数据的 SQL。
#
# ★ 多数据源：prefix 参数决定 index（chatbi_{prefix}_values）。
# ============================================================

from elasticsearch import AsyncElasticsearch

from src.nl2sql.entities import ValueInfo

VALUE_INDEX = "chatbi_values"  # 默认 index（兼容常量引用）

# 单次查询最多带回多少条真实枚举值（防止宽表把 prompt 撑爆）
_MAX_DB_VALUES = 60




class ESValueRepository:
    """ES 检索列值枚举 + 同义词"""

    def __init__(self, client: AsyncElasticsearch, prefix: str = ""):
        self.client = client
        self.index = f"chatbi_{prefix}_values" if prefix else VALUE_INDEX

    async def search(self, keyword: str, size: int = 10) -> list[ValueInfo]:
        """按关键词检索，分两步（见 _build_query 的说明）。

        第 ① 步：value 全文匹配 → 找到「用户说的词」关联到哪些列。
        第 ② 步：把这些列的**真实枚举值**一并取回。

        ★ 为什么必须有第 ② 步：库里 status 存的是英文 'paid'，用户说「已缴费」。
          ES 永远匹配不到 'paid'（字面毫无交集），只做第 ① 步的话，
          模型只知道「要查 status 这一列」，却依然不知道合法取值是 paid/unpaid/cancelled
          —— 它还是会写 WHERE status='已缴费'，查出空结果。
          第 ② 步把「这一列有哪些合法值」补上，才真正堵住这个洞。
        """
        if not await self._index_exists():
            return []

        matched = await self._match_values(keyword, size)
        if not matched:
            return []

        column_ids = {v.column_id for v in matched if v.column_id}
        db_values = await self._db_values_for_columns(column_ids)

        # 按 column_id 去重合并；真实值排在前面（模型最需要的是它）
        out: dict[str, ValueInfo] = {}
        for v in db_values + matched:
            out.setdefault(v.id, v)
        return list(out.values())

    async def _match_values(self, keyword: str, size: int) -> list[ValueInfo]:
        """第 ① 步：value 全文匹配（alias 与 db 值都参与）"""
        result = await self.client.search(
            index=self.index,
            body={"query": self._build_query(keyword), "size": size},
        )
        return [self._to_value_info(h["_source"]) for h in result["hits"]["hits"]]

    async def _db_values_for_columns(self, column_ids: set[str]) -> list[ValueInfo]:
        """第 ② 步：把这些列的**真实枚举值**全取回来（按列分组返回）。

        上限 _MAX_DB_VALUES：一次查询最多带 60 条，防止宽表把上下文撑爆。
        """
        if not column_ids:
            return []
        result = await self.client.search(
            index=self.index,
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"terms": {"column_id": sorted(column_ids)}},
                            {"term": {"source": "db"}},
                        ]
                    }
                },
                "size": _MAX_DB_VALUES,
            },
        )
        return [self._to_value_info(h["_source"]) for h in result["hits"]["hits"]]

    @staticmethod
    def _to_value_info(src: dict) -> ValueInfo:
        return ValueInfo(
            id=src.get("id", ""),
            value=src.get("value", ""),
            column_id=src.get("column_id", ""),
            source=src.get("source", "alias"),
        )

    @staticmethod
    def _build_query(keyword: str) -> dict:
        """value 全文匹配（alias + db 都参与检索）。

        ★ 为什么两类都要检索：alias 是「用户的话 → 字段」的**匹配线索**。
          例：库里 status 存的是英文 'paid'，用户却说「已缴费」。
          standard 分词器对中文逐字切分，「已缴费」切成的字跟 'paid'
          **字面上毫无交集**，只查 db 值会一条都命中不了。
          靠 alias「已缴费」命中 status 这一列，再把它自己的真实值 'paid' 带出来，
          模型才知道该写 WHERE status = 'paid'。

        ★ 两类语义不同（db=合法取值，alias=同义词），区分交给下游：
          merge_info 会分别标注后挂到列的 examples 上。
        """
        return {"match": {"value": {"query": keyword, "operator": "or"}}}

    async def _index_exists(self) -> bool:
        try:
            return await self.client.indices.exists(index=self.index)
        except Exception:
            return False
