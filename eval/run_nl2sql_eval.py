# ============================================================
# NL2SQL 离线评测运行器
#
# 两种模式：
#   python eval/run_nl2sql_eval.py            # 离线门禁（默认）—— 案例结构
#                                             # 校验 + golden SQL 必须通过安全层
#                                             # （LIMIT 强制覆盖/单语句/SELECT-only），
#                                             # 纯函数，CI 可跑
#   python eval/run_nl2sql_eval.py --live     # 实况 —— LLM 生成 SQL 与 golden_sql
#                                             # 在同一只读连接执行，按结果集等价
#                                             # 判定执行准确率，输出按 category ×
#                                             # difficulty 的分层统计
#
# 任一模式失败都返回非零退出码，作为回归门禁。
# ============================================================

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases"
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CASES = CASES_DIR / "nl2sql_cases.json"


def load_cases(path: Path | None = None) -> list[dict]:
    data = json.loads((path or DEFAULT_CASES).read_text(encoding="utf-8"))
    cases = data["cases"]
    seen = set()
    for c in cases:
        for key in ("id", "category", "difficulty", "question", "golden_sql"):
            assert key in c, f"案例缺字段 {key}: {c.get('id', '?')}"
        assert c["id"] not in seen, f"案例 id 重复: {c['id']}"
        seen.add(c["id"])
    return cases


# ════════════════════════════════════════════════════════════════════════
# 结果集等价判定（exec-match）
# ════════════════════════════════════════════════════════════════════════

