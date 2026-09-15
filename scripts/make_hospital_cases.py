# ============================================================
# hospital_demo 评测案例 —— 在一个只读连接上把每条 golden_sql 真跑一遍
#
# 目的：50 条存量案例全打在 rd_agent 上，默认演示数据源 hospital_demo
# 零覆盖。这里补齐后在评测器里按 datasource 分组执行。
#
# 用法：.venv/bin/python scripts/make_hospital_cases.py [--write]
#   不带 --write 只校验并打印统计（安全，不改仓库文件）
#   --write 把结果写到 eval/cases/nl2sql_cases_hospital.json
# ============================================================

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text  # noqa: E402

OUT_PATH = REPO_ROOT / "eval" / "cases" / "nl2sql_cases_hospital.json"

CASES: list[dict] = [
    # ── 单表聚合 ──────────────────────────────────────────────
    dict(id="H01", category="单表聚合", difficulty="easy",
         question="一共有多少条门诊挂号记录？",
         golden_sql="SELECT COUNT(*) AS cnt FROM outpatient_visits"),
    dict(id="H02", category="单表聚合", difficulty="easy",
         question="门诊挂号费总收入是多少？",
         golden_sql="SELECT SUM(registration_fee) AS total FROM outpatient_visits WHERE status = 'paid'"),
    dict(id="H03", category="单表聚合", difficulty="easy",
         question="目前有多少条住院记录？",
         golden_sql="SELECT COUNT(*) AS cnt FROM inpatient_records"),
    dict(id="H04", category="单表聚合", difficulty="medium",
         question="未缴费的门诊挂号有多少笔？",
         golden_sql="SELECT COUNT(*) AS cnt FROM outpatient_visits WHERE status = 'unpaid'"),
    dict(id="H05", category="单表聚合", difficulty="medium",
         question="住院总费用一共是多少？",
         golden_sql="SELECT SUM(total_cost) AS total FROM inpatient_records"),
    dict(id="H06", category="单表聚合", difficulty="easy",
         question="一共有多少张挂号单被退号了？",
         golden_sql="SELECT COUNT(*) AS cnt FROM outpatient_visits WHERE status = 'cancelled'"),
    dict(id="H07", category="单表聚合", difficulty="medium",
         question="还在住院（未出院）的病人有多少？",
         golden_sql="SELECT COUNT(*) AS cnt FROM inpatient_records WHERE status = 'in_treatment'"),
    dict(id="H08", category="单表聚合", difficulty="easy",
         question="平均住院天数是多少？",
         golden_sql="SELECT AVG(stay_days) AS avg_days FROM inpatient_records"),
    dict(id="H09", category="单表聚合", difficulty="medium",
         question="住院已结算的记录一共收入多少？",
         golden_sql="SELECT SUM(total_cost) AS total FROM inpatient_records WHERE status = 'settled'"),

    # ── 分组统计 ──────────────────────────────────────────────
    dict(id="H10", category="分组统计", difficulty="medium",
         question="各挂号类型的门诊量分别是多少？",
         golden_sql="SELECT visit_type, COUNT(*) AS cnt FROM outpatient_visits WHERE status <> 'cancelled' GROUP BY visit_type"),
    dict(id="H11", category="分组统计", difficulty="medium",
         question="按缴费状态统计挂号笔数",
         golden_sql="SELECT status, COUNT(*) AS cnt FROM outpatient_visits GROUP BY status"),
    dict(id="H12", category="分组统计", difficulty="medium",
         question="各科室大类下有多少个科室？",
         golden_sql="SELECT category, COUNT(*) AS cnt FROM departments GROUP BY category"),
    dict(id="H13", category="分组统计", difficulty="medium",
         question="各楼栋分别有多少个科室？",
         golden_sql="SELECT building, COUNT(*) AS cnt FROM departments GROUP BY building"),
    dict(id="H14", category="分组统计", difficulty="hard",
         question="各住院状态的记录数和总费用分别是多少？",
         golden_sql="SELECT status, COUNT(*) AS cnt, SUM(total_cost) AS total FROM inpatient_records GROUP BY status"),
    dict(id="H15", category="分组统计", difficulty="hard",
         question="各科室的住院人次和平均住院天数",
         golden_sql=(
             "SELECT d.name AS dept, COUNT(*) AS cnt, AVG(i.stay_days) AS avg_days "
             "FROM inpatient_records i JOIN departments d ON i.department_id = d.id "
             "GROUP BY d.name"
         )),
    dict(id="H16", category="分组统计", difficulty="hard",
         question="各科室大类下的门诊挂号总费用是多少？",
         golden_sql=(
             "SELECT d.category, SUM(o.registration_fee) AS total "
             "FROM outpatient_visits o JOIN departments d ON o.department_id = d.id "
             "WHERE o.status = 'paid' GROUP BY d.category"
         )),
    dict(id="H17", category="分组统计", difficulty="medium",
         question="各楼栋的科室数量按楼层区分一下",
         golden_sql="SELECT building, floor, COUNT(*) AS cnt FROM departments GROUP BY building, floor"),

    # ── 排序 TopN ─────────────────────────────────────────────
    dict(id="H18", category="排序TopN", difficulty="medium",
         question="门诊量最多的前 5 个科室是哪些？",
         golden_sql=(
             "SELECT d.name AS dept, COUNT(*) AS cnt "
             "FROM outpatient_visits o JOIN departments d ON o.department_id = d.id "
             "WHERE o.status <> 'cancelled' GROUP BY d.name ORDER BY cnt DESC LIMIT 5"
         )),
    dict(id="H19", category="排序TopN", difficulty="medium",
         question="住院收入最高的 3 个科室",
         golden_sql=(
             "SELECT d.name AS dept, SUM(i.total_cost) AS total "
             "FROM inpatient_records i JOIN departments d ON i.department_id = d.id "
             "GROUP BY d.name ORDER BY total DESC LIMIT 3"
         )),
    dict(id="H20", category="排序TopN", difficulty="medium",
         question="哪个科室的住院人次最多？",
         golden_sql=(
             "SELECT d.name AS dept, COUNT(*) AS cnt "
             "FROM inpatient_records i JOIN departments d ON i.department_id = d.id "
             "GROUP BY d.name ORDER BY cnt DESC LIMIT 1"
         )),
    dict(id="H21", category="排序TopN", difficulty="medium",
         question="接诊量最高的 5 位医生是谁？",
         golden_sql=(
             "SELECT doctor_name, COUNT(*) AS cnt FROM outpatient_visits "
             "WHERE status <> 'cancelled' GROUP BY doctor_name ORDER BY cnt DESC LIMIT 5"
         )),
    dict(id="H22", category="排序TopN", difficulty="hard",
         question="退号率最高的 3 个科室",
         golden_sql=(
             "SELECT d.name AS dept, "
             "COUNT(*) FILTER (WHERE o.status = 'cancelled') AS cancelled_cnt "
             "FROM outpatient_visits o JOIN departments d ON o.department_id = d.id "
             "GROUP BY d.name ORDER BY cancelled_cnt DESC LIMIT 3"
         )),

    # ── 时间窗口 ──────────────────────────────────────────────
    # ★ 全部用**固定日期区间**，不用 CURRENT_DATE 相对窗口。
    #   原因（实测）：demo 数据区间是 2025-09-09 ~ 2026-09-08 的滚动一年，
    #   而执行评测的「今天」是 2026-09-13，已经超出数据上界 —— "最近30天"
    #   这类相对条件会返回 0 行，案例恒等空集：任何 SQL 只要同样返回空集
    #   就算「通过」，评测退化成摆设。
    #   固定区间还有额外好处：golden 与 LLM 生成的 SQL 在**同一时刻**执行，
    #   相对窗口会有跨天漂移（今天命中 30 天、明天命中 29 天）。
    dict(id="H23", category="时间窗口", difficulty="medium",
         question="2026 年 8 月的门诊挂号量是多少？",
         golden_sql=(
             "SELECT COUNT(*) AS cnt FROM outpatient_visits "
             "WHERE visit_date >= DATE '2026-08-01' AND visit_date < DATE '2026-09-01'"
         )),
    dict(id="H24", category="时间窗口", difficulty="medium",
         question="2025 年 12 月的门诊挂号量",
         golden_sql=(
             "SELECT COUNT(*) AS cnt FROM outpatient_visits "
             "WHERE visit_date >= DATE '2025-12-01' AND visit_date < DATE '2026-01-01'"
         )),
    dict(id="H25", category="时间窗口", difficulty="hard",
         question="2026 年上半年的门诊逐月挂号量",
         golden_sql=(
             "SELECT DATE_TRUNC('month', visit_date) AS mon, COUNT(*) AS cnt "
             "FROM outpatient_visits "
             "WHERE visit_date >= DATE '2026-01-01' AND visit_date < DATE '2026-07-01' "
             "GROUP BY mon ORDER BY mon"
         )),
    dict(id="H26", category="时间窗口", difficulty="medium",
         question="2026 年 7 月新增了多少住院记录？",
         golden_sql=(
             "SELECT COUNT(*) AS cnt FROM inpatient_records "
             "WHERE admit_date >= DATE '2026-07-01' AND admit_date < DATE '2026-08-01'"
         )),
    dict(id="H27", category="时间窗口", difficulty="hard",
         question="2026 年每个季度的住院收入",
         golden_sql=(
             "SELECT DATE_TRUNC('quarter', admit_date) AS qtr, SUM(total_cost) AS total "
             "FROM inpatient_records "
             "WHERE admit_date >= DATE '2026-01-01' AND admit_date < DATE '2027-01-01' "
             "GROUP BY qtr ORDER BY qtr"
         )),
    dict(id="H28", category="时间窗口", difficulty="medium",
         question="2026 年 3 月各挂号类型的门诊量",
         golden_sql=(
             "SELECT visit_type, COUNT(*) AS cnt FROM outpatient_visits "
             "WHERE visit_date >= DATE '2026-03-01' AND visit_date < DATE '2026-04-01' "
             "GROUP BY visit_type"
         )),
    dict(id="H29", category="时间窗口", difficulty="hard",
         question="2026 年 8 月每天的挂号量，按日期排序",
         golden_sql=(
             "SELECT visit_date, COUNT(*) AS cnt FROM outpatient_visits "
             "WHERE visit_date >= DATE '2026-08-01' AND visit_date < DATE '2026-09-01' "
             "GROUP BY visit_date ORDER BY visit_date"
         )),
    dict(id="H30", category="时间窗口", difficulty="hard",
         question="2026 年 5 月各科室的门诊量对比",
         golden_sql=(
             "SELECT d.name AS dept, COUNT(*) AS cnt "
             "FROM outpatient_visits o JOIN departments d ON o.department_id = d.id "
             "WHERE o.visit_date >= DATE '2026-05-01' AND o.visit_date < DATE '2026-06-01' "
             "GROUP BY d.name"
         )),

    # ── 多表 JOIN ─────────────────────────────────────────────
    dict(id="H31", category="多表JOIN", difficulty="hard",
         question="各科室的门诊收入和住院收入分别是多少？",
         golden_sql=(
             "SELECT d.name AS dept, "
             "(SELECT COALESCE(SUM(o.registration_fee), 0) FROM outpatient_visits o "
             " WHERE o.department_id = d.id AND o.status = 'paid') AS outpatient_total, "
             "(SELECT COALESCE(SUM(i.total_cost), 0) FROM inpatient_records i "
             " WHERE i.department_id = d.id) AS inpatient_total "
             "FROM departments d"
         )),
    dict(id="H32", category="多表JOIN", difficulty="hard",
         question="每个科室的门诊量和住院人次对比",
         golden_sql=(
             "SELECT d.name AS dept, "
             "(SELECT COUNT(*) FROM outpatient_visits o WHERE o.department_id = d.id) AS outpatient_cnt, "
             "(SELECT COUNT(*) FROM inpatient_records i WHERE i.department_id = d.id) AS inpatient_cnt "
             "FROM departments d"
         )),
    dict(id="H33", category="多表JOIN", difficulty="hard",
         question="各楼栋的门诊挂号量分别是多少？",
         golden_sql=(
             "SELECT d.building, COUNT(*) AS cnt "
             "FROM outpatient_visits o JOIN departments d ON o.department_id = d.id "
             "GROUP BY d.building"
         )),
    dict(id="H34", category="多表JOIN", difficulty="hard",
         question="各科室大类的门诊量排名",
         golden_sql=(
             "SELECT d.category, COUNT(*) AS cnt "
             "FROM outpatient_visits o JOIN departments d ON o.department_id = d.id "
             "WHERE o.status <> 'cancelled' GROUP BY d.category ORDER BY cnt DESC"
         )),
    dict(id="H35", category="多表JOIN", difficulty="hard",
         question="已结算住院记录里各科室的次均费用",
         golden_sql=(
             "SELECT d.name AS dept, AVG(i.total_cost) AS avg_cost "
             "FROM inpatient_records i JOIN departments d ON i.department_id = d.id "
             "WHERE i.status = 'settled' GROUP BY d.name"
         )),

    # ── 明细查询 ──────────────────────────────────────────────
    dict(id="H36", category="明细查询", difficulty="easy",
         question="列出前 10 条门诊挂号的单号、日期和挂号费",
         golden_sql=(
             "SELECT visit_no, visit_date, registration_fee FROM outpatient_visits "
             "ORDER BY id LIMIT 10"
         )),
    dict(id="H37", category="明细查询", difficulty="medium",
         question="前 10 条已缴费的门诊挂号记录",
         golden_sql=(
             "SELECT visit_no, visit_date, registration_fee FROM outpatient_visits "
             "WHERE status = 'paid' ORDER BY id LIMIT 10"
         )),
    dict(id="H38", category="明细查询", difficulty="medium",
         question="住院时间超过 15 天的记录有多少条？",
         golden_sql=(
             "SELECT COUNT(*) AS cnt FROM inpatient_records WHERE stay_days > 15"
         )),
    dict(id="H39", category="明细查询", difficulty="hard",
         question="列出住院费最高的前 5 条记录的单号和费用",
         golden_sql=(
             "SELECT record_no, total_cost FROM inpatient_records "
             "ORDER BY total_cost DESC LIMIT 5"
         )),
    dict(id="H40", category="明细查询", difficulty="easy",
         question="列出所有科室的名称和所在楼层",
         golden_sql="SELECT name, floor FROM departments ORDER BY id LIMIT 10"),
]


