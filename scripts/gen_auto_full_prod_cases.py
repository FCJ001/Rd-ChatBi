# ============================================================
# auto_full 补题（AP 批次）：模拟生产环境问题的案例扩充
#
# 背景（2026-09-20）：
#   - hospital_demo 评测案例下线，案例集全部压在 auto_full 生产库上；
#   - 数据已具备生产特征：周末低谷（周中 82% / 周末 18%）、
#     svc_work_orders.finished_at 与未完工状态对齐地可空（50% NULL）；
#   - 本批次围绕这些特征出题：未完工工单、周末/工作日对比、
#     促销窗口、枚举字面量（考值召回）、明细单查、聚合 TopN。
#
# 写题约束（沿用 gen_auto_full_cases.py 的实测教训）：
#   ★ 时间条件一律固定日期字面量（相对时间会随墙钟漂移成恒空，
#     AUF23/24/43 实测翻车）；月度分组输出 date_trunc 保持 TIMESTAMP
#     （判分器把零点归到月，与模型输出自然对齐，AUF22 同款）。
#   ★ 排序 TopN 只用「分组聚合 + 偏斜外键」形态，边界值必须无并列
#     （写题前已用 SQL 逐一核对）；禁止对稀疏数值列做原始行 TopN。
#   ★ 明细查询要么唯一键单查，要么「显式指定唯一排序键 + LIMIT」，
#     禁止「最近 N 条」这类无唯一答案的形态（AUF28 教训）。
#
# 用法：
#   python scripts/gen_auto_full_prod_cases.py                # 只校验并打印
#   python scripts/gen_auto_full_prod_cases.py --write        # 校验后合入案例文件
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
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

CASE_FILE = REPO_ROOT / "eval" / "cases" / "nl2sql_cases_auto_full.json"