def _normalize_value(v, precision: int = 4):
    """统一单元格类型：Decimal/float 数值舍入（聚合口径差异容忍），
    日期时间转字符串，字符串去首尾空白。"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, Decimal):
        return round(float(v), precision)
    if isinstance(v, float):
        return round(v, precision)
    if isinstance(v, (datetime, date)):
        return str(v)
    if isinstance(v, str):
        return v.strip()
    return v


def normalize_rows(rows: list[dict], precision: int = 4) -> list[tuple]:
    """行 → 排序后的值元组列表。列名不参与比较（LLM 的别名与 golden 不同是常态），
    行序不参与比较（无 ORDER BY 语义的集合等价）。"""
    out = []
    for r in rows:
        vals = tuple(_normalize_value(v, precision) for v in r.values())
        out.append((repr(sorted(vals, key=repr)),))  # 行内列序也不比较
    return sorted(out)


def compare_results(golden_rows: list[dict], pred_rows: list[dict], precision: int = 4) -> bool:
    """执行准确率判定：结果集多重集合等价。"""
    return normalize_rows(golden_rows, precision) == normalize_rows(pred_rows, precision)


# ════════════════════════════════════════════════════════════════════════
# 模式一：离线门禁（纯函数）
# ════════════════════════════════════════════════════════════════════════

def run_offline_gate(path: Path | None = None) -> tuple[int, int, list[str]]:
    """校验案例结构 + golden SQL 必须能通过安全层。

    golden 被安全层拒绝只有两种可能：案例本身写错，或安全层误伤合法查询
    —— 两者都是必须拦在 CI 里的回归。"""
    from src.nl2sql.security import validate_sql

    cases = load_cases(path)
    passed, failed, failures = 0, 0, []

    for c in cases:
        ok, result = validate_sql(c["golden_sql"])
        if not ok:
            failed += 1
            failures.append(f"{c['id']}: golden_sql 未通过安全层 —— {result}")
        else:
            passed += 1
            print(f"  ✓ {c['id']} [{c['category']}/{c['difficulty']}]")

    # 分层维度合法性：评测要按 category × difficulty 出统计，写错分层等于没有度量
    valid_levels = {"easy", "medium", "hard"}
    for c in cases:
        if c["difficulty"] not in valid_levels:
            failed += 1
            failures.append(f"{c['id']}: difficulty 必须是 {sorted(valid_levels)}，实际 {c['difficulty']}")

    return passed, failed, failures


# ════════════════════════════════════════════════════════════════════════
# 模式二：实况执行准确率（需要 LLM + 业务库）
# ════════════════════════════════════════════════════════════════════════

async def run_live(project: str, threshold: float, path: Path | None) -> bool:
    from src.api.deps import get_llm
    from src.infra.datasources import dw_session_factory, get_datasource
    from src.nl2sql.engine import build_schema_prompt, run_query, setup_readonly_session
    from src.nl2sql.repositories import PgMetaRepository
    from src.infra.db import AsyncSessionLocal
    from sqlalchemy import text

    cases = load_cases(path)
    ds = await get_datasource(project)
    if ds is None:
        print(f"数据源 {project} 未注册或未启用")
        return False

    # 动态 schema（与线上 /query 相同的构建路径）
    meta_db = AsyncSessionLocal()
    try:
        repo = PgMetaRepository(meta_db, ds.id)
        tables = await repo.get_all_tables()
        for t in tables:
            t.columns = await repo.get_columns_by_table(t.id)
        schema = build_schema_prompt(tables)
    finally:
        await meta_db.close()

    llm = get_llm()
    stats: dict[str, list[bool]] = defaultdict(list)
    latencies: list[float] = []
    all_ok = True

    factory = dw_session_factory(project)
    async with factory() as db:
        for c in cases:
            cid = c["id"]
            t0 = asyncio.get_event_loop().time()
            result = await run_query(
                question=c["question"], llm=llm, db=db, role="admin",
                role_rules=ds.role_rules, params={}, schema=schema,
                source_name=ds.name,
            )
            latency = asyncio.get_event_loop().time() - t0
            latencies.append(latency)

            errors = []
            if not result.success:
                errors.append(f"NL2SQL 失败: {result.error[:120]}")
            else:
                await setup_readonly_session(db)
                golden_rows = (await db.execute(text(c["golden_sql"]))).mappings().all()
                if not compare_results([dict(r) for r in golden_rows], result.data):
                    errors.append(f"结果不一致 golden={len(golden_rows)}行 pred={result.row_count}行 "
                                  f"sql={result.sql[:120]}")

            ok = not errors
            all_ok = all_ok and ok
            key = f"{c['category']}/{c['difficulty']}"
            stats[c["category"]].append(ok)
            stats[c["difficulty"]].append(ok)
            stats[key].append(ok)
            stats["_overall"].append(ok)
            mark = "✓" if ok else "✗"
            print(f"  {mark} {cid} [{key}] {c['question'][:30]}  ({latency:.1f}s)")
            for e in errors:
                print(f"      - {e}")

    print("\n执行准确率（分层）：")
    for layer in ("_overall", "单表聚合", "分组统计", "排序TopN", "时间窗口", "多表JOIN", "明细查询",
                  "easy", "medium", "hard"):
        if layer not in stats:
            continue
        rs = stats[layer]
        name = "总体" if layer == "_overall" else layer
        print(f"  {name}: {sum(rs)}/{len(rs)} = {sum(rs) / len(rs):.0%}")
    avg = sum(latencies) / len(latencies) if latencies else 0
    print(f"  平均耗时: {avg:.1f}s")

    overall = stats["_overall"]
    acc = sum(overall) / len(overall) if overall else 0.0
    return acc >= threshold


# ════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NL2SQL 评测运行器")
    parser.add_argument("--live", action="store_true", help="跑实况执行准确率（需 LLM + 业务库）")
    parser.add_argument("--project", default="rd_agent", help="数据源编码（bi_datasources.code）")
    parser.add_argument("--threshold", type=float, default=0.8, help="live 模式准确率门限")
    parser.add_argument("--cases", type=Path, default=None, help="自定义案例文件")
    args = parser.parse_args()

    if not args.live:
        print("离线门禁（案例结构 + golden SQL 安全校验，无外部依赖）：")
        passed, failed, failures = run_offline_gate(args.cases)
        print(f"\n通过 {passed} / {passed + failed}")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1 if failed else 0)

    ok = asyncio.run(run_live(args.project, args.threshold, args.cases))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
