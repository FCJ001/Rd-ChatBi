# ============================================================
# auto_full 补题：把「多跳 JOIN」的占比提上来
#
# 背景（实测画像，补题前 43 题）：
#   JOIN 跳数  0跳 33 条(77%) | 1跳 6 | 2跳 2 | 4跳 1 | 6跳 1
#   答案只有单个数字的题 16 条(37%)
#   → 通过率最低的恰好是 JOIN 多的类别，而题库里几乎没有多跳题。
#      不是系统「不会 JOIN」，是**题库没测**。
#
# 这个脚本做三件事：
#   ① 生成候选案例（大量 2~6 跳 JOIN，覆盖 13 个子域）
#   ② 逐条在真库上跑 golden，**实证合格才收录**：
#      - 执行不报错
#      - 结果非空、且不是「单行全 0」（恒过题）
#      - 时间条件一律用固定日期字面量（不用 now()，否则会随时间漂移成恒空）
#   ③ 写到 --out 指定的文件
#
# 用法：
#   python scripts/gen_auto_full_cases.py                    # 只校验并打印统计
#   python scripts/gen_auto_full_cases.py --write            # 写入案例文件
#   python scripts/gen_auto_full_cases.py --write --out eval/cases/nl2sql_cases_auto_full_v2.json
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