CASES: list[dict] = [
    # ── 单表聚合：NULL 语义 + 枚举字面量（考值召回）──
    dict(id="AP01", category="单表聚合", difficulty="easy",
         question="还有多少维修工单尚未完工？",
         golden_sql="SELECT COUNT(*) AS cnt FROM svc_work_orders WHERE finished_at IS NULL"),
    dict(id="AP02", category="单表聚合", difficulty="easy",
         question="处于停业状态的经销商有多少家？",
         golden_sql="SELECT COUNT(*) AS cnt FROM sal_dealers WHERE status = '停业'"),
    dict(id="AP03", category="单表聚合", difficulty="easy",
         question="平均每次充电大概充了多少度电？",
         golden_sql="SELECT AVG(energy_kwh) AS avg_kwh FROM iot_charging_sessions"),
    dict(id="AP04", category="单表聚合", difficulty="easy",
         question="初级技师一共有多少名？",
         golden_sql="SELECT COUNT(*) AS cnt FROM svc_mechanics WHERE skill_level = '初级'"),
    dict(id="AP05", category="单表聚合", difficulty="easy",
         question="待付款状态的销售订单有多少张？",
         golden_sql="SELECT COUNT(*) AS cnt FROM sal_sales_orders WHERE status = '待付款'"),

    # ── 分组统计：状态分布 + 周末/工作日对比 ──
    dict(id="AP06", category="分组统计", difficulty="easy",
         question="按工单状态统计维修工单的数量",
         golden_sql="SELECT status, COUNT(*) AS cnt FROM svc_work_orders GROUP BY status ORDER BY status"),
    dict(id="AP07", category="分组统计", difficulty="medium",
         question="工作日和周末的充电记录分别有多少条？",
         golden_sql="""SELECT CASE WHEN EXTRACT(ISODOW FROM started_at) >= 6 THEN '周末' ELSE '工作日' END AS day_type,
       COUNT(*) AS cnt
FROM iot_charging_sessions
GROUP BY 1 ORDER BY 1"""),
    dict(id="AP08", category="分组统计", difficulty="medium",
         question="各工单类型里还有多少工单没完工？",
         golden_sql="""SELECT wo_type, COUNT(*) AS unfinished_cnt
FROM svc_work_orders
WHERE finished_at IS NULL
GROUP BY wo_type ORDER BY wo_type"""),
    dict(id="AP09", category="分组统计", difficulty="easy",
         question="研发缺陷问题单按状态分布，各有多少条？",
         golden_sql="SELECT status, COUNT(*) AS cnt FROM alm_issues GROUP BY status ORDER BY status"),
    dict(id="AP10", category="分组统计", difficulty="medium",
         question="各动力类型下分别有多少个车型？",
         golden_sql="""SELECT e.name AS energy_type, COUNT(*) AS model_cnt
FROM veh_models m
JOIN veh_energy_types e ON m.energy_type_id = e.id
GROUP BY e.name ORDER BY e.name"""),
    dict(id="AP11", category="分组统计", difficulty="easy",
         question="各城市分别有多少家经销商？",
         golden_sql="SELECT city, COUNT(*) AS dealer_cnt FROM sal_dealers GROUP BY city ORDER BY city"),

    # ── 时间窗口：固定日期字面量（促销窗口、月度、周末交叉）──
    dict(id="AP12", category="时间窗口", difficulty="medium",
         question="2026年8月进厂的维修工单有多少张？",
         golden_sql="""SELECT COUNT(*) AS cnt FROM svc_work_orders
WHERE intake_at >= '2026-08-01' AND intake_at < '2026-09-01'"""),
    dict(id="AP13", category="时间窗口", difficulty="medium",
         question="2026年7月1日到8月31日（暑假）总共充了多少度电？",
         golden_sql="""SELECT SUM(energy_kwh) AS total_kwh FROM iot_charging_sessions
WHERE started_at >= '2026-07-01' AND started_at < '2026-09-01'"""),
    dict(id="AP14", category="时间窗口", difficulty="medium",
         question="2025年第四季度每个月分别卖出了多少张销售订单？",
         golden_sql="""SELECT date_trunc('month', created_at) AS mon, COUNT(*) AS cnt
FROM sal_sales_orders
WHERE created_at >= '2025-10-01' AND created_at < '2026-01-01'
GROUP BY date_trunc('month', created_at) ORDER BY mon"""),
    dict(id="AP15", category="时间窗口", difficulty="hard",
         question="2026年6月1日到6月18日（618大促）的销售订单总金额和订单数是多少？",
         golden_sql="""SELECT SUM(amount) AS total_amount, COUNT(*) AS order_cnt
FROM sal_sales_orders
WHERE created_at >= '2026-06-01' AND created_at < '2026-06-19'"""),
    dict(id="AP16", category="时间窗口", difficulty="hard",
         question="2026年8月15日到9月15日期间，周末进厂的维修工单有多少张？",
         golden_sql="""SELECT COUNT(*) AS cnt FROM svc_work_orders
WHERE intake_at >= '2026-08-15' AND intake_at < '2026-09-16'
  AND EXTRACT(ISODOW FROM intake_at) >= 6"""),
    dict(id="AP17", category="时间窗口", difficulty="medium",
         question="2025年10月到12月每月的 OTA 安装失败次数分别是多少？",
         golden_sql="""SELECT date_trunc('month', occurred_at) AS mon, COUNT(*) AS cnt
FROM ota_install_failures
WHERE occurred_at >= '2025-10-01' AND occurred_at < '2026-01-01'
GROUP BY date_trunc('month', occurred_at) ORDER BY mon"""),

    # ── 多表JOIN：2~6 跳，跨域组合生产语义 ──
    dict(id="AP18", category="多表JOIN", difficulty="hard",
         question="各动力类型的维修工单分别有多少张？",
         golden_sql="""SELECT e.name AS energy_type, COUNT(*) AS wo_cnt
FROM svc_work_orders w
JOIN veh_vehicles v ON w.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_energy_types e ON m.energy_type_id = e.id
GROUP BY e.name ORDER BY e.name"""),
    dict(id="AP19", category="多表JOIN", difficulty="hard",
         question="各服务中心还有多少张维修工单没完工？",
         golden_sql="""SELECT c.name AS center, COUNT(*) AS unfinished_cnt
FROM svc_work_orders w
JOIN svc_service_centers c ON w.service_center_id = c.id
WHERE w.finished_at IS NULL
GROUP BY c.name ORDER BY c.name"""),
    dict(id="AP20", category="多表JOIN", difficulty="hard",
         question="各品牌的已交付订单分别有多少张？",
         golden_sql="""SELECT b.name AS brand, COUNT(*) AS delivered_cnt
FROM sal_sales_orders o
JOIN veh_trims t ON o.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series s ON m.series_id = s.id
JOIN veh_brands b ON s.brand_id = b.id
WHERE o.status = '已交付'
GROUP BY b.name ORDER BY b.name"""),
    dict(id="AP21", category="多表JOIN", difficulty="hard",
         question="各车型的平均进厂里程是多少公里？",
         golden_sql="""SELECT m.name AS model, ROUND(AVG(w.mileage), 2) AS avg_mileage
FROM svc_work_orders w
JOIN veh_vehicles v ON w.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
GROUP BY m.name ORDER BY m.name"""),
    dict(id="AP22", category="多表JOIN", difficulty="medium",
         question="各车系的平均试驾评分是多少？",
         golden_sql="""SELECT s.name AS series, AVG(td.score) AS avg_score
FROM sal_test_drives td
JOIN veh_models m ON td.model_id = m.id
JOIN veh_model_series s ON m.series_id = s.id
GROUP BY s.name ORDER BY s.name"""),
    dict(id="AP23", category="多表JOIN", difficulty="hard",
         question="2026年上半年各品牌分别收到了多少张销售订单？",
         golden_sql="""SELECT b.name AS brand, COUNT(*) AS order_cnt
FROM sal_sales_orders o
JOIN veh_trims t ON o.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series s ON m.series_id = s.id
JOIN veh_brands b ON s.brand_id = b.id
WHERE o.created_at >= '2026-01-01' AND o.created_at < '2026-07-01'
GROUP BY b.name ORDER BY b.name"""),
    dict(id="AP24", category="多表JOIN", difficulty="medium",
         question="机电维修类型的工单，各服务中心分别承担了多少张？",
         golden_sql="""SELECT c.name AS center, COUNT(*) AS wo_cnt
FROM svc_work_orders w
JOIN svc_service_centers c ON w.service_center_id = c.id
WHERE w.wo_type = '机电维修'
GROUP BY c.name ORDER BY c.name"""),
    dict(id="AP25", category="多表JOIN", difficulty="hard",
         question="各城市的试驾次数分别是多少？",
         golden_sql="""SELECT d.city AS city, COUNT(*) AS drive_cnt
FROM sal_test_drives td
JOIN sal_dealers d ON td.dealer_id = d.id
GROUP BY d.city ORDER BY d.city"""),
    dict(id="AP26", category="多表JOIN", difficulty="hard",
         question="各品牌的三电维修工单分别有多少张？",
         golden_sql="""SELECT b.name AS brand, COUNT(*) AS wo_cnt
FROM svc_work_orders w
JOIN veh_vehicles v ON w.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series s ON m.series_id = s.id
JOIN veh_brands b ON s.brand_id = b.id
WHERE w.wo_type = '三电维修'
GROUP BY b.name ORDER BY b.name"""),

    # ── 排序TopN：分组聚合 + 偏斜外键（边界值已核对无并列）──
    dict(id="AP27", category="排序TopN", difficulty="hard",
         question="工单量最大的前3个服务中心是哪些？列出名称和工单数",
         golden_sql="""SELECT c.name AS center, COUNT(*) AS wo_cnt
FROM svc_work_orders w
JOIN svc_service_centers c ON w.service_center_id = c.id
GROUP BY c.name ORDER BY wo_cnt DESC LIMIT 3"""),
    dict(id="AP28", category="排序TopN", difficulty="hard",
         question="未完工工单最多的前5个服务中心是哪些？",
         golden_sql="""SELECT c.name AS center, COUNT(*) AS unfinished_cnt
FROM svc_work_orders w
JOIN svc_service_centers c ON w.service_center_id = c.id
WHERE w.finished_at IS NULL
GROUP BY c.name ORDER BY unfinished_cnt DESC LIMIT 5"""),
    dict(id="AP29", category="排序TopN", difficulty="medium",
         question="充电量最大的前3个城市是哪些？列出城市和总电量",
         golden_sql="""SELECT city, SUM(energy_kwh) AS total_kwh
FROM iot_charging_sessions
GROUP BY city ORDER BY total_kwh DESC LIMIT 3"""),
    dict(id="AP30", category="排序TopN", difficulty="hard",
         question="卖得最好的前5个车型（款型）是哪些？按订单数排",
         golden_sql="""SELECT t.name AS trim, COUNT(*) AS order_cnt
FROM sal_sales_orders o
JOIN veh_trims t ON o.trim_id = t.id
GROUP BY t.name ORDER BY order_cnt DESC LIMIT 5"""),

    # ── 明细查询：唯一键单查 / 显式唯一排序键 ──
    dict(id="AP31", category="明细查询", difficulty="easy",
         question="查一下维修工单 SWO00000001 的状态、进厂时间和完工时间",
         golden_sql="""SELECT wo_no, status, intake_at, finished_at
FROM svc_work_orders WHERE wo_no = 'SWO00000001'"""),
    dict(id="AP32", category="明细查询", difficulty="medium",
         question="2026年9月1日到9月15日进厂的维修工单，按工单号从小到大列出前10条的工单号和状态",
         golden_sql="""SELECT wo_no, status
FROM svc_work_orders
WHERE intake_at >= '2026-09-01' AND intake_at < '2026-09-16'
ORDER BY wo_no LIMIT 10"""),
    dict(id="AP33", category="明细查询", difficulty="easy",
         question="订单 SO00000001 的金额、状态和下单时间是什么？",
         golden_sql="""SELECT order_no, amount, status, created_at
FROM sal_sales_orders WHERE order_no = 'SO00000001'"""),
    dict(id="AP34", category="明细查询", difficulty="hard",
         question="维修工单 SWO00000042 对应的车辆属于哪个品牌、哪个车系、哪个款型？",
         golden_sql="""SELECT b.name AS brand, s.name AS series, t.name AS trim
FROM svc_work_orders w
JOIN veh_vehicles v ON w.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series s ON m.series_id = s.id
JOIN veh_brands b ON s.brand_id = b.id
WHERE w.wo_no = 'SWO00000042'"""),
]