async def validate() -> tuple[int, list[str]]:
    """把每条 golden_sql 真跑一遍：语法/列名错在这里就暴露"""
    from src.infra.pool import get_sessionmaker
    from src.core.config import get_settings

    sm = get_sessionmaker(get_settings().DEMO_DATABASE_URL, "eval_hospital")
    failures: list[str] = []
    ok = 0
    async with sm() as s:
        for c in CASES:
            try:
                rows = (await s.execute(text(c["golden_sql"]))).mappings().all()
                if not rows:
                    # ★ 空结果集是**无效案例**：任何同样返回空的 SQL 都会被
                    #   exec-match 判为通过，评测形同虚设。宁可让脚本失败。
                    failures.append(f"{c['id']}: golden_sql 返回 0 行（案例无效）")
                    print(f"  ✗ {c['id']:<4} 返回 0 行 —— 案例无效，需换条件")
                    continue
                ok += 1
                head = dict(rows[0])
                print(f"  ✓ {c['id']:<4} [{c['category']}/{c['difficulty']}] "
                      f"{len(rows)} 行  {str(head)[:70]}")
            except Exception as e:
                # ★ 一条失败会让整个事务进入 aborted 态，后续每条都会报
                #   InFailedSQLTransactionError 而不是真原因 —— 必须逐条 rollback，
                #   否则只能看到第一条真实的报错（本项目 execute_sql 节点同款处理）
                await s.rollback()
                failures.append(f"{c['id']}: {type(e).__name__}: {str(e)[:160]}")
                print(f"  ✗ {c['id']:<4} 执行失败: {str(e)[:120]}")
    return ok, failures


