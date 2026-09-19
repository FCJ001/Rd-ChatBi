# ============================================================
# NL2SQL 安全防线单元测试
# 覆盖：validate_sql 四层防线 / apply_role_filter 角色过滤 /
#       _inject_where_ast AST 注入 / _remove_trailing_line_comment
# ============================================================

from src.nl2sql.security import (
    _inject_where_ast,
    _remove_trailing_line_comment,
    apply_role_filter,
    validate_sql,
)

# 数据源权限规则（与 conf/projects/*.yaml 的 role_rules 同构）
HOSPITAL_RULES = {
    "default": "deny",
    "roles": {
        "admin": "all",
        "doctor": {"column": "department_id", "param": "dept_id"},
        "cashier": {"value": "status = 'paid'"},
        "patient": "deny",
    },
}
ALM_RULES = {
    "default": "deny",
    "roles": {
        "admin": "all",
        "engineer": {"column": "owner_domain_id", "param": "owner_domain_id"},
        "business": {"column": "business_line", "param": "business_line"},
        "aftersales": {"value": "status IN ('closed', 'verified')"},
        "customer": "deny",
    },
}


# ── validate_sql ──────────────────────────────────────────────

def test_select_pass():
    ok, sql = validate_sql("SELECT * FROM outpatient_visits WHERE visit_type='急诊'")
    assert ok
    assert "LIMIT 100" in sql


def test_non_select_rejected():
    ok, msg = validate_sql("UPDATE outpatient_visits SET status='paid'")
    assert not ok
    assert "SELECT" in msg


def test_forbidden_keyword_rejected():
    for bad in [
        "DROP TABLE outpatient_visits",
        "DELETE FROM inpatient_records WHERE id=1",
        "TRUNCATE TABLE departments",
    ]:
        ok, _ = validate_sql(bad)
        assert not ok, f"{bad} 应被拦截"


def test_sensitive_field_rejected():
    # FORBIDDEN_PATTERNS 匹配 "引用了门诊表又出现敏感列" 的语句
    ok, _ = validate_sql("SELECT * FROM outpatient_visits WHERE patient_phone = '138'")
    assert not ok


def test_existing_limit_not_duplicated():
    ok, sql = validate_sql("SELECT id FROM outpatient_visits LIMIT 5")
    assert ok
    assert sql.count("LIMIT") == 1


def test_multi_statement_rejected():
    # LLM 面对复合提问可能用分号拼接多条 SELECT，asyncpg 不支持多命令 → 直接拒绝
    ok, msg = validate_sql(
        "SELECT * FROM outpatient_visits WHERE visit_type='急诊'; "
        "SELECT COUNT(*) FROM outpatient_visits"
    )
    assert not ok
    assert "单条" in msg


def test_with_cte_allowed():
    # WITH ... SELECT 是合法的单条只读查询，sqlglot 中 WITH 挂在 Select 节点上
    ok, sql = validate_sql(
        "WITH recent AS (SELECT COUNT(*) AS c FROM outpatient_visits WHERE visit_type='急诊') "
        "SELECT * FROM recent"
    )
    assert ok
    assert "LIMIT 100" in sql


def test_trailing_comment_stripped():
    ok, sql = validate_sql("SELECT * FROM outpatient_visits -- 备注")
    assert ok
    assert "--" not in sql
    assert "LIMIT 100" in sql


# ── _remove_trailing_line_comment ─────────────────────────────

def test_comment_inside_string_not_stripped():
    sql = "SELECT 'a--b' AS x FROM t"
    assert _remove_trailing_line_comment(sql) == sql


def test_inline_comment_with_newline_kept():
    sql = "SELECT * FROM t -- 注释\nWHERE id=1"
    # 注释后有换行，不是尾部注释 → 不截断
    assert "WHERE" in _remove_trailing_line_comment(sql)


# ── apply_role_filter ─────────────────────────────────────────

def test_patient_denied():
    ok, msg = apply_role_filter("SELECT 1", role="patient", role_rules=HOSPITAL_RULES)
    assert not ok


def test_unknown_role_denied():
    # 未在 role_rules 里声明的角色 → default deny
    ok, _ = apply_role_filter("SELECT 1", role="hacker", role_rules=HOSPITAL_RULES)
    assert not ok


def test_no_rules_denied():
    # 数据源未配置 role_rules → 整体拒绝（防越权兜底）
    ok, _ = apply_role_filter("SELECT 1", role="admin", role_rules=None)
    assert not ok


