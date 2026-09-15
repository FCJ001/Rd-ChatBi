# ============================================================
# 会话历史 → badcase 待审表（可重复执行的挖掘脚本）
#
# 干什么：扫 Redis 里的会话历史 → 按判据挑出可疑轮次 → upsert 进 chatbi_badcases
#
# 为什么：线上真实查询是最好的评测素材，但会话历史 7 天 TTL 就过期了。
#
# ★ 反直觉结论（别高估这个脚本）：它**不是**「立刻能挖出几百题」的路径。
#   它能挖到的上限取决于历史里留下了什么 —— 而 /query-stream 原先失败轮次
#   根本不写历史（router 里 `if last_result.get("result_data")` 那个门槛），
#   写进去的 QueryResult 也不传 success（dataclass 默认 True = 假成功）。
#   该缺陷已修，但**修复之前产生的历史已经没了**，所以首次运行很可能一条都挖不到。
#   真正持续供料的是服务端采集路径（pipeline.py 的 finally），不是这个脚本。
#
# 用法：
#   python scripts/mine_badcases.py                      # 只扫描打印，不写库（默认安全）
#   python scripts/mine_badcases.py --datasource hospital_demo --dry-run
#   python scripts/mine_badcases.py --write              # 真正 upsert
#   python scripts/mine_badcases.py --include-clean      # 把「查成功但 0 行」也算候选
# ============================================================

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import get_settings          # noqa: E402
from src.infra.datasources import get_datasource  # noqa: E402
from src.nl2sql.badcase_store import MAX_ROW_LIMIT, classify_error  # noqa: E402
from src.nl2sql.ctx_store import iter_all_payloads  # noqa: E402


@dataclass
class Candidate:
    question: str
    sql: str
    error: str
    error_type: str
    row_count: int


def pick(turn: dict, include_clean: bool) -> Candidate | None:
    """单轮判定。返回 None 表示这轮是正常的，不进队列。

    判据优先级（取首个命中）：
      1. success=False            → 按错误文案分类（role_denied / timeout / db_error）
      2. 有 SQL 且 0 行（可选）    → empty_result：SQL 合法、结果为空，最难排查的一类
      3. 有 SQL 且行数达上限       → truncated：被 LIMIT 钳到上限，很可能结果被截断

    ★ 第 3 条容易被误读：row_count 的上限是 MAX_ROW_LIMIT（AST 层强制），
      所以 row_count == 100 的含义是「被截断了」，不是「正好 100 行」。
    """
    sql = (turn.get("sql") or "").strip()
    error = (turn.get("error") or "").strip()
    row_count = int(turn.get("row_count") or 0)

    if not turn.get("success", True):
        et = classify_error(error, sql, row_count) or "db_error"
        return Candidate(turn.get("question") or "", sql, error, et, row_count)

    if not sql:
        return None

    if row_count == 0 and include_clean:
        return Candidate(turn.get("question") or "", sql, "", "empty_result", 0)
    if row_count >= MAX_ROW_LIMIT and include_clean:
        return Candidate(turn.get("question") or "", sql, "", "truncated", row_count)
    return None


async def main(datasource: str, include_clean: bool, write: bool) -> int:
    if get_settings().CONVERSATION_BACKEND != "redis":
        print(f"✗ CONVERSATION_BACKEND={get_settings().CONVERSATION_BACKEND}，"
              f"会话历史只在进程内存里，独立脚本读不到。请改用 redis 后端，"
              f"或直接跑服务端采集路径。")
        return 1

    ds = await get_datasource(datasource)
    if ds is None:
        print(f"✗ 数据源 {datasource} 未注册或未启用")
        return 1

    n_sessions = n_turns = 0
    found: list[Candidate] = []
    async for payload in iter_all_payloads():
        n_sessions += 1
        for turn in payload:
            if not isinstance(turn, dict):
                continue
            n_turns += 1
            c = pick(turn, include_clean)
            if c is not None:
                found.append(c)

    print(f"扫描 {n_sessions} 个会话、{n_turns} 轮对话 → 命中候选 {len(found)} 条")
    by_type: dict[str, int] = {}
    for c in found:
        by_type[c.error_type] = by_type.get(c.error_type, 0) + 1
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  {t}: {n}")
    for c in found[:15]:
        print(f"  ✗ [{c.error_type}] {c.question[:40]} → {(c.error or c.sql)[:60]}")

    if not write:
        print("\n（未加 --write，仅扫描。加 --write 才会写入待审队列）")
        return 0

    from src.infra.db import AsyncSessionLocal
    from src.nl2sql.repositories import BadcaseRepository

    added = 0
    async with AsyncSessionLocal() as db:
        repo = BadcaseRepository(db)
        for c in found:
            try:
                await repo.upsert_badcase(
                    datasource_id=ds.id,
                    datasource_code=ds.code,
                    source="history",
                    question=c.question,
                    # ★ 历史里的 question 已经是改写后的，原始问句没有被保存下来 ——
                    #   这里如实标注「无法区分」，不要假装能分开
                    resolved_question=c.question,
                    user_role="",
                    predicted_sql=c.sql,
                    error_type=c.error_type,
                    error_message=c.error,
                    row_count=c.row_count,
                    note="来自会话历史挖掘：question 为改写后的问题（原始问句未留存）",
                )
                added += 1
            except Exception as e:
                print(f"  ! 写入失败（跳过）: {c.question[:30]} —— {e}")
    print(f"\n已 upsert {added}/{len(found)} 条到待审队列（数据源 {ds.code}）")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="从会话历史挖 badcase 进待审队列")
    ap.add_argument("--datasource", default="rd_agent",
                    help="数据源编码（历史里的 project 段不参与判定，必须显式指定）")
    ap.add_argument("--include-clean", action="store_true",
                    help="把「查成功但 0 行 / 被 LIMIT 截断」也算候选（噪声更大，默认关）")
    ap.add_argument("--write", action="store_true", help="写入待审队列（默认只扫描）")
    ap.add_argument("--dry-run", action="store_true", help="同默认行为，显式写法")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.datasource, args.include_clean,
                              args.write and not args.dry_run)))