# 时间窗口类案例依赖种子数据的具体日期区间（2025-09-09 ~ 2026-09-08 的滚动一年）。
# 造数脚本的随机日期或数据区间一旦变了，这些案例可能"仍能跑出非空结果、但语义
# 已经不是问的那段时间了" —— 非空校验抓不到。
#
# ★ 判定用「至少有一行其计数列 > 0」，不能简单按行数：
#   单值聚合类（COUNT/SUM）本来就只返回 1 行，分类型的也只返回 3 行左右。
class _WindowCase:
    """按结果形状区分：单值聚合 / 分组 / 明细"""

    SINGLE = "single"    # COUNT(*)/SUM → 1 行，判第一行的数值列 > 0
    GROUPED = "grouped"  # GROUP BY → N 行，判至少一行计数 > 0
    ROWS = "rows"        # 明细 → 多行，判行数 >= 5


WINDOW_CASES: dict[str, str] = {
    "H23": _WindowCase.SINGLE,
    "H24": _WindowCase.SINGLE,
    "H25": _WindowCase.GROUPED,
    "H26": _WindowCase.SINGLE,
    "H27": _WindowCase.GROUPED,
    "H28": _WindowCase.GROUPED,
    "H29": _WindowCase.ROWS,
    "H30": _WindowCase.GROUPED,
}

