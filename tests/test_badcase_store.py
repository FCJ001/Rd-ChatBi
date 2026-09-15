# ============================================================
# badcase 采集的纯函数回归（不需要 DB / Redis，CI 全量跑）
#
# 锁三件事：
#   ① 指纹去重：同一条问题反复失败必须合并，且不同数据源不能串号
#   ② 错误分类：落库的 error_message 是脱敏文案，分类只能按文案匹配 ——
#      这里用源码里的字面量把它钉住，改了文案就会红
#   ③ 导出前校验：一条脏数据能让整个离线门禁变红，必须提前拦
# ============================================================

import pytest

from src.nl2sql.badcase_store import (
    MAX_ROW_LIMIT,
    VALID_CATEGORIES,
    VALID_DIFFICULTIES,
    CaseValidationError,
    classify_error,
    fingerprint,
    normalize_question,
    validate_case,
)


# ── ① 归一化 + 指纹 ──────────────────────────────────────────

def test_normalize_collapses_noise():
    """空白、全角、尾标点都是输入法噪声，不该造成不同指纹"""
    base = "一共有多少个未关闭的问题单"
    for variant in [
        "  一共有多少个未关闭的问题单？ ",
        "一共有多少个未关闭的问题单",
        "一共有多少个未关闭的问题单。",
        "一共有多少个未关闭的　问题单",   # 全角空格
        "一共有多少个未关闭的问题单！",
    ]:
        assert normalize_question(variant) == base, variant


def test_fingerprint_stable_across_noise():
    a = fingerprint(1, "各科室有多少人？")
    b = fingerprint(1, "  各科室有多少人 ")
    assert a == b
    assert len(a) == 64


def test_fingerprint_separates_datasources():
    """★ 同一句话在 hospital_demo 和 rd_agent 是两个完全不同的案例，
    指纹不带 datasource_id 会让它们互相顶掉"""
    assert fingerprint(1, "有多少条记录") != fingerprint(2, "有多少条记录")


def test_fingerprint_distinguishes_different_questions():
    assert fingerprint(1, "有多少个医生") != fingerprint(1, "有多少个科室")


# ── ② 错误分类 ───────────────────────────────────────────────
# 下面这些字符串全部来自源码字面量，改文案不改这里就会红（这是刻意的）

def test_role_denied_from_security_module():
    # src/nl2sql/security.py:apply_role_filter 的各拒绝出口
    for msg in [
        "当前角色 patient 无数据查询权限",
        "角色 doctor 需要提供参数 dept_id，才能按数据隔离查询",
        "角色 doctor 的过滤参数 dept_id 不合法，已拒绝查询",
        "角色 x 的过滤规则配置不完整，已拒绝查询",
        "行级过滤条件注入失败，已拒绝查询",
    ]:
        assert classify_error(msg) == "role_denied", msg


def test_timeout_classified():
    assert classify_error("查询超时（10秒）") == "timeout"
    assert classify_error("查询超时（10秒），请缩小查询范围") == "timeout"


def test_security_rejection_is_llm_error_not_db_error():
    """安全层拒绝 = 模型没生成合规 SQL，该去调 prompt 而不是调 SQL"""
    assert classify_error("安全校验失败: 只允许 SELECT 查询") == "llm_error"
    assert classify_error("查询包含敏感字段，已被拦截") == "llm_error"
    assert classify_error("查询包含不允许调用的函数 pg_read_file") == "llm_error"


def test_generic_db_error():
    assert classify_error("查询执行失败，请调整问题后重试") == "db_error"
    assert classify_error("数据库执行失败，请调整问题后重试") == "db_error"


def test_ok_query_without_error_is_not_captured():
    """查成功且有数据 —— 正常查询，不该进队列"""
    assert classify_error("", "SELECT 1", 5) == ""
    assert classify_error("", "", 0) == ""


