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

# 每个数据源的案例文件（案例文件里的 datasource 字段优先）
#
# ★ *_reflow.json 是线上 badcase 回流出来的案例，由
#   scripts/export_badcase_cases.py 从 chatbi_badcases 的 approved 记录生成后
#   提交进仓库 —— 评测器本身仍然是纯函数、零外部依赖（CI 的 test job 没有 PG）。
#   这两个文件必须始终存在（空集也要有 {"cases": []}），否则案例集会在
#   「有没有跑过导出」之间悄悄变化。
CASE_FILES_BY_PROJECT: dict[str, list[Path]] = {
    "rd_agent": [
        CASES_DIR / "nl2sql_cases.json",
        CASES_DIR / "nl2sql_cases_reflow.json",
    ],
    "hospital_demo": [
        CASES_DIR / "nl2sql_cases_hospital.json",
        CASES_DIR / "nl2sql_cases_reflow_hospital.json",
    ],
}


def _files_for(project: str, override: Path | None) -> list[Path]:
    if override is not None:
        return [override]
    files = CASE_FILES_BY_PROJECT.get(project, [])
    return [f for f in files if f.exists()]


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


def load_project_cases(project: str, override: Path | None = None) -> list[dict]:
    """某数据源的全部案例（多文件合并，id 全局唯一）。

    ★ 原实现只读单个文件，导致 50 条案例全打在 rd_agent 上、
      hospital_demo（默认演示数据源）零覆盖。现在按数据源聚合。"""
    files = _files_for(project, override)
    if not files:
        raise FileNotFoundError(f"数据源 {project} 没有可用案例文件（找过 {CASE_FILES_BY_PROJECT.get(project, [])}）")

    merged: list[dict] = []
    seen: set[str] = set()
    for f in files:
        for c in load_cases(f):
            if c["id"] in seen:
                raise ValueError(f"案例 id 跨文件重复: {c['id']}（{f.name}）")
            seen.add(c["id"])
            merged.append(c)
    return merged



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
    —— 两者都是必须拦在 CI 里的回归。

    ★ 覆盖全部已注册数据源的案例文件（不传 path 时）；传 path 则只跑该文件。"""
    from src.nl2sql.security import validate_sql

    if path is not None:
        groups = [(path.stem, load_cases(path))]
    else:
        groups = [
            (project, load_project_cases(project))
            for project in CASE_FILES_BY_PROJECT
            if _files_for(project, None)
        ]

    passed, failed, failures = 0, 0, []
    valid_levels = {"easy", "medium", "hard"}

    for project, cases in groups:
        print(f"\n[{project}] {len(cases)} 条案例")
        for c in cases:
            ok, result = validate_sql(c["golden_sql"])
            if not ok:
                failed += 1
                failures.append(f"{project}/{c['id']}: golden_sql 未通过安全层 —— {result}")
                continue
            # 分层维度合法性：评测要按 category × difficulty 出统计，
            # 写错分层等于没有度量
            if c["difficulty"] not in valid_levels:
                failed += 1
                failures.append(
                    f"{project}/{c['id']}: difficulty 必须是 {sorted(valid_levels)}，"
                    f"实际 {c['difficulty']}"
                )
                continue
            passed += 1
            print(f"  ✓ {c['id']} [{c['category']}/{c['difficulty']}]")

    return passed, failed, failures


# ════════════════════════════════════════════════════════════════════════
# 模式二：实况执行准确率（需要 LLM + 业务库）
# ════════════════════════════════════════════════════════════════════════

async def run_live_project(project: str, cases: list[dict]) -> tuple[float, int, int]:
    """跑一个数据源的全部案例，返回 (执行准确率, 通过数, 总数)"""
    from src.api.deps import get_llm
    from src.infra.datasources import dw_session_factory, get_datasource
    from src.nl2sql.engine import build_schema_prompt, run_query, setup_readonly_session
    from src.nl2sql.repositories import PgMetaRepository
    from src.infra.db import AsyncSessionLocal
    from sqlalchemy import text

    ds = await get_datasource(project)
    if ds is None:
        print(f"数据源 {project} 未注册或未启用")
        return 0.0, 0, len(cases)

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

    factory = dw_session_factory(project)
    async with factory() as db:
        for c in cases:
            cid = c["id"]
            t0 = asyncio.get_event_loop().time()
            result = await run_query(
                question=c["question"], llm=llm, db=db, role="admin",
                role_rules=ds.role_rules, params={}, schema=schema,
                source_name=ds.name, sensitive_columns=ds.sensitive_columns,
            )
            latency = asyncio.get_event_loop().time() - t0
            latencies.append(latency)

            errors = []
            if not result.success:
                errors.append(f"NL2SQL 失败: {result.error[:120]}")
            else:
                try:
                    await setup_readonly_session(db)
                    golden_rows = (await db.execute(text(c["golden_sql"]))).mappings().all()
                except Exception as e:
                    # 逐条 rollback：一条失败会把事务打成 aborted，
                    # 后续每条都报 InFailedSQLTransactionError 而不是真原因
                    await db.rollback()
                    errors.append(f"golden 执行失败: {type(e).__name__}: {str(e)[:120]}")
                else:
                    if not compare_results([dict(r) for r in golden_rows], result.data):
                        errors.append(
                            f"结果不一致 golden={len(golden_rows)}行 "
                            f"pred={result.row_count}行 sql={result.sql[:120]}"
                        )

            ok = not errors
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
    return acc, sum(overall), len(overall)


# ════════════════════════════════════════════════════════════════════════

async def run_live(project: str, threshold: float, path: Path | None) -> bool:
    """实况执行准确率。project="all" 时跑全部有案例文件的数据源。"""
    projects = list(CASE_FILES_BY_PROJECT) if project == "all" else [project]

    results: dict[str, tuple[float, int, int]] = {}
    for p in projects:
        if not _files_for(p, path):
            print(f"数据源 {p} 没有案例文件，跳过")
            continue
        try:
            cases = load_project_cases(p, path)
        except (FileNotFoundError, ValueError) as e:
            print(f"数据源 {p} 案例加载失败: {e}")
            return False

        print(f"\n{'=' * 62}\n数据源 {p}（{len(cases)} 条案例）\n{'=' * 62}")
        results[p] = await run_live_project(p, cases)

    if not results:
        print("没有任何数据源可评测")
        return False

    print(f"\n{'=' * 62}\n汇总\n{'=' * 62}")
    total_ok = total_n = 0
    all_pass = True
    for p, (acc, ok, n) in results.items():
        total_ok += ok
        total_n += n
        flag = "✓" if acc >= threshold else "✗"
        if acc < threshold:
            all_pass = False
        print(f"  {flag} {p}: {ok}/{n} = {acc:.0%}（门限 {threshold:.0%}）")
    if total_n:
        print(f"  总体: {total_ok}/{total_n} = {total_ok / total_n:.0%}")
    return all_pass


# ════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NL2SQL 评测运行器")
    parser.add_argument("--live", action="store_true", help="跑实况执行准确率（需 LLM + 业务库）")
    parser.add_argument("--project", default="all",
                        help="数据源编码（bi_datasources.code），默认 all = 全部有案例的数据源")
    parser.add_argument("--threshold", type=float, default=0.8, help="live 模式准确率门限")
    parser.add_argument("--cases", type=Path, default=None, help="自定义案例文件（覆盖数据源默认文件）")
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
