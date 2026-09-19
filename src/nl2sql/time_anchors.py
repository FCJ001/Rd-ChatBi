# ============================================================
# 时间锚点生成器（升级方案 P1）
#
# 背景：实测 badcase —— 问"上个月和上上个月相比变化多少"，LLM 用
# NOW() - INTERVAL '2 months' 当自然月边界，7 月只被统计了 12 天，
# 错误值 4518 vs 正确值 0，且 SQL 完全合法、安全层拦不住。
#
# 方案（对齐 Cube compareDateRange / MetricFlow offset 的共识）：
# 相对时间边界由 Python 预计算成字符串注入 prompt，LLM 只"抄"不算。
# ============================================================

from datetime import date, datetime, timedelta

# 锚点顺序 = prompt 里的展示顺序
_ANCHOR_ORDER = [
    "今天", "昨天", "本周", "上周", "本月", "上个月", "上上个月",
    "本季度", "上季度", "今年", "去年", "近7天", "近30天", "近90天",
]


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    """d 所在月的最后一天（含 d 当月）"""
    nxt = (_month_start(d) + timedelta(days=32)).replace(day=1)
    return nxt - timedelta(days=1)


def _quarter_start(d: date) -> date:
    return date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)


def build_time_anchors(now: datetime) -> dict[str, str]:
    """按当前时刻预计算常用相对周期的自然边界（闭区间，含两端）。

    返回 {锚点名: "YYYY-MM-DD ~ YYYY-MM-DD"}。"""
    today = now.date()
    monday = today - timedelta(days=today.weekday())  # 周一为一周开始
    m_start, m_end = _month_start(today), _month_end(today)
    prev_end = m_start - timedelta(days=1)            # 上月最后一天
    prev_start = _month_start(prev_end)
    prev2_end = prev_start - timedelta(days=1)        # 上上月最后一天
    prev2_start = _month_start(prev2_end)
    q_start = _quarter_start(today)
    prev_q_end = q_start - timedelta(days=1)
    prev_q_start = _quarter_start(prev_q_end)

    rng = lambda a, b: f"{a.isoformat()} ~ {b.isoformat()}"  # noqa: E731
    anchors = {
        "今天": rng(today, today),
        "昨天": rng(today - timedelta(days=1), today - timedelta(days=1)),
        "本周": rng(monday, monday + timedelta(days=6)),
        "上周": rng(monday - timedelta(days=7), monday - timedelta(days=1)),
        "本月": rng(m_start, m_end),
        "上个月": rng(prev_start, prev_end),
        "上上个月": rng(prev2_start, prev2_end),
        "本季度": rng(q_start, _quarter_end(q_start)),
        "上季度": rng(prev_q_start, prev_q_end),
        "今年": rng(date(today.year, 1, 1), date(today.year, 12, 31)),
        "去年": rng(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
        "近7天": rng(today - timedelta(days=6), today),
        "近30天": rng(today - timedelta(days=29), today),
        "近90天": rng(today - timedelta(days=89), today),
    }
    return {k: anchors[k] for k in _ANCHOR_ORDER}


def _quarter_end(q_start: date) -> date:
    return _month_end(q_start + timedelta(days=64))


# 头部硬规则（单一来源）：format_anchor_block 与节点的 render_anchor_lines 共用，
# 杜绝"兜底路径渲染出少了两条硬规则的简化版"这种措辞漂移。
_ANCHOR_RULES_HEADER = [
    "## 时间锚点（按当前日期预计算的自然周期边界，直接取用，禁止自行推算）",
    "★ 规则：涉及相对时间（上个月/上周/近30天等）时，必须使用下列精确边界，"
    "禁止用 NOW()/CURRENT_DATE 加减 INTERVAL 自行推算。边界为闭区间（含首尾两天）。",
    "★★ TIMESTAMP/DATETIME 列的时间过滤必须写成半开区间：col >= '起始日' AND col < '结束日次日'，"
    "禁止 col <= '结束日'（'2026-08-31' 字面量是当天零点，<= 会丢掉末日白天的数据）。",
    "   例：上个月 [2026-08-01, 2026-08-31] → WHERE ts >= '2026-08-01' AND ts < '2026-09-01'；"
    "DATE 列才可用 BETWEEN '起始日' AND '结束日'。",
]


def format_anchor_block(now: datetime | None = None) -> str:
    """完整锚点区块：硬规则头部 + 当前时刻的全量边界（引擎路径直接调用）。"""
    anchors = build_time_anchors(now or datetime.now())
    return render_anchor_lines([f"{name}：{span}" for name, span in anchors.items()])


def render_anchor_lines(anchor_lines: list[str]) -> str:
    """把 add_context 产出的锚点行渲染成 prompt 区块（generate_sql 节点用）。
    与 format_anchor_block 共用同一头部 —— 换任何一个，两条路径一起变。"""
    return "\n".join(_ANCHOR_RULES_HEADER + [f"- {a}" for a in anchor_lines])
