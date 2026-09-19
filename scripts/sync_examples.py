# ============================================================
# few-shot 示例库构建脚本（升级方案 P1 主线 B）
#
# 数据源 = 评测集 golden SQL（eval/cases/*.json，与评测器同一份映射）
#         + 可选：审核通过的 badcase（--include-badcases，从 chatbi_badcases 读）
# 写入 Milvus chatbi_{prefix}_examples，生成前检索注入 prompt ——
# badcase 修复后重跑本脚本即在线生效。
#
# 用法：
#   python scripts/sync_examples.py --datasource auto_full
#   python scripts/sync_examples.py --datasource auto_full --include-badcases
#   python scripts/sync_examples.py --datasource auto_full --rebuild
# ============================================================

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.api.deps import get_embedding_model  # noqa: E402
from src.infra.datasources import get_datasource  # noqa: E402
from src.infra.milvus_client import get_milvus_client  # noqa: E402
from src.nl2sql.example_store import MilvusExampleRepository, example_id  # noqa: E402

# 示例腐烂检测：问题是相对时间表述、SQL 却写死了日期字面量 →
# 过几周这个示例教的就是错答案（"近7天：2026-09-13" 下周就失效）。
_RELATIVE_WORDS = ("最近", "近7", "近30", "近90", "上个月", "上上月", "本月", "上周",
                   "本周", "今天", "昨天", "今年", "去年", "上季度")
_DATE_LITERAL = re.compile(r"20\d{2}-\d{2}-\d{2}")


def _lint_stale_examples(examples: list[dict]) -> int:
    warned = 0
    for e in examples:
        if _DATE_LITERAL.search(e["sql"]) and any(w in e["question"] for w in _RELATIVE_WORDS):
            print(f"  [warn] 示例含硬编码日期且问题是相对时间表述，会随时间腐烂：{e['question'][:36]}")
            warned += 1
    return warned


def _load_eval_cases(project: str) -> list[dict]:
    """复用评测器的案例映射（单一来源，别处不再维护一份文件列表）"""
    spec = importlib.util.spec_from_file_location(
        "run_nl2sql_eval", REPO_ROOT / "eval" / "run_nl2sql_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cases: list[dict] = []
    for path in mod._files_for(project, None):
        data = json.loads(path.read_text(encoding="utf-8"))
        for c in data.get("cases", []):
            if c.get("golden_sql"):
                cases.append({
                    "question": c["question"], "sql": c["golden_sql"],
                    "category": c.get("category", ""), "source": "eval",
                })
    return cases


async def _load_badcase_examples(datasource_id: int) -> list[dict]:
    """审核通过且带 golden_sql 的 badcase（人工口径，最高可信度）"""
    from src.infra.db import AsyncSessionLocal
    from sqlalchemy import text

    cases: list[dict] = []
    db = AsyncSessionLocal()
    try:
        rows = (await db.execute(text(
            "SELECT question, golden_sql, category FROM chatbi_badcases "
            "WHERE datasource_id = :ds AND status = 'approved' AND golden_sql IS NOT NULL"
        ), {"ds": datasource_id})).mappings().all()
        for r in rows:
            cases.append({"question": r["question"], "sql": r["golden_sql"],
                          "category": r["category"] or "", "source": "badcase"})
    finally:
        await db.close()
    return cases


async def main(datasource: str, include_badcases: bool, rebuild: bool):
    ds = await get_datasource(datasource)
    if ds is None:
        raise SystemExit(f"数据源 {datasource} 未注册（先跑 build_nl2sql_meta.py）")

    examples = _load_eval_cases(datasource)
    print(f"[cases] 评测集 {len(examples)} 条")
    if include_badcases:
        bc = await _load_badcase_examples(ds.id)
        print(f"[cases] 审核通过的 badcase {len(bc)} 条")
        examples += bc

    if not examples:
        raise SystemExit("没有可入库的示例")

    stale = _lint_stale_examples(examples)
    if stale:
        print(f"  [lint] {stale} 条示例建议改用相对时间写法（CURRENT_DATE - N / date_trunc(now())）")

    embedding = get_embedding_model()
    vectors = await embedding.aembed_documents([e["question"] for e in examples])
    rows = [{**e, "vector": v} for e, v in zip(examples, vectors)]

    repo = MilvusExampleRepository(get_milvus_client(), prefix=ds.milvus_prefix)
    if rebuild:
        repo.drop()
    repo.ensure_collection()
    n = repo.upsert(rows)

    # 陈旧清理：评测集里删掉/改写的案例，示例库同步删除（防旧 golden 教坏模型）
    keep = {example_id(e["question"]) for e in examples}
    stale = [i for i in repo.list_ids() if i not in keep]
    if stale:
        repo.delete_ids(stale)
        print(f"[milvus] 清理陈旧示例 {len(stale)} 条")

    # 幂等校验：同题只留一条
    unique = len(keep)
    print(f"[milvus] {repo.collection}: upsert {n} 条（去重后 {unique}）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="few-shot 示例库构建")
    parser.add_argument("--datasource", required=True, help="数据源编码")
    parser.add_argument("--include-badcases", action="store_true", help="并入审核通过的 badcase")
    parser.add_argument("--rebuild", action="store_true", help="删掉 collection 重建")
    args = parser.parse_args()
    asyncio.run(main(args.datasource, args.include_badcases, args.rebuild))