_MIN_DETAIL_ROWS = 5


def _window_hits(rows, shape: str) -> int:
    """该案例"命中了多少条数据"（不是返回了几行）"""
    if shape == _WindowCase.ROWS:
        return len(rows)
    hits = 0
    for r in rows:
        # 取结果里第一个数值列当计数量（golden 都带了 cnt/total/avg）
        for v in r.values():
            if isinstance(v, (int, float)) or v.__class__.__name__ == "Decimal":
                if float(v) > 0:
                    hits += 1
                break
    return hits


async def validate_windows() -> list[str]:
    """时间窗口案例的语义校验：必须真的命中该时间段的量级，不能只是凑巧非空"""
    from src.infra.pool import get_sessionmaker
    from src.core.config import get_settings

    sm = get_sessionmaker(get_settings().DEMO_DATABASE_URL, "eval_hospital")
    problems: list[str] = []
    async with sm() as s:
        for c in CASES:
            shape = WINDOW_CASES.get(c["id"])
            if shape is None:
                continue
            try:
                rows = (await s.execute(text(c["golden_sql"]))).mappings().all()
            except Exception as e:
                await s.rollback()
                problems.append(f"{c['id']}: 执行失败 {type(e).__name__}")
                continue
            hits = _window_hits(rows, shape)
            floor = _MIN_DETAIL_ROWS if shape == _WindowCase.ROWS else 1
            if hits < floor:
                problems.append(
                    f"{c['id']}: 命中 {hits} 条（< {floor}，返回 {len(rows)} 行）"
                    f"—— 时间窗口可能已与种子数据区间脱节，需同步更新日期"
                )
            else:
                print(f"  ✓ {c['id']} 命中 {hits} 条（{len(rows)} 行）")
    return problems