def _is_degenerate(rows: list[dict]) -> str:
    """判断结果是否「判分无意义」。返回原因，正常则返回空串。"""
    if not rows:
        return "0 行"
    if len(rows) == 1 and len(rows[0]) == 1:
        v = list(rows[0].values())[0]
        if v is None or (isinstance(v, (int, float, Decimal)) and v == 0):
            return "单行单列且为 0/None（恒过题）"
    return ""


async def main(write: bool) -> None:
    from src.core.config import get_settings

    settings = get_settings()
    dsn = (f"postgresql+asyncpg://{settings.DB_USER}:{settings.DB_PASSWORD}"
           f"@{settings.DB_HOST}:{settings.DB_PORT}/auto_full")
    engine = create_async_engine(dsn)

    ok, bad = [], []
    async with engine.connect() as conn:
        for case in CASES:
            try:
                rows = [dict(r) for r in (await conn.execute(text(case["golden_sql"]))).mappings().all()]
            except Exception as e:
                await conn.rollback()
                bad.append((case["id"], f"执行失败: {str(e)[:70]}"))
                continue
            reason = _is_degenerate(rows)
            if reason:
                bad.append((case["id"], reason))
            else:
                ok.append((case, len(rows), len(rows[0])))
    await engine.dispose()

    print(f"候选 {len(CASES)} 条 → 合格 {len(ok)} 条，不合格 {len(bad)} 条\n")
    for case, nrows, ncols in ok:
        print(f"  ✓ {case['id']} [{case['category']}/{case['difficulty']}] {nrows} 行 × {ncols} 列  {case['question'][:36]}")
    for cid, why in bad:
        print(f"  ✗ {cid}: {why}")

    if write:
        payload = json.loads(CASE_FILE.read_text(encoding="utf-8"))
        existing_ids = {c["id"] for c in payload["cases"]}
        merged = 0
        for case, _, _ in ok:
            if case["id"] not in existing_ids:
                payload["cases"].append(case)
                merged += 1
        CASE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n已合入 {CASE_FILE.name}（新增 {merged} 条，现有 {len(payload['cases'])} 条）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="auto_full 补题（AP 批次：生产环境模拟）")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.write))
