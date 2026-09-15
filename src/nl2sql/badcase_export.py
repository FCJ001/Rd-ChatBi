# ============================================================
# badcase → 评测案例 导出
#
# ★ 为什么导出成文件而不是让评测器直接读 DB：
#   ① CI 的 test job 没有 postgres service，而离线门禁
#      （eval/run_nl2sql_eval.py）刻意是纯函数、零外部依赖；
#   ② 评测集是仓库资产 —— 让它进 git，评测集的变化才能出现在
#      PR diff 里被人 review，「这次加了哪 5 道题」看得见；
#   ③ DB 会被清空/回滚/换环境，文件不会。
#
# 一条 approved 案例要变成可用案例，必须同时满足评测器的全部约束
# （见 badcase_store.validate_case），任何一条不满足都直接报错退出 ——
# 脏数据放进评测集等于把门禁的判据本身污染了。
# ============================================================

from __future__ import annotations

from src.nl2sql.badcase_store import CaseValidationError, validate_case
from src.nl2sql.repositories import BadcaseRepository

# 回流案例的 id 前缀。★ 必须与存量案例的 N*/H* 区分：
#   load_project_cases 对跨文件 id 重复是直接 raise 的（不静默覆盖），
#   撞 id 会让整个离线门禁报错。
ID_PREFIX = "R"


def _case_id(index: int) -> str:
    return f"{ID_PREFIX}{index:03d}"


async def build_export(db, ds) -> dict:
    """把某数据源的全部 approved 案例渲染成评测器格式。

    返回 {datasource, cases, skipped, total}：
      cases   —— 可直接写进 eval/cases/*.json 的案例列表
      skipped —— 被校验拦下的（含原因），导出脚本据此报错退出
    """
    repo = BadcaseRepository(db, ds.id)
    approved = await repo.list_approved(ds.id)

    cases: list[dict] = []
    skipped: list[dict] = []
    for i, c in enumerate(approved, start=1):
        try:
            validate_case(
                question=c.question,
                golden_sql=c.golden_sql,
                category=c.category,
                difficulty=c.difficulty,
                sensitive_columns=ds.sensitive_columns,
            )
        except CaseValidationError as e:
            skipped.append({"id": c.id, "question": c.question[:60], "reason": str(e)})
            continue
        cases.append({
            "id": _case_id(i),
            "category": c.category,
            "difficulty": c.difficulty,
            "question": c.question,
            "golden_sql": c.golden_sql,
            "_badcase_id": c.id,
        })

    return {
        "datasource": ds.code,
        "total": len(approved),
        "cases": cases,
        "skipped": skipped,
    }