# ── 候选案例 ────────────────────────────────────────────────────────
#
# ★★ 写题时必须避开的一类形态（实测踩过，别重犯）：
#
#   「按数值列排序取前 N」在**这个库**上不可能有唯一答案，给 golden 加
#   tie-breaker 也治不了。原因是种子数据的数值列太稀疏：
#     labor_hours 40000 行只有 75 个不同值、amount 25000 行只有 51 个、
#     cost 95000 行只有 415 个（根因见 auto_full_domain.NUM 的注释）。
#   取 TOP10 时第 10 名的值上往往并列几百行，而**模型不会用 golden 那个
#   隐藏的二级排序键**（题目里推不出来），于是它取到的 10 行必然不同。
#
#   可判分的替代形态（二选一）：
#     ① 分组聚合 + 行数 ≤ 100 + 分组键唯一（如「各服务中心的工时统计」）
#     ② 排序键用离散枚举列（如 road_type/charge_type），值少但无并列
#
#
# 设计原则：
#   ★ 尽量 2 跳以上（这是本次补题的目的）
#   ★ 时间条件用固定日期字面量（2025-09-01 ~ 2026-09-16 是种子数据区间）
#   ★ 避开「答案就是一个数字」的形态：优先返回多行分组结果
#   ★ 覆盖多个子域：销售/售后/生产/质量/电池/智驾/OTA/供应链/PLM
CASES: list[dict] = [
    # ── 销售域：4~6 跳（订单→交付→整车→款型→车系→品牌）──
    dict(id="AF01", category="多表JOIN", difficulty="hard",
         question="各个汽车品牌分别卖出了多少台车？（按订单数统计）",
         golden_sql="""SELECT b.name AS brand, COUNT(*) AS order_cnt
FROM sal_sales_orders o
JOIN sal_deliveries d ON d.order_id = o.id
JOIN veh_vehicles v ON d.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series ms ON m.series_id = ms.id
JOIN veh_brands b ON ms.brand_id = b.id
GROUP BY b.name ORDER BY order_cnt DESC"""),
    dict(id="AF02", category="多表JOIN", difficulty="hard",
         question="各大区、各品牌的销售额分别是多少？",
         golden_sql="""SELECT r.name AS region, b.name AS brand, SUM(o.amount) AS total_amount
FROM sal_sales_orders o
JOIN sal_regions r ON o.region_id = r.id
JOIN sal_deliveries d ON d.order_id = o.id
JOIN veh_vehicles v ON d.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series ms ON m.series_id = ms.id
JOIN veh_brands b ON ms.brand_id = b.id
GROUP BY r.name, b.name ORDER BY total_amount DESC"""),
    dict(id="AF03", category="多表JOIN", difficulty="hard",
         question="各个工厂生产的车分别卖给了哪些大区的客户？统计每个工厂-大区组合的订单量",
         golden_sql="""SELECT p.name AS plant, r.name AS region, COUNT(*) AS cnt
FROM sal_sales_orders o
JOIN sal_deliveries d ON d.order_id = o.id
JOIN veh_vehicles v ON d.vehicle_id = v.id
JOIN mfg_plants p ON v.plant_id = p.id
JOIN sal_regions r ON o.region_id = r.id
GROUP BY p.name, r.name ORDER BY cnt DESC"""),
    dict(id="AF04", category="多表JOIN", difficulty="hard",
         question="各款型的平均成交金额是多少？按款型名称列出",
         golden_sql="""SELECT t.name AS trim, ROUND(AVG(o.amount), 2) AS avg_amount, COUNT(*) AS cnt
FROM sal_sales_orders o
JOIN veh_trims t ON o.trim_id = t.id
GROUP BY t.name ORDER BY avg_amount DESC"""),
    dict(id="AF05", category="多表JOIN", difficulty="hard",
         question="经销商数量最多的前 5 个大区，各自的订单总量是多少？",
         golden_sql="""SELECT r.name AS region, COUNT(DISTINCT d2.id) AS dealer_cnt, COUNT(o.id) AS order_cnt
FROM sal_regions r
JOIN sal_dealers d2 ON d2.region_id = r.id
LEFT JOIN sal_sales_orders o ON o.region_id = r.id
GROUP BY r.name ORDER BY dealer_cnt DESC, order_cnt DESC LIMIT 5"""),

    # ── 售后域：3~4 跳（工单→整车→款型→品牌；工单→技师/服务中心→大区）──
    dict(id="AF06", category="多表JOIN", difficulty="hard",
         question="各品牌的售后维修工单数分别是多少？",
         golden_sql="""SELECT b.name AS brand, COUNT(*) AS wo_cnt
FROM svc_work_orders wo
JOIN veh_vehicles v ON wo.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series ms ON m.series_id = ms.id
JOIN veh_brands b ON ms.brand_id = b.id
GROUP BY b.name ORDER BY wo_cnt DESC"""),
    dict(id="AF07", category="多表JOIN", difficulty="medium",
         question="各大区的维修工单总数和平均工时分别是多少？",
         golden_sql="""SELECT r.name AS region, COUNT(*) AS wo_cnt, ROUND(AVG(wo.labor_hours), 2) AS avg_hours
FROM svc_work_orders wo
JOIN svc_service_centers sc ON wo.service_center_id = sc.id
JOIN sal_regions r ON sc.region_id = r.id
GROUP BY r.name ORDER BY wo_cnt DESC"""),
    dict(id="AF08", category="多表JOIN", difficulty="hard",
         question="每个技师完成的工单数和平均工时，按工单数降序取前 10 名",
         golden_sql="""SELECT m.name AS mechanic, COUNT(*) AS wo_cnt, ROUND(AVG(wo.labor_hours), 2) AS avg_hours
FROM svc_work_orders wo
JOIN svc_mechanics m ON wo.mechanic_id = m.id
GROUP BY m.name ORDER BY wo_cnt DESC LIMIT 10"""),
    dict(id="AF09", category="多表JOIN", difficulty="hard",
         question="维修工单里消耗金额最高的前 5 个零件名称和总金额",
         golden_sql="""SELECT p.name AS part, ROUND(SUM(wp.amount), 2) AS total_amount
FROM svc_wo_parts wp
JOIN plm_parts p ON wp.part_id = p.id
GROUP BY p.name ORDER BY total_amount DESC LIMIT 5"""),

    # ── 生产域：3~4 跳 ──
    dict(id="AF10", category="多表JOIN", difficulty="hard",
         question="各工厂、各车型的生产记录条数分别是多少？",
         golden_sql="""SELECT p.name AS plant, m.name AS model, COUNT(*) AS cnt
FROM mfg_production_records pr
JOIN mfg_plants p ON pr.plant_id = p.id
JOIN veh_models m ON pr.model_id = m.id
GROUP BY p.name, m.name ORDER BY cnt DESC"""),
    dict(id="AF11", category="多表JOIN", difficulty="hard",
         question="各车间的设备故障次数排行，列出车间名和故障数",
         golden_sql="""SELECT w.name AS workshop, COUNT(*) AS fault_cnt
FROM mfg_equipment_faults ef
JOIN mfg_equipment e ON ef.equipment_id = e.id
JOIN mfg_workshops w ON e.workshop_id = w.id
GROUP BY w.name ORDER BY fault_cnt DESC"""),
    dict(id="AF12", category="多表JOIN", difficulty="hard",
         question="各整车品牌在质量检验中不合格的记录数是多少？",
         golden_sql="""SELECT b.name AS brand, COUNT(*) AS fail_cnt
FROM mfg_quality_inspections qi
JOIN veh_vehicles v ON qi.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series ms ON m.series_id = ms.id
JOIN veh_brands b ON ms.brand_id = b.id
WHERE qi.result <> 'pass'
GROUP BY b.name ORDER BY fail_cnt DESC"""),

    # ── 质量域：3~4 跳 ──
    dict(id="AF13", category="多表JOIN", difficulty="hard",
         question="各供应商的 PPM 记录平均 PPM 值是多少？按供应商名称列出",
         # ★ ORDER BY 必须带唯一 tie-breaker：只按 avg_ppm 排序会有并列，
         #   判分器无法确定"这一组应该一一对应到哪一行"（实测被判无唯一答案）
         golden_sql="""SELECT s.name AS supplier, ROUND(AVG(pp.ppm), 2) AS avg_ppm
FROM qms_ppm_records pp
JOIN plm_suppliers s ON pp.supplier_id = s.id
GROUP BY s.name ORDER BY avg_ppm DESC, s.name ASC"""),
    dict(id="AF14", category="多表JOIN", difficulty="hard",
         question="各工厂的体系审核发现项数量统计，按发现项数降序",
         golden_sql="""SELECT p.name AS plant, COUNT(*) AS finding_cnt
FROM qms_audit_findings f
JOIN qms_audits a ON f.audit_id = a.id
JOIN mfg_plants p ON a.plant_id = p.id
GROUP BY p.name ORDER BY finding_cnt DESC"""),

    # ── 电池域：3~4 跳（电池包→整车→款型→品牌；电池包→供应商）──
    dict(id="AF15", category="多表JOIN", difficulty="hard",
         question="各品牌的电池包故障记录数分别是多少？",
         golden_sql="""SELECT b.name AS brand, COUNT(*) AS fault_cnt
FROM bat_battery_faults bf
JOIN bat_battery_packs bp ON bf.pack_id = bp.id
JOIN veh_vehicles v ON bp.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series ms ON m.series_id = ms.id
JOIN veh_brands b ON ms.brand_id = b.id
GROUP BY b.name ORDER BY fault_cnt DESC"""),
    dict(id="AF16", category="多表JOIN", difficulty="hard",
         question="各电池供应商的电池包平均健康度（SOH）是多少？",
         golden_sql="""SELECT s.name AS supplier, ROUND(AVG(soh.soh), 2) AS avg_soh, COUNT(*) AS cnt
FROM bat_soh_records soh
JOIN bat_battery_packs bp ON soh.pack_id = bp.id
JOIN plm_suppliers s ON bp.supplier_id = s.id
GROUP BY s.name ORDER BY avg_soh DESC"""),

    # ── 智驾域：3~4 跳 ──
    dict(id="AF17", category="多表JOIN", difficulty="hard",
         question="各车型的智驾测试接管事件数统计",
         golden_sql="""SELECT m.name AS model, COUNT(*) AS cnt
FROM ad_disengagements ad
JOIN ad_test_drives td ON ad.test_drive_id = td.id
JOIN ad_test_vehicles tv ON td.test_vehicle_id = tv.id
JOIN veh_models m ON tv.model_id = m.id
GROUP BY m.name ORDER BY cnt DESC"""),
    dict(id="AF18", category="多表JOIN", difficulty="hard",
         question="各类道路类型的智驾场景通过率分别是多少百分比？",
         golden_sql="""SELECT s.road_type,
       ROUND(COUNT(*) FILTER (WHERE sr.passed = 'yes') * 100.0 / COUNT(*), 2) AS pass_rate
FROM ad_scenario_runs sr
JOIN ad_scenarios s ON sr.scenario_id = s.id
GROUP BY s.road_type ORDER BY pass_rate DESC"""),

    # ── OTA 域：3~4 跳 ──
    dict(id="AF19", category="多表JOIN", difficulty="hard",
         question="各品牌的 OTA 安装失败次数分别是多少？",
         golden_sql="""SELECT b.name AS brand, COUNT(*) AS fail_cnt
FROM ota_install_failures f
JOIN ota_installations i ON f.installation_id = i.id
JOIN veh_vehicles v ON i.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series ms ON m.series_id = ms.id
JOIN veh_brands b ON ms.brand_id = b.id
GROUP BY b.name ORDER BY fail_cnt DESC"""),

    # ── 供应链 / PLM：3 跳 ──
    dict(id="AF20", category="多表JOIN", difficulty="hard",
         question="各零件类别的采购订单行数量统计",
         golden_sql="""SELECT pc.name AS category, COUNT(*) AS line_cnt
FROM pur_po_lines pl
JOIN plm_parts p ON pl.part_id = p.id
JOIN plm_part_categories pc ON p.category_id = pc.id
GROUP BY pc.name ORDER BY line_cnt DESC"""),
    dict(id="AF21", category="多表JOIN", difficulty="hard",
         question="各供应商的零件数量是多少？按零件数降序",
         golden_sql="""SELECT s.name AS supplier, COUNT(*) AS part_cnt
FROM plm_supplier_parts sp
JOIN plm_suppliers s ON sp.supplier_id = s.id
GROUP BY s.name ORDER BY part_cnt DESC, s.name ASC"""),

    # ── 明细类：多跳 + 明确排序，避开「任意 N 行」 ══
    dict(id="AF22", category="明细查询", difficulty="hard",
         question="列出维修工时最长的 10 张工单，包含工单号、技师姓名和服务中心名称",
         golden_sql="""SELECT wo.id, m.name AS mechanic, sc.name AS service_center, wo.labor_hours
FROM svc_work_orders wo
JOIN svc_mechanics m ON wo.mechanic_id = m.id
JOIN svc_service_centers sc ON wo.service_center_id = sc.id
ORDER BY wo.labor_hours DESC, wo.id ASC LIMIT 10"""),
    dict(id="AF23", category="明细查询", difficulty="hard",
         question="列出交付金额最高的 10 张订单，包含订单号、大区名称和经销商名称",
         golden_sql="""SELECT o.id, r.name AS region, d.name AS dealer, o.amount
FROM sal_sales_orders o
JOIN sal_regions r ON o.region_id = r.id
JOIN sal_dealers d ON o.dealer_id = d.id
ORDER BY o.amount DESC, o.id ASC LIMIT 10"""),
    dict(id="AF24", category="明细查询", difficulty="medium",
         question="列出故障次数最多的 10 台设备，包含设备名称和所属车间",
         golden_sql="""SELECT e.name AS equipment, w.name AS workshop, COUNT(*) AS fault_cnt
FROM mfg_equipment_faults ef
JOIN mfg_equipment e ON ef.equipment_id = e.id
JOIN mfg_workshops w ON e.workshop_id = w.id
GROUP BY e.name, w.name ORDER BY fault_cnt DESC, e.name ASC LIMIT 10"""),

    # ── 时间窗口 + 多跳（固定日期，不漂移）──
    dict(id="AF25", category="时间窗口", difficulty="hard",
         question="2026年上半年（1~6月）各大区的销售订单量分别是多少？",
         golden_sql="""SELECT r.name AS region, COUNT(*) AS cnt
FROM sal_sales_orders o
JOIN sal_regions r ON o.region_id = r.id
WHERE o.created_at >= '2026-01-01' AND o.created_at < '2026-07-01'
GROUP BY r.name ORDER BY cnt DESC"""),
    dict(id="AF26", category="时间窗口", difficulty="hard",
         question="2026年第一季度各品牌的售后工单数统计",
         golden_sql="""SELECT b.name AS brand, COUNT(*) AS wo_cnt
FROM svc_work_orders wo
JOIN veh_vehicles v ON wo.vehicle_id = v.id
JOIN veh_trims t ON v.trim_id = t.id
JOIN veh_models m ON t.model_id = m.id
JOIN veh_model_series ms ON m.series_id = ms.id
JOIN veh_brands b ON ms.brand_id = b.id
WHERE wo.intake_at >= '2026-01-01' AND wo.intake_at < '2026-04-01'
GROUP BY b.name ORDER BY wo_cnt DESC"""),

    # ── 排序 TopN + 多跳 ──
    dict(id="AF27", category="排序TopN", difficulty="hard",
         question="卖得最好的前 5 个经销商，各自的订单数和总销售额是多少？",
         golden_sql="""SELECT d.name AS dealer, COUNT(*) AS order_cnt, ROUND(SUM(o.amount), 2) AS total_amount
FROM sal_sales_orders o
JOIN sal_dealers d ON o.dealer_id = d.id
GROUP BY d.name ORDER BY total_amount DESC LIMIT 5"""),
]


def _is_degenerate(rows: list[dict]) -> str:
    """判断结果是否「判分无意义」。返回原因，正常则返回空串。"""
    if not rows:
        return "0 行"
    if len(rows) == 1 and len(rows[0]) == 1:
        v = list(rows[0].values())[0]
        if v is None or (isinstance(v, (int, float, Decimal)) and v == 0):
            return "单行单列且为 0/None"
    return ""


async def main(write: bool, out_path: Path) -> None:
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
        payload = {"datasource": "auto_full", "cases": [c for c, _, _ in ok]}
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n已写入 {out_path}（{len(ok)} 条）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="auto_full 补题（多跳 JOIN）")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--out", type=Path,
                    default=REPO_ROOT / "eval" / "cases" / "nl2sql_cases_auto_full_v2.json")
    a = ap.parse_args()
    asyncio.run(main(a.write, a.out))