def test_doctor_without_dept_denied():
    # doctor 角色必须提供科室 ID，否则拒绝（防止越权看全院）
    ok, msg = apply_role_filter("SELECT 1", role="doctor", role_rules=HOSPITAL_RULES)
    assert not ok


def test_admin_passthrough():
    sql = "SELECT COUNT(*) FROM outpatient_visits"
    ok, out = apply_role_filter(sql, role="admin", role_rules=HOSPITAL_RULES)
    assert ok and out == sql


def test_doctor_injects_dept():
    ok, out = apply_role_filter(
        "SELECT id FROM outpatient_visits LIMIT 5", role="doctor",
        role_rules=HOSPITAL_RULES, params={"dept_id": 7},
    )
    assert ok
    assert "department_id = 7" in out
    assert "LIMIT" in out  # 原有 LIMIT 保留


def test_cashier_injects_paid_status():
    ok, out = apply_role_filter(
        "SELECT id FROM outpatient_visits", role="cashier", role_rules=HOSPITAL_RULES
    )
    assert ok
    assert "status = 'paid'" in out


def test_alm_engineer_injects_owner_domain():
    ok, out = apply_role_filter(
        "SELECT id FROM alm_issues LIMIT 5", role="engineer",
        role_rules=ALM_RULES, params={"owner_domain_id": 3},
    )
    assert ok
    assert "owner_domain_id = 3" in out


def test_alm_business_injects_business_line():
    ok, out = apply_role_filter(
        "SELECT id FROM alm_issues", role="business",
        role_rules=ALM_RULES, params={"business_line": "ev"},
    )
    assert ok
    assert "business_line = 'ev'" in out


def test_existing_filter_column_still_enforced():
    """★ SQL 已含过滤列时必须无条件叠加授权值，不允许去重跳过。

    去重跳过是越权通道：用户诱导 LLM 写出 `department_id = 3`（别人的科室），
    若跳过注入，doctor(dept=7) 就能查任意科室。无条件 AND 后，同列不同值
    结果为空集（deny 语义），是安全方向的失败。"""
    sql = "SELECT id FROM outpatient_visits WHERE department_id = 3"
    ok, out = apply_role_filter(
        sql, role="doctor", role_rules=HOSPITAL_RULES, params={"dept_id": 7},
    )
    assert ok
    # 授权值条件必须出现在最终 SQL 里
    assert "department_id = 7" in out


def test_same_column_same_value_injects_once_more_harmlessly():
    """已有条件恰好等于授权值时，重复 AND 只是冗余，行为不变。"""
    sql = "SELECT id FROM outpatient_visits WHERE department_id = 7"
    ok, out = apply_role_filter(
        sql, role="doctor", role_rules=HOSPITAL_RULES, params={"dept_id": 7},
    )
    assert ok
    assert "department_id = 7" in out


def test_subquery_where_injected_at_top_level():
    sql = "SELECT * FROM (SELECT * FROM outpatient_visits WHERE status='paid') AS sub LIMIT 5"
    ok, out = apply_role_filter(
        sql, role="doctor", role_rules=HOSPITAL_RULES, params={"dept_id": 1},
    )
    assert ok
    # 注入发生在顶层 WHERE，而非子查询
    assert "department_id" in out


# ── LIMIT 强制覆盖（AST 层）───────────────────────────────────

def test_excessive_limit_overridden():
    ok, sql = validate_sql("SELECT id FROM outpatient_visits LIMIT 99999999")
    assert ok
    assert "99999999" not in sql
    assert "LIMIT 100" in sql


def test_limit_in_subquery_clamped():
    ok, sql = validate_sql(
        "SELECT * FROM (SELECT * FROM outpatient_visits LIMIT 50000) AS sub LIMIT 20"
    )
    assert ok
    assert "50000" not in sql
    assert "LIMIT 20" in sql


def test_limit_all_overridden():
    # LIMIT ALL 合法但等于不限行数 → 必须覆盖为常量上限
    ok, sql = validate_sql("SELECT id FROM outpatient_visits LIMIT ALL")
    assert ok
    assert "LIMIT 100" in sql


def test_column_named_limit_still_gets_row_cap():
    # 旧实现用 "LIMIT" not in sql.upper() 判断，列名含 limit 就漏加行数上限
    ok, sql = validate_sql("SELECT limit_cnt FROM outpatient_visits")
    assert ok
    assert "LIMIT 100" in sql


def test_fetch_first_clamped():
    ok, sql = validate_sql(
        "SELECT id FROM outpatient_visits OFFSET 0 ROWS FETCH FIRST 99999 ROWS ONLY"
    )
    assert ok
    assert "99999" not in sql


