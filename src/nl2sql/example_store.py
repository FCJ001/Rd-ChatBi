# ============================================================
# few-shot 示例库（升级方案 P1 主线 B）
#
# 参考 Vanna 的核心机制：(question, SQL) 问答对向量化，生成 SQL 前
# 检索相似问题注入 prompt。数据来源 = 评测集 golden SQL + 审核通过的
# badcase —— badcase 修复后在线立即生效（不止回归不退化）。
#
# 防评测自泄漏：检索时默认剔除与当前问题完全相同的示例（normalize 后
# 比较）。近义改写仍会命中（这正是生产价值），但"原题撞库"不算数。
# ============================================================

import hashlib
import re

from pymilvus import DataType, MilvusClient

from src.core.logger import logger
from src.core.metrics import RETRIEVAL_REQUESTS

EXAMPLE_COLLECTION = "chatbi_examples"  # 默认 collection（无 prefix 时）
VECTOR_DIM = 1024  # text-embedding-v3，与 build_nl2sql_meta 一致


def example_collection_name(prefix: str = "") -> str:
    return f"chatbi_{prefix}_examples" if prefix else EXAMPLE_COLLECTION


def example_id(question: str) -> str:
    """主键 = 归一化问题的 sha256 前 32 位（同题去重，upsert 幂等）"""
    return hashlib.sha256(normalize_question(question).encode()).hexdigest()[:32]


def normalize_question(q: str) -> str:
    """比较用归一化：去空白/标点，统一小写。避免"空格差异"绕过去重。"""
    return re.sub(r"[\s，。？！?!,.:'\"（）()【】\[\]]+", "", q or "").lower()


def format_examples_block(examples: list[dict]) -> str:
    """渲染进 prompt 的 few-shot 区块。明确"参考口径而非照抄"。"""
    if not examples:
        return ""
    lines = [
        "## 相似问题示例（已验证的正确问答对，参考其表选择/口径/写法，不要照抄数值条件）",
    ]
    for i, ex in enumerate(examples, 1):
        lines.append(f"### 示例{i}：{ex.get('question', '').strip()}")
        lines.append(f"正确 SQL：{ex.get('sql', '').strip()}")
    return "\n".join(lines)


class MilvusExampleRepository:
    """示例库的 Milvus 读写。schema 对齐 build_nl2sql_meta（VARCHAR 主键 + IVF_FLAT/COSINE）。"""

    def __init__(self, client: MilvusClient, prefix: str = ""):
        self.client = client
        self.collection = example_collection_name(prefix)

    # ── 管理 ──────────────────────────────────────────────
    def ensure_collection(self):
        if self.client.has_collection(self.collection):
            return
        schema = MilvusClient.create_schema(auto_id=False)
        schema.add_field("example_id", DataType.VARCHAR, max_length=64, is_primary=True)
        schema.add_field("question", DataType.VARCHAR, max_length=1024)
        schema.add_field("golden_sql", DataType.VARCHAR, max_length=8192)
        schema.add_field("category", DataType.VARCHAR, max_length=64)
        schema.add_field("source", DataType.VARCHAR, max_length=16)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
        index_params = MilvusClient.prepare_index_params()
        index_params.add_index(
            field_name="vector", metric_type="COSINE",
            index_type="IVF_FLAT", params={"nlist": 128},
        )
        self.client.create_collection(self.collection, schema=schema, index_params=index_params)

    def drop(self):
        if self.client.has_collection(self.collection):
            self.client.drop_collection(self.collection)

    def upsert(self, rows: list[dict]):
        """rows: [{question, sql, category, source, vector}]，主键由 example_id 派生"""
        if not rows:
            return 0
        data = []
        for r in rows:
            data.append({
                "example_id": example_id(r["question"]),
                "question": r["question"][:1000],
                "golden_sql": r["sql"][:8000],
                "category": (r.get("category") or "")[:60],
                "source": (r.get("source") or "eval")[:15],
                "vector": r["vector"],
            })
        # 同 question 去重（upsert 主键冲突会报错，先按主键收敛）
        seen: dict[str, dict] = {}
        for d in data:
            seen[d["example_id"]] = d
        self.client.upsert(collection_name=self.collection, data=list(seen.values()))
        self.client.flush(self.collection)
        return len(seen)

    def list_ids(self) -> list[str]:
        """列出 collection 内全部示例 id（sync 脚本做陈旧清理用）"""
        if not self.client.has_collection(self.collection):
            return []
        rows = self.client.query(
            collection_name=self.collection,
            filter='example_id != ""',
            output_fields=["example_id"],
            limit=16384,
        )
        return [r["example_id"] for r in rows]

    def delete_ids(self, ids: list[str]) -> int:
        if not ids:
            return 0
        self.client.delete(collection_name=self.collection, ids=ids)
        return len(ids)

    # ── 检索 ──────────────────────────────────────────────
    def search(self, query_vector: list[float], top_k: int = 3,
               threshold: float = 0.6) -> list[dict]:
        if not self.client.has_collection(self.collection):
            return []
        results = self.client.search(
            collection_name=self.collection,
            data=[query_vector],
            limit=top_k,
            output_fields=["question", "golden_sql", "category", "source"],
        )
        out = []
        for hit in results[0]:
            if hit["distance"] < threshold:  # COSINE：越大越相似，与列检索同约定
                continue
            e = hit["entity"]
            out.append({
                "question": e.get("question", ""),
                "sql": e.get("golden_sql", ""),
                "category": e.get("category", ""),
                "source": e.get("source", ""),
                "score": hit["distance"],
            })
        return out


async def find_similar_examples(
    repo: MilvusExampleRepository,
    embedding_model,
    question: str,
    top_k: int = 3,
    threshold: float = 0.6,
    exclude_exact: bool = True,
) -> list[dict]:
    """在线检索 few-shot 示例。exclude_exact：剔除与当前问题完全相同的示例
    （防评测自泄漏；生产上完全相同的重复问题由会话历史/缓存服务）。

    ★ 打点归这里管，不归调用方 —— 因为「是 embedding 挂了还是 Milvus 挂了」
      只有这一层知道。曾经这里是裸 `except: return []`：DashScope 停服期间
      示例召回静默归零，调用方看到的是"未命中"，**与「一切正常」在监控上
      完全一样**。现在按 ok/empty/failed 三态记账，并区分失败发生在哪一步。
    """
    if repo is None or embedding_model is None or not question:
        return []
    try:
        vectors = await embedding_model.aembed_documents([question])
    except Exception as e:
        # embedding 供应商故障（如 DashScope 欠费停服）—— 这一路整个失效
        RETRIEVAL_REQUESTS.labels(channel="examples", status="failed").inc()
        logger.warning(f"[examples] embedding 调用失败，示例召回降级为空: {e}")
        return []
    try:
        hits = repo.search(vectors[0], top_k=top_k + 1, threshold=threshold)
    except Exception as e:
        # Milvus 故障：与上面分开记，否则排查时分不清是哪一侧挂了
        RETRIEVAL_REQUESTS.labels(channel="examples", status="failed").inc()
        logger.warning(f"[examples] Milvus 检索失败，示例召回降级为空: {e}")
        return []
    out = []
    for h in hits:
        if exclude_exact and normalize_question(h["question"]) == normalize_question(question):
            continue
        out.append({"question": h["question"], "sql": h["sql"],
                    "category": h["category"], "source": h["source"]})
        if len(out) >= top_k:
            break
    RETRIEVAL_REQUESTS.labels(channel="examples", status="ok" if out else "empty").inc()
    return out
