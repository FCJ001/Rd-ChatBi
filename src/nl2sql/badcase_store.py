# ============================================================
# badcase 采集 —— 纯函数层（不碰 DB / 不碰网络，CI 可全量跑）
#
# 三个职责：
#   ① fingerprint   归一化问题 → 去重指纹（同一条问题反复失败只累加 seen_count）
#   ② classify_*    从用户可见错误文本反推错误类型（原始 DB 错误只进日志，
#                   落库的 error_message 是脱敏文案，所以分类只能按文案匹配）
#   ③ validate_case 导出前校验 —— 一条脏数据能让整个 CI 离线门禁变红
#
# ★ 为什么分类要靠中文文案匹配而不是错误码：engine.py 与 nodes/*.py 刻意
#   把原始 DB 错误吞掉（「错误文本可能暴露表结构」），透给用户的只有固定
#   文案。文案是这几处的唯一可观测信号，所以这里的常量必须与那些出处分
#   保持同步；tests/test_badcase_store.py 用字面量把它们钉住。
# ============================================================

from __future__ import annotations

import hashlib
import re

# 与 eval/run_nl2sql_eval.py 的 valid_levels 保持一致
VALID_DIFFICULTIES = ("easy", "medium", "hard")

# 与 eval/cases/*.json 现有分层保持一致。★ 写错分层等于没有度量：
#   离线门禁按 category × difficulty 出统计，新造类别会让统计裂成两条。
VALID_CATEGORIES = (
    "单表聚合", "分组统计", "排序TopN", "时间窗口", "多表JOIN", "明细查询",
)

# 结果集行数上限（与 src/nl2sql/security.MAX_ROW_LIMIT 同值）。
# ★ 行数被 AST 层钳到这个值，所以 row_count == 100 的含义是「被截断了」，
#   不是「正好 100 行」—— 挖历史与审核时都会用到这个判断。
MAX_ROW_LIMIT = 100

_ERROR_TYPE_LABELS = {
    "db_error": "数据库执行失败",
    "timeout": "查询超时",
    "role_denied": "角色无权查询",
    "llm_error": "模型侧故障",
    "empty_result": "结果为空",
    "truncated": "结果被截断",
    "unsatisfied": "用户标记答非所问",
}


def error_type_label(error_type: str) -> str:
    return _ERROR_TYPE_LABELS.get(error_type, error_type or "未知")


# ════════════════════════════════════════════════════════════════════════
# ① 归一化 + 指纹
# ════════════════════════════════════════════════════════════════════════

_WS_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[，。？！,?!；;：:、\s]+$")
# 全角 ASCII（！到～）→ 半角：用户输入法差异不该造成重复条目
_FULLWIDTH_MAP = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20  # 全角空格


def normalize_question(question: str) -> str:
    """入库前的统一形态：去首尾空白 → 全角转半角 → 折叠内部空白 → 去尾标点。

    ★ 刻意不做分词/同义改写：分词规则一改，历史指纹全部失效，等于去重失效。
      这里只消除「同一个问题在字面上确实不同」的噪声（空格、标点、全角）。
    """
    q = (question or "").strip().translate(_FULLWIDTH_MAP)
    q = _WS_RE.sub("", q)
    return _TRAILING_PUNCT_RE.sub("", q)


def fingerprint(datasource_id: int, question: str) -> str:
    """去重指纹 = sha256(datasource_id | 归一化问题)。

    ★ 必须带 datasource_id：同一句话在 hospital_demo 和 rd_agent 是两个
      完全不同的案例（表结构、角色、口径都不同），不带就会互相顶掉。
    """
    norm = normalize_question(question)
    return hashlib.sha256(f"{datasource_id}|{norm}".encode("utf-8")).hexdigest()


# ════════════════════════════════════════════════════════════════════════
# ② 错误分类
# ════════════════════════════════════════════════════════════════════════

# 角色拒绝文案，来自 src/nl2sql/security.py:apply_role_filter 的各拒绝出口
_ROLE_DENIED_MARKERS = (
    "无数据查询权限",
    "才能按数据隔离查询",
    "不合法，已拒绝查询",
    "过滤规则配置不完整",
    "行级过滤条件注入失败",
)
_TIMEOUT_MARKERS = ("超时",)
# 安全层拒绝文案，来自 src/nl2sql/security.py:validate_sql
_SECURITY_MARKERS = (
    "安全校验失败",
    "只允许 SELECT",
    "只允许单条 SELECT",
    "敏感字段",
    "禁止的操作或字段",
    "不允许调用的函数",
)