# ── 行级过滤参数安全（列名校验 + 字面量编码）─────────────────

INJECTION_RULES = {
    "default": "deny",
    "roles": {
        "doctor": {"column": "department_id", "param": "dept_id"},
    },
}


def test_param_string_injection_contained():
    # 值不是 int → 按字符串字面量编码，OR 1=1 不会逃逸出引号
    ok, out = apply_role_filter(
        "SELECT id FROM outpatient_visits", role="doctor",
        role_rules=INJECTION_RULES, params={"dept_id": "7 OR 1=1"},
    )
    assert ok
    assert "department_id = '7 OR 1=1'" in out


def test_param_quote_escape():
    ok, out = apply_role_filter(
        "SELECT id FROM outpatient_visits", role="doctor",
        role_rules=INJECTION_RULES, params={"dept_id": "x' OR '1'='1"},
    )
    assert ok
    # 单引号被双写转义，无法跳出字符串字面量
    assert "department_id = 'x'' OR ''1''=''1'" in out


def test_param_union_injection_contained():
    ok, out = apply_role_filter(
        "SELECT id FROM outpatient_visits", role="doctor",
        role_rules=INJECTION_RULES, params={"dept_id": "1 UNION SELECT 2"},
    )
    assert ok
    assert "'1 UNION SELECT 2'" in out
    assert out.upper().count("UNION") == 1  # 只在字符串字面量里


def test_illegal_column_rejected():
    bad_rules = {
        "default": "deny",
        "roles": {"doctor": {"column": "a; DROP TABLE x", "param": "dept_id"}},
    }
    ok, _ = apply_role_filter(
        "SELECT id FROM t", role="doctor", role_rules=bad_rules, params={"dept_id": 1},
    )
    assert not ok


def test_unsupported_param_type_rejected():
    ok, _ = apply_role_filter(
        "SELECT id FROM t", role="doctor",
        role_rules=INJECTION_RULES, params={"dept_id": {"$gt": 1}},
    )
    assert not ok


def test_param_bool_normalized():
    bool_rules = {
        "default": "deny",
        "roles": {"doctor": {"column": "is_vip", "param": "dept_id"}},
    }
    ok, out = apply_role_filter(
        "SELECT id FROM t", role="doctor", role_rules=bool_rules, params={"dept_id": True},
    )
    assert ok
    assert "is_vip = 1" in out


def test_param_oversized_string_rejected():
    ok, _ = apply_role_filter(
        "SELECT id FROM t", role="doctor",
        role_rules=INJECTION_RULES, params={"dept_id": "x" * 300},
    )
    assert not ok


# ══ 敏感列拦截的作用域（数据驱动 vs 旧版表级兜底）════════════════
# 修复背景：hospital/ALM 的表级敏感正则曾无条件作用于所有数据源，
# 对配置了 sensitive_columns 的数据源是无效计算且违背数据驱动设计。

def test_sensitive_columns_configured_uses_ast_not_legacy_regex():
    """配置了 sensitive_columns → AST 精确匹配生效，旧版粗正则不参与：
    命中旧正则文本但不在配置名单里的列名，应当放行（口径由配置说了算）"""
    # patient_name 命中旧 hospital 正则，但本数据源只配了 phone → 不拦
    ok, _ = validate_sql(
        "SELECT patient_name FROM outpatient_visits WHERE patient_phone = 'x' LIMIT 5",
        sensitive_columns=["phone"],
    )
    assert ok
    # 配置名单里的列 → AST 拦截（哪怕不在任何旧正则里）
    ok, msg = validate_sql(
        "SELECT secret_col FROM t LIMIT 5", sensitive_columns=["secret_col"],
    )
    assert not ok and "敏感" in msg


def test_sensitive_columns_absent_falls_back_to_legacy_regex():
    """未配置 sensitive_columns 的数据源 → 旧版表级兜底仍生效（向后兼容）"""
    ok, _ = validate_sql(
        "SELECT patient_name FROM outpatient_visits WHERE patient_phone = '138' LIMIT 5",
    )
    assert not ok
    # ★ 旧正则是顺序敏感的（表名在前、敏感列在后才命中）——这是它不如
    # AST 列引用匹配的地方，也是"配置了 sensitive_columns 就不该再用它"的又一论据
    ok, _ = validate_sql(
        "SELECT id FROM alm_issues WHERE reporter_phone = 'x' LIMIT 5",
    )
    assert not ok
    # 不命中兜底正则的正常查询不受影响
    ok, _ = validate_sql("SELECT COUNT(*) FROM alm_issues")
    assert ok
