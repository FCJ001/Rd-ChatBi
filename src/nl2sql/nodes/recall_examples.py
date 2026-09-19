# ============================================================
# Node ②d — few-shot 示例召回（并行召回组第 4 路）
#
# 升级方案 P1 主线 B：从示例库（评测集 golden SQL + 审核通过的 badcase）
# 检索相似问题，注入 generate_sql 的 prompt。
# repo/embedding_model 缺失时静默跳过 —— 示例库是增强不是依赖。
# ============================================================

from src.nl2sql.example_store import find_similar_examples
from src.nl2sql.state import DataAgentState
from src.nl2sql.context import DataAgentContext
from src.core.logger import logger
from src.core.metrics import RETRIEVAL_REQUESTS


async def recall_examples(state: DataAgentState, ctx: DataAgentContext) -> dict:
    repo = ctx.get("milvus_example_repo")
    embedding_model = ctx.get("embedding_model")
    question = state.get("query", "")

    if not repo or not embedding_model or not question:
        # 依赖没注入 / 没建库 → 这一路本来就该跳过，记 skipped 以区别于失败
        RETRIEVAL_REQUESTS.labels(channel="examples", status="skipped").inc()
        return {}

    writer = ctx.get("writer")
    if writer:
        writer({"type": "progress", "step": "召回相似示例", "status": "running"})
    try:
        # ok/empty/failed 的打点在 example_store 里（那一层才知道是 embedding
        # 还是 Milvus 挂了）；节点这里只管"依赖缺失"的 skipped。
        examples = await find_similar_examples(repo, embedding_model, question)
        # 状态只用 running/success/error 三态（前端进度渲染的既有契约）
        if writer:
            writer({"type": "progress", "step": "召回相似示例", "status": "success",
                    "detail": f"{len(examples)} 条" if examples else "未命中"})
        return {"few_shot_examples": examples}
    except Exception as e:
        # 正常路径的失败已在 example_store 里打点；到这里是节点级意外
        # （如 repo 接口变更）。留一条日志 + failed，保持可观测闭环。
        RETRIEVAL_REQUESTS.labels(channel="examples", status="failed").inc()
        logger.warning(f"[recall_examples] 节点级异常，本路降级为空（不影响查询）: {e}")
        if writer:
            writer({"type": "progress", "step": "召回相似示例", "status": "success",
                    "detail": "跳过（检索异常）"})
        return {}
