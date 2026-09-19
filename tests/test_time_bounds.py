# ============================================================
# 数据时间上界归因 单测
#
# 背景（实测生产缺陷）：种子数据的时间上界停在生成脚本跑的那天（09-16），
# 而 now() 永远在它之后。用户问「这个月17号比上个月17号的销量对比」，
# 系统答「本月17号销量归零…呈断崖式下滑」—— 把"没有数据"说成了"业务为零"。
#
# 这类错误比单纯的答错更危险：它给出的是**确定性的业务结论**，业务方会
# 据此去查一个不存在的故障。
#
# 这组测试锁住判定口径。★ 边界极易写反（改之前先想清楚）：
#   max_ts = "2026-09-16 23:00:00" 表示 **09-16 这一天有数据**，只是最后一
#   小时没有。所以窗口终点 == 09-16 在内，== 09-17 就已越界。
# ============================================================

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.nl2sql.time_bounds import (  # noqa: E402
    TimeBounds,
    beyond_data_upper_bound,
    looks_empty,
    render_time_bounds,
)

BOUNDS = [TimeBounds("sal_sales_orders", "created_at",
                     "2025-09-01 00:00:00", "2026-09-16 23:00:00")]


def _sql(cond: str) -> str:
    return f"SELECT COUNT(*) FROM sal_sales_orders WHERE {cond}"


def test_window_end_beyond_upper_bound_is_flagged():
    """窗口终点晚于数据最后一天 → 必须给出"数据尚未产生"的事实"""
    note = beyond_data_upper_bound(
        _sql("created_at >= '2026-09-17' AND created_at < '2026-09-18'"), BOUNDS)
    assert note
    assert "2026-09-16" in note          # 说清数据截止到哪天
    assert "尚未产生" in note
    assert "不是" in note and "业务量为 0" in note


def test_window_ending_on_last_data_day_is_not_flagged():
    """★ 边界：终点正好是最后一天 → 在内，不该报。
    max_ts 的 23:00 说明这一天是有数据的，报了就成误报。"""
    assert beyond_data_upper_bound(
        _sql("created_at >= '2026-09-15' AND created_at < '2026-09-16'"), BOUNDS) == ""


def test_window_ending_the_day_after_is_flagged():
    """★ 边界另一侧：终点是其次日 → 越界。零宽限，不加'若干天'豁免 ——
    实测数据停在生成那一刻，次日就是彻底没有。"""
    assert beyond_data_upper_bound(
        _sql("created_at >= '2026-09-16' AND created_at < '2026-09-17'"), BOUNDS)


def test_in_range_window_not_flagged():
    """窗口完全在数据范围内 → 不报（避免把正常查询的摘要也污染掉）"""
    assert beyond_data_upper_bound(
        _sql("created_at >= '2026-08-01' AND created_at < '2026-09-01'"), BOUNDS) == ""


def test_multi_window_only_needs_one_beyond():
    """★ 关键回归：「这个月17号 vs 上个月17号」的 SQL 里**同时**有超界的
    '2026-09-17' 和在范围内的 '2026-08-17'。早先按"SQL 里出现的任意日期"判定，
    会被在内那个冲掉而漏报 —— 必须只看每个窗口的**终点**。"""
    sql = _sql("created_at >= '2026-09-17' AND created_at < '2026-09-18'"
               " OR created_at >= '2026-08-17' AND created_at < '2026-08-18'")
    assert beyond_data_upper_bound(sql, BOUNDS)


def test_between_closed_range_uses_upper_end():
    """DATE 列的闭区间写法（BETWEEN）取上界作为终点"""
    assert beyond_data_upper_bound(
        _sql("d BETWEEN '2026-09-01' AND '2026-09-30'"), BOUNDS)
    assert beyond_data_upper_bound(
        _sql("d BETWEEN '2026-08-01' AND '2026-08-31'"), BOUNDS) == ""


def test_no_bounds_no_note():
    """探测失败（空列表）→ 不编造上界，返回空串"""
    assert beyond_data_upper_bound(
        _sql("created_at < '2030-01-01'"), []) == ""


def test_no_date_literals_no_note():
    assert beyond_data_upper_bound(
        "SELECT COUNT(*) FROM t WHERE status = 'open'", BOUNDS) == ""


# ══ 语义空判定：COUNT 形态也是"空" ═══════════════════════════════════

def test_looks_empty_covers_count_zero():
    """★ COUNT(*) 永远返回 1 行 —— 只判 `not rows` 会漏掉最典型的
    "把没有数据说成业务为零"场景（实测「今天有多少张订单」返回 [{cnt: 0}]）。"""
    assert looks_empty([])
    assert looks_empty([{"cnt": 0}])
    assert looks_empty([{"cnt": 0, "amount": None}])
    assert not looks_empty([{"cnt": 5}])
    assert not looks_empty([{"a": 0}, {"b": 1}])   # 多行不算空


# ══ prompt 渲染 ═════════════════════════════════════════════════════

def test_render_lists_every_table_with_global_latest():
    """★ 不去重：实测各表上界并不一致（mfg_plants 停在 2025-10-13，
    sal_sales_orders 到 2026-09-16），按上界去重会把另一批藏掉。"""
    bounds = BOUNDS + [TimeBounds("mfg_plants", "created_at",
                                  "2025-09-08 01:00:00", "2025-10-13 06:00:00")]
    out = render_time_bounds(bounds)
    assert "2026-09-16" in out and "2025-10-13" in out
    assert "sal_sales_orders" in out and "mfg_plants" in out


def test_render_empty_returns_empty_string():
    assert render_time_bounds([]) == ""
