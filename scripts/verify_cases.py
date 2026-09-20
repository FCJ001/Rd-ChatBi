# ============================================================
# 案例实况校验 —— 把案例文件里每条 golden_sql 在真实业务库上跑一遍
#
# 离线门禁（eval/run_nl2sql_eval.py 默认模式）只验证 golden 能过安全层
# （纯 SQL 解析，不连库）；golden 在**真实 schema 上能不能执行、结果是否
# 判分有效**（非空、非「单行全 0」的恒过题）必须连库才知道。CI 的
# eval-cases job 用本脚本对新生成的库做这道实况校验。
#
# 用法：
#   python scripts/verify_cases.py                          # 校验 auto_full 全部案例
#   python scripts/verify_cases.py --datasource auto_full   # 同上（显式）
#   python scripts/verify_cases.py --case-file eval/cases/x.json
#
# 任一案例失败 → 非零退出码。
# ============================================================

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text  # noqa: E402


def _is_degenerate(rows: list[dict]) -> str:
    """判断结果是否「判分无意义」。返回原因，正常则返回空串。"""
    if not rows:
        return "0 行"
    if len(rows) == 1 and len(rows[0]) == 1:
        v = list(rows[0].values())[0]
        if v is None or (isinstance(v, (int, float, Decimal)) and v == 0):
            return "单行单列且为 0/None（恒过题）"
    return ""


async def verify(case_file: Path, datasource: str) -> int:
    from src.infra.datasources import dw_session_factory, get_datasource
    from eval.run_nl2sql_eval import load_cases

    ds = await get_datasource(datasource)
    if ds is None:
        print(f"✗ 数据源 {datasource} 未注册或未启用")
        return 1

    cases = load_cases(case_file)
    print(f"校验 {case_file.name}: {len(cases)} 条 golden → 数据源 {datasource}")

    failed: list[tuple[str, str]] = []
    factory = dw_session_factory(datasource)
    async with factory() as db:
        await db.execute(text("SET statement_timeout = '10s'"))
        for c in cases:
            try:
                rows = [dict(r) for r in (await db.execute(text(c["golden_sql"]))).mappings().all()]
            except Exception as e:
                await db.rollback()
                failed.append((c["id"], f"执行失败: {type(e).__name__}: {str(e)[:100]}"))
                continue
            reason = _is_degenerate(rows)
            if reason:
                failed.append((c["id"], reason))
    if failed:
        print(f"✗ {len(failed)} 条不合格：")
        for cid, why in failed:
            print(f"  ✗ {cid}: {why}")
        return 1
    print(f"✓ {len(cases)} 条 golden 全部执行通过且判分有效")
    return 0


async def main(case_file: Path | None, datasource: str) -> int:
    from eval.run_nl2sql_eval import CASE_FILES_BY_PROJECT, _files_for

    if case_file is not None:
        return await verify(case_file, datasource)
    # 不指定文件：校验该数据源注册的全部案例文件（主文件 + 回流文件）
    rc = 0
    for f in _files_for(datasource, None):
        rc |= await verify(f, datasource)
    if not rc:
        print(f"\n数据源 {datasource} 全部案例实况校验通过")
    return rc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="案例 golden_sql 实况校验（连业务库）")
    ap.add_argument("--datasource", default="auto_full")
    ap.add_argument("--case-file", type=Path, default=None,
                    help="只校验单个案例文件（默认：该数据源注册的全部文件）")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.case_file, a.datasource)))