def check_structure() -> list[str]:
    """与主评测器同口径的结构校验（安全层 + 分层合法性 + id 唯一）"""
    from src.nl2sql.security import validate_sql

    problems: list[str] = []
    seen: set[str] = set()
    valid_levels = {"easy", "medium", "hard"}
    for c in CASES:
        for key in ("id", "category", "difficulty", "question", "golden_sql"):
            if key not in c:
                problems.append(f"{c.get('id','?')}: 缺字段 {key}")
        if c["id"] in seen:
            problems.append(f"id 重复: {c['id']}")
        seen.add(c["id"])
        if c["difficulty"] not in valid_levels:
            problems.append(f"{c['id']}: difficulty 非法 {c['difficulty']}")
        ok, result = validate_sql(c["golden_sql"])
        if not ok:
            problems.append(f"{c['id']}: golden_sql 未通过安全层 —— {result}")
    return problems


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写入 eval/cases/nl2sql_cases_hospital.json")
    args = ap.parse_args()

    print(f"结构校验（{len(CASES)} 条）：")
    problems = check_structure()
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        print(f"\n结构校验失败 {len(problems)} 条")
        sys.exit(1)
    print("  ✓ 全部通过安全层 + 结构合法\n")

    print("执行校验（真跑 golden_sql，需 chatbi_demo 库）：")
    ok, failures = await validate()

    print("\n时间窗口语义校验（命中量级）：")
    window_problems = await validate_windows()
    if window_problems:
        for p in window_problems:
            print(f"  ✗ {p}")
        failures.extend(window_problems)
    else:
        print("  ✓ 窗口案例命中量级正常")

    print(f"\n执行通过 {ok}/{len(CASES)}")
    if failures:
        print("\n失败明细：")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1)

    if args.write:
        payload = {
            "description": (
                f"hospital_demo 执行准确率评测案例（{len(CASES)} 题，6 类 × 3 档难度）。"
                "补齐原评测集只覆盖 rd_agent 的空白。"
            ),
            "datasource": "hospital_demo",
            "cases": CASES,
        }
        OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {OUT_PATH}")
    else:
        print("\n（未加 --write，仅校验）")


if __name__ == "__main__":
    asyncio.run(main())
