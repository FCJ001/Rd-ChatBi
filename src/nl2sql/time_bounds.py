# ============================================================
# 数据时间上界探测（生产缺陷修复 §9.3）
#
# 背景：实测生产场景「这个月17号比上个月17号的销量对比」，系统答
#   「本月17号销量归零…呈断崖式下滑」，而真相是**那天根本没有数据**——
#   种子数据的时间上界停在生成脚本跑的那天（2026-09-16），now() 在其之后。
#   系统从不告诉 LLM 数据到哪天为止，于是"查不到"被摘要解读成"业务为零"。
#
# 同类实测：最近7天的问题模型答对了（能看出「只返回4天 ≠ 7天」这个算术矛盾），
#   而"这个月17号"答错 —— 差别在于后者需要知道数据上界，而**没人告诉它**。
#
# 修法：执行前查一次各时间列的真实上下界，注入 prompt，并用于在
#   结果为空时给出正确的归因（"数据只到 X" 而不是"销量为零"）。
# ============================================================

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as _date, timedelta as _td

from sqlalchemy import text

from src.core.logger import logger

# 探测哪些表：只查**主事实表**，不查维表。维表要么没有时间列，要么时间无意义
# （如 veh_brands.created_at），全查一遍 127 张表会拖慢每次请求。
# 名字里带这些前缀的是事实表（与 gen_auto_full.py 的命名约定一致）。
_FACT_PREFIXES = ("sal_", "svc_", "veh_", "mfg_", "iot_", "ad_", "qms_", "rcl_", "alm_")

# 每张表最多探测这几列（生成的数据里时间列就这几种命名）
_TIME_COLUMN_HINTS = (
    "created_at", "occurred_at", "delivered_at", "started_at",
    "surveyed_at", "build_date", "install_date", "detected_at",
)


@dataclass(frozen=True)
class TimeBounds:
    """某列的时间上下界。min/max 都是 ISO 字符串（便于直接进 prompt）。"""
    table: str
    column: str
    min_ts: str
    max_ts: str

    def render(self) -> str:
        return f"{self.table}.{self.column}: {self.min_ts} ~ {self.max_ts}"