def classify_error(error: str, sql: str = "", row_count: int = 0) -> str:
    """用户可见错误文案 → error_type。传入 sql/row_count 以区分「查成功但没用」。

    顺序有讲究：角色拒绝要先于通用 DB 错误判断 —— 它的文案里也可能带
    「拒绝查询」，先判 db_error 会把权限问题误归成 SQL 问题，
    而权限拒绝恰恰是最该被审核的一类（往往是案例/规则设计问题，不是模型问题）。
    """
    text = (error or "").strip()
    if text:
        if any(m in text for m in _ROLE_DENIED_MARKERS):
            return "role_denied"
        if any(m in text for m in _TIMEOUT_MARKERS):
            return "timeout"
        if any(m in text for m in _SECURITY_MARKERS):
            # 安全层拒绝的 SQL 没进过库，本质是「模型没生成合规 SQL」，
            # 归 llm_error 而不是 db_error：审核时该去调 prompt，不是调 SQL
            return "llm_error"
        return "db_error"
    # 无错误文本：SQL 成功执行但结果可能没用
    if sql and row_count == 0:
        return "empty_result"
    if sql and row_count >= MAX_ROW_LIMIT:
        return "truncated"
    return ""


def classify_capture(error: str, sql: str, row_count: int) -> str:
    """采集侧判据：返回非空 error_type 才落库，空串表示「这条不采」。"""
    et = classify_error(error, sql, row_count)
    # 没写 SQL 又没错误：LLM 没产出任何可用东西，属于模型侧故障
    if not et and not sql and not (error or "").strip():
        return "llm_error"
    return et


# ════════════════════════════════════════════════════════════════════════
# ③ 导出前校验
# ════════════════════════════════════════════════════════════════════════

class CaseValidationError(ValueError):
    """案例不合法 —— 导出/审核时必须拦下，否则会污染离线门禁"""


def _has_unqualified_star(sql: str) -> bool:
    """SQL 里有没有未限定的 `*`（`SELECT *` 拦，`SELECT t.*` / `COUNT(*)` 放）。

    ★ 这是个真实的洞：sensitive_columns 的文本层拦截只挡「写了列名」的查询，
      `SELECT *` 不含列名，靠执行层 filter_result_columns 兜底。所以一条
      predicted_sql 看着干净、过得了 validate_sql，却会捞出敏感列。
      它一旦被人工填成 golden_sql，就把这个洞固化进了评测集。

    ★ `COUNT(*)` 里的 * 在 AST 上同样是 exp.Star（父节点是 exp.Count），
      但那是聚合计数、不展开任何列 —— 只按 Star 判会把所有聚合查询误杀，
      所以必须显式放行函数参数位。
    """
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:
        return False
    for star in tree.find_all(exp.Star):
        parent = star.parent
        if isinstance(parent, (exp.Column, exp.Count)):  # t.* / COUNT(*) / COUNT(t.*)
            continue
        return True
    return False


def validate_case(
    *,
    question: str,
    golden_sql: str,
    category: str,
    difficulty: str,
    sensitive_columns: list[str] | None = None,
) -> None:
    """校验一条待导出案例。不合法直接抛 CaseValidationError。

    校验项与 eval/run_nl2sql_eval.py 的离线门禁对齐 —— 一条脏数据能让
    CI 变红，所以在导出/审核时就要拦，而不是等 CI 报错。
    """
    if not (question or "").strip():
        raise CaseValidationError("question 为空")
    if not (golden_sql or "").strip():
        raise CaseValidationError("golden_sql 为空 —— 没有标准答案的案例不能进评测集")
    if category not in VALID_CATEGORIES:
        raise CaseValidationError(
            f"category 必须是 {list(VALID_CATEGORIES)} 之一，实际 {category!r}"
        )
    if difficulty not in VALID_DIFFICULTIES:
        raise CaseValidationError(
            f"difficulty 必须是 {list(VALID_DIFFICULTIES)} 之一，实际 {difficulty!r}"
        )

    from src.nl2sql.security import validate_sql

    ok, result = validate_sql(golden_sql, sensitive_columns=sensitive_columns)
    if not ok:
        raise CaseValidationError(f"golden_sql 未通过安全层：{result}")

    if sensitive_columns and _has_unqualified_star(golden_sql):
        raise CaseValidationError(
            "golden_sql 含未限定的 * —— 该数据源有敏感列，SELECT * 会捞出它们，"
            "请写全列名"
        )


def result_rows_truncated(row_count: int) -> bool:
    """行数是否被 LIMIT 钳制截断（见 MAX_ROW_LIMIT 的说明）"""
    return row_count >= MAX_ROW_LIMIT