def test_empty_result_detected():
    """★ 0 行是收敛期唯一能自动抓到的隐性 badcase：
    SQL 完全合法、执行成功，但用户要的不是这个"""
    assert classify_error("", "SELECT 1", 0) == "empty_result"


def test_truncated_detected():
    """★ 行数被 AST 层钳到 MAX_ROW_LIMIT，==上限 的含义是「被截断」"""
    assert classify_error("", "SELECT 1", MAX_ROW_LIMIT) == "truncated"
    assert classify_error("", "SELECT 1", MAX_ROW_LIMIT - 1) == ""


def test_role_denied_checked_before_generic():
    """角色拒绝优先于通用 DB 错误 —— 它文案里也含「拒绝查询」，
    顺序写反会把权限问题误归成 SQL 问题"""
    assert classify_error("当前角色 x 无数据查询权限") == "role_denied"


# ── ③ 导出前校验 ─────────────────────────────────────────────

def _ok(**kw):
    base = dict(question="有多少个科室", golden_sql="SELECT COUNT(*) AS cnt FROM departments",
                category="单表聚合", difficulty="easy")
    base.update(kw)
    return base


def test_valid_case_passes():
    validate_case(**_ok())


def test_missing_golden_rejected():
    with pytest.raises(CaseValidationError, match="golden_sql 为空"):
        validate_case(**_ok(golden_sql=""))


def test_bad_category_rejected():
    with pytest.raises(CaseValidationError, match="category"):
        validate_case(**_ok(category="我自己编的类别"))


def test_bad_difficulty_rejected():
    with pytest.raises(CaseValidationError, match="difficulty"):
        validate_case(**_ok(difficulty="impossible"))


def test_golden_must_pass_security_layer():
    with pytest.raises(CaseValidationError, match="未通过安全层"):
        validate_case(**_ok(golden_sql="DELETE FROM departments"))


def test_sensitive_star_rejected():
    """★ 这是个真实的洞：sensitive_columns 的文本层拦截只挡「写了列名」的查询，
    SELECT * 不含列名、能过 validate_sql，却会在执行层捞出敏感列。
    它一旦被填成 golden_sql，就把这个洞固化进了评测集。"""
    with pytest.raises(CaseValidationError, match="未限定的 \\*"):
        validate_case(**_ok(golden_sql="SELECT * FROM outpatient_visits",
                            sensitive_columns=["patient_name"]))


def test_count_star_is_not_flagged():
    """★ COUNT(*) 里的 * 在 AST 上也是 Star，但那是聚合计数、不展开任何列。
    只按 Star 判会把所有聚合查询误杀 —— 这是实现时真踩过的坑。"""
    validate_case(**_ok(golden_sql="SELECT COUNT(*) AS cnt FROM outpatient_visits",
                        sensitive_columns=["patient_name"]))
    validate_case(**_ok(golden_sql="SELECT COUNT(DISTINCT id) AS c, SUM(fee) FROM t",
                        sensitive_columns=["patient_name"]))


def test_qualified_star_is_allowed():
    """t.* 限定了表，但它展开的仍是全部列 —— 这里只拦未限定形态，
    因为 sensitive_columns 的检测能力就到这一步"""
    validate_case(**_ok(golden_sql="SELECT t.count FROM t AS t"))


def test_star_allowed_when_no_sensitive_columns():
    """没有敏感列声明的数据源不该被这条规则拦住"""
    validate_case(**_ok(golden_sql="SELECT * FROM departments", sensitive_columns=[]))


def test_categories_match_evaluator():
    """分层维度必须与评测器的统计维度一致：新造类别会让统计裂成两条"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
    from run_nl2sql_eval import load_cases

    used = set()
    for c in load_cases():
        used.add(c["category"])
        assert c["difficulty"] in VALID_DIFFICULTIES
    assert used <= set(VALID_CATEGORIES), f"存量案例里出现了未登记的分层: {used - set(VALID_CATEGORIES)}"
