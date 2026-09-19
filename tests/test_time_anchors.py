# ============================================================
# 时间锚点生成器单测（升级方案 P1）
#
# 防回归的就是那个实测 badcase：NOW()-interval ≠ 自然月。
# 用固定日期断言边界，杜绝"7月只算了12天"这类静默错误。
# ============================================================

from datetime import date, datetime

from src.nl2sql.example_store import (
    example_id,
    format_examples_block,
    normalize_question,
)
from src.nl2sql.time_anchors import build_time_anchors, format_anchor_block


def _anchors(y, m, d):
    return build_time_anchors(datetime(y, m, d, 10, 30))


def test_adjacent_months_mid_month():
    """badcase 复现场景：2026-09-19 的上月/上上月必须是完整自然月"""
    a = _anchors(2026, 9, 19)
    assert a["上个月"] == "2026-08-01 ~ 2026-08-31"
    assert a["上上个月"] == "2026-07-01 ~ 2026-07-31"
    assert a["本月"] == "2026-09-01 ~ 2026-09-30"


def test_adjacent_months_year_boundary():
    """1月的上上月跨年"""
    a = _anchors(2026, 1, 15)
    assert a["上个月"] == "2025-12-01 ~ 2025-12-31"
    assert a["上上个月"] == "2025-11-01 ~ 2025-11-30"
    assert a["今年"] == "2026-01-01 ~ 2026-12-31"
    assert a["去年"] == "2025-01-01 ~ 2025-12-31"


def test_month_start_and_end_edges():
    """月初/月末时点：本月/上月仍是完整月"""
    assert _anchors(2026, 8, 1)["本月"] == "2026-08-01 ~ 2026-08-31"
    assert _anchors(2024, 2, 29)["本月"] == "2024-02-01 ~ 2024-02-29"  # 闰年
    assert _anchors(2026, 3, 31)["上个月"] == "2026-02-01 ~ 2026-02-28"


def test_week_monday_start():
    a = _anchors(2026, 9, 19)  # 周六
    assert a["本周"] == "2026-09-14 ~ 2026-09-20"   # 周一起
    assert a["上周"] == "2026-09-07 ~ 2026-09-13"


def test_rolling_windows_inclusive():
    a = _anchors(2026, 9, 19)
    assert a["近7天"] == "2026-09-13 ~ 2026-09-19"
    assert a["近30天"] == "2026-08-21 ~ 2026-09-19"


def test_quarters():
    a = _anchors(2026, 9, 19)
    assert a["本季度"] == "2026-07-01 ~ 2026-09-30"
    assert a["上季度"] == "2026-04-01 ~ 2026-06-30"
    assert _anchors(2026, 1, 5)["上季度"] == "2025-10-01 ~ 2025-12-31"


def test_anchor_block_contains_hard_rules():
    block = format_anchor_block(datetime(2026, 9, 19))
    assert "禁止" in block and "上个月：2026-08-01 ~ 2026-08-31" in block
    assert "闭区间" in block
    # ★ TIMESTAMP 半开区间规则（badcase：<= '2026-08-31' 丢掉 8/31 白天数据）
    assert "半开区间" in block and "< '2026-09-01'" in block


def test_example_normalize_and_dedup():
    """示例库防自泄漏：标点/空白差异不绕过去重"""
    assert normalize_question("上个月 和上上个月？") == normalize_question("上个月和上上个月")
    assert example_id("问题 A") == example_id("问题A ")
    assert example_id("问题A") != example_id("问题B")


def test_format_examples_block():
    assert format_examples_block([]) == ""
    block = format_examples_block([{"question": "各品牌销量？", "sql": "SELECT 1"}])
    assert "各品牌销量" in block and "SELECT 1" in block and "不要照抄" in block