async def _list_tables(db) -> list[str]:
    """直接问 information_schema 要表名。

    ★ 不依赖 pg_meta_repo：旧引擎路径（engine.run_query）拿不到元数据仓，
      但它有业务库连接 —— 这样两条链路都能用同一份探测逻辑。"""
    try:
        rows = (await db.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"))).scalars().all()
        return list(rows)
    except Exception:
        await _safe_rollback(db)
        return []


async def detect_time_bounds(db, meta_tables: list[str] | None = None,
                             max_tables: int = 40) -> list[TimeBounds]:
    """探测业务库各事实表时间列的上下界。

    ★ 失败绝不抛：这只是给 prompt 补充上下文，探测不到就退化成现状
      （LLM 自行判断），不能因为在它上面出错而让整个查询挂掉。
    ★ 走调用方传入的 session（业务库只读连接），不新建连接 —— 与
      add_context.detect_db_info 同一条链路。
    """
    if db is None:
        return []
    candidates = meta_tables if meta_tables is not None else await _list_tables(db)
    tables = [t for t in candidates if t.startswith(_FACT_PREFIXES)]
    out: list[TimeBounds] = []
    for table in sorted(tables)[:max_tables]:
        for col in _TIME_COLUMN_HINTS:
            try:
                row = (await db.execute(text(
                    f'SELECT MIN("{col}"), MAX("{col}") FROM "{table}"'
                ))).one()
            except Exception:
                # 该表没有这一列 → 正常，继续试下一列；表不存在也走这里
                await _safe_rollback(db)
                continue
            lo, hi = row[0], row[1]
            if lo is None or hi is None:
                continue
            out.append(TimeBounds(table=table, column=col,
                                  min_ts=str(lo)[:19], max_ts=str(hi)[:19]))
            break   # 一张表只取第一个命中的时间列，避免同一张表占多行
    return out


async def _safe_rollback(db) -> None:
    """列不存在会把事务打成 aborted 态，必须逐条回滚否则后续全报
    InFailedSQLTransactionError —— 本次会话在评测脚本里踩过同一个坑。"""
    try:
        await db.rollback()
    except Exception:
        pass


def render_time_bounds(bounds: list[TimeBounds]) -> str:
    """渲染进 prompt 的「数据时间范围」区块。空列表返回空串（不污染 prompt）。

    ★ 不去重：实测各表上界**并不一致**（mfg_plants 停在 2025-10-13，
      sal_sales_orders 到 2026-09-16）。按 max_ts 去重会只留一批、把另一批
      藏掉，而"用户问的那张表数据到哪天"正是要回答的问题。
    ★ 排序按表名：让同前缀的表相邻，便于人读也便于模型定位。
    """
    if not bounds:
        return ""
    latest = max(b.max_ts for b in bounds)
    lines = [
        "## 数据时间范围（业务库各表数据的真实起止，必须据此判断“有没有数据”）",
        f"★ 全库最新数据时间：{latest}。",
        "★ 硬规则：用户问的时间段若**超出对应表的范围**，说明该时段数据尚未产生，"
        "应如实回答“该时间段暂无数据”，**禁止**解释成“业务量为 0”或“环比下滑/异常”。",
    ]
    for b in sorted(bounds, key=lambda x: (x.table, x.column)):
        lines.append(f"- {b.table}.{b.column}: {b.min_ts} ~ {b.max_ts}")
    return "\n".join(lines)


def looks_empty(rows: list[dict]) -> bool:
    """结果是否**语义为空**：0 行，或 COUNT 形态（单行且所有数值列都是 0/None）。

    ★ 必须区分「0 行」和「COUNT 返回 0」——实测「今天有多少张订单」返回的是
      `[{cnt: 0}]`（1 行），只判 `not rows` 会漏掉它，而那正是最典型的
      "把没有数据说成业务为零"的场景。
    """
    if not rows:
        return True
    if len(rows) == 1:
        vals = list(rows[0].values())
        return bool(vals) and all(
            v is None or (isinstance(v, (int, float)) and v == 0)
            for v in vals
        )
    return False


# ★ 只抓**右开区间的上界**（`col < '2026-09-18'` / `<= '...'`），不抓左边界。
#
#   为什么不能用「SQL 里出现的所有日期」：实测「这个月17号比上个月17号」的
#   SQL 里既有 '2026-09-17'（超界）也有 '2026-08-17'（在内），按"任意日期超界"
#   判定会被在内那个日期冲掉，漏报 —— 而那正是最典型的误报场景。
#
#   右开区间的上界才是"这个查询问到了哪一天"的真正终点：标准写法是
#   `col >= '起始' AND col < '结束次日'`（半开区间，见 prompt 硬规则），
#   若该终点超出数据上界，说明窗口伸进了无数据区。
_WINDOW_END_RE = re.compile(r"(?:<|<=)\s*(?:TIMESTAMP\s+)?'(\d{4}-\d{2}-\d{2})")
# 闭区间写法（DATE 列专用）：BETWEEN 'a' AND 'b' —— 取 b 作为终点
_BETWEEN_END_RE = re.compile(r"BETWEEN\s+'(\d{4}-\d{2}-\d{2})'\s+AND\s+'(\d{4}-\d{2}-\d{2})'")


def beyond_data_upper_bound(sql: str, bounds: list[TimeBounds],
                            window_days: int = 0) -> str:
    """判断 SQL 引用的日期是否超出数据上界；是则返回事实陈述，否则空串。

    ★ 为什么必须用代码判定而不是"告诉摘要模型让它注意"：实测把上界事实塞进
      摘要 prompt 后，模型仍先写「处于明显异常低位」，**然后**才补一句免责
      声明 —— 结论在前、免责在后，用户读到的是前者。上界是**可判定的事实**，
      不该交给模型去"注意"。这里直接比对，命中就给出确定结论。

    ★ 为什么不是"结果为空才算"：实测「这个月17号比上个月17号」是多列对比
      （`{本月:0, 上月:66, 差:-66}`），没有任何一列"全零"，按空结果判定会
      漏掉它 —— 而那正是最典型的误报场景。

    ★ 判定口径（这里错一次就全量误报或全量漏报，改之前先想清楚）：
      `max_ts` 形如 "2026-09-16 23:00:00" —— **它表示 09-16 这一天是有数据的**
      （只是最后一小时没数据）。所以：
        · 窗口终点 == 09-16  → 在内，不报
        · 窗口终点 == 09-17  → **越界，报**
      默认 window_days=0（零宽限）。★ 不要加"若干天宽限"：实测数据是停在
      生成脚本跑的那一刻，次日就是彻底没有；加宽限会让 09-17/09-18 这类
      **确定越界**的窗口被豁免掉，而它们正是要抓的目标。
    """
    if not bounds or not sql:
        return ""
    ends = _WINDOW_END_RE.findall(sql)
    ends += [b for _, b in _BETWEEN_END_RE.findall(sql)]
    if not ends:
        return ""
    latest = max(b.max_ts for b in bounds)[:10]
    latest_d = _parse(latest)
    if latest_d is None:
        return ""
    # 完全越界：窗口终点晚于数据最后一天 + 宽限
    hard = sorted({d for d in ends
                   if (_parse(d) or latest_d) > latest_d + _td(days=window_days)})
    if not hard:
        return ""
    return (
        f"\n\n【已核实的事实】业务库数据截止到 {latest}，而本次查询的时间窗口"
        f"延伸到 {'、'.join(hard[:3])}，**该时段数据尚未产生**。"
        f"因此结果中的 0 代表“无数据”，**不是“业务量为 0”**，"
        f"也**不得**据此得出“下滑/异常/归零”的结论。"
        f"请在摘要开头直接说明这一点。"
    )


def _parse(s: str):
    """'YYYY-MM-DD' → date；解析不了返回 None（调用方跳过，不猜）。"""
    try:
        y, m, d = (int(x) for x in s.split("-"))
        return _date(y, m, d)
    except Exception:
        return None
