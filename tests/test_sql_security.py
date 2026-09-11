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


def test_no_duplicate_injection_same_column():
    sql = "SELECT id FROM outpatient_visits WHERE department_id = 3"
    ok, out = apply_role_filter(
        sql, role="doctor", role_rules=HOSPITAL_RULES, params={"dept_id": 7},
    )
    assert ok
    # 已存在同名列，不重复注入
    assert out.count("department_id") == 1


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
