# ============================================================
# SQL 安全校验 — 四层防线
#
# 1. Prompt 层：SCHEMA_DESC 人工裁剪（敏感字段不在元数据中定义）
# 2. 正则层：FORBIDDEN_PATTERNS
# 3. 执行层：SELECT-only + LIMIT 100 + timeout 10s
# 4. 数据库层：只读副本
#
# ★ _inject_where 用 sqlglot AST 注入（不用 str.replace）
# ★ apply_role_filter 规则数据驱动：role_rules 来自 bi_datasources.role_rules，
#   每个数据源（项目）可配置自己的角色模型，代码零改动
# ============================================================

import re

import sqlglot
import sqlglot.expressions as exp

FORBIDDEN_PATTERNS = [
    re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b", re.IGNORECASE),
    re.compile(r"\b(outpatient_visits|inpatient_records)\b[^;]*\b(patient_name|patient_phone|id_card|patient_no)\b", re.IGNORECASE),
    re.compile(r"\balm_issues\b[^;]*\b(reporter_phone|vin|customer_name)\b", re.IGNORECASE),
]


def _remove_trailing_line_comment(sql: str) -> str:
    """移除语句末尾的 `--` 行注释（跳过字符串字面量内的 `--`）。

    若不移除，后面追加的 `LIMIT 100` 会被注释吞掉而被数据库忽略
    （如 `SELECT * FROM t -- 备注` + ` LIMIT 100` → LIMIT 成了注释内容），
    从而绕过行数上限，可查全表。"""
    in_str: str | None = None  # "'" 或 '"'，None 表示不在字符串内
    last_comment = -1
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            if ch == in_str:
                # 处理 SQL 转义引号（'' / ""）
                if i + 1 < n and sql[i + 1] == in_str:
                    i += 2
                    continue
                in_str = None
        else:
            if ch in ("'", '"'):
                in_str = ch
            elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
                last_comment = i
                i += 1
        i += 1
    # 只有注释延伸到字符串末尾（其后无换行）才是尾部注释，才截断
    if last_comment >= 0 and "\n" not in sql[last_comment:]:
        return sql[:last_comment].rstrip()
    return sql


def validate_sql(sql: str) -> tuple[bool, str]:
    """校验 SQL 安全性。返回 (is_valid, validated_sql_or_error)

    用 sqlglot 做语句级解析，彻底替代 startswith("SELECT")：
    - ★ 强制单条语句：asyncpg prepared statement 不支持多命令，且多语句是注入面。
       LLM 面对复合提问可能用分号拼接多条 SELECT，直接拒绝并给出明确提示。
    - 语句类型必须是 SELECT（含 WITH ... SELECT，sqlglot 中 WITH 挂在 Select 节点上）。
    """
    stripped = sql.strip().rstrip(";")
    stripped = _remove_trailing_line_comment(stripped)
    if not stripped:
        return False, "SQL 为空"

    try:
        statements = sqlglot.parse(stripped, dialect="postgres")
    except Exception:
        return False, "SQL 解析失败"
    if not statements:
        return False, "SQL 解析失败"

    if len(statements) > 1:
        return False, "只允许单条 SELECT 语句，请把多个查询拆开"

    if not isinstance(statements[0], exp.Select):
        return False, "只允许 SELECT 查询"

    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(stripped):
            return False, "查询包含禁止的操作或字段"

    if "LIMIT" not in stripped.upper():
        stripped += " LIMIT 100"

    return True, stripped


def apply_role_filter(
    sql: str,
    role: str,
    role_rules: dict | None = None,
    params: dict | None = None,
) -> tuple[bool, str]:
    """
    数据驱动的角色行级过滤。返回 (allowed, modified_sql)。

    role_rules 结构（存在 bi_datasources.role_rules，按项目配置）：
        {
          "default": "deny",                       # 未声明角色 → deny
          "roles": {
            "admin":    "all",                      # 放行，不注入条件
            "doctor":   {"column": "department_id", "param": "dept_id"},
                        #  → 注入 department_id = {params[dept_id]}；参数缺失 → 拒绝
            "cashier":  {"value": "status = 'paid'"},  # 注入字面条件
            "patient":  "deny"                      # 直接拒绝
          }
        }
    无 role_rules（未配置）→ 拒绝，防越权兜底。
    """
    params = params or {}
    rule = _resolve_role_rule(role, role_rules)

    if rule == "all":
        return True, sql
    if rule is None or rule == "deny":
        return False, f"当前角色 {role} 无数据查询权限"

    condition = None
    if isinstance(rule, dict):
        if rule.get("column") and rule.get("param"):
            value = params.get(rule["param"])
            if value is None:
                return False, f"角色 {role} 需要提供参数 {rule['param']}，才能按数据隔离查询"
            condition = f"{rule['column']} = {value}" if isinstance(value, int) \
                else f"{rule['column']} = '{value}'"
        elif rule.get("value"):
            condition = rule["value"]

    if condition:
        sql = _inject_where_ast(sql, condition)
    return True, sql


def _resolve_role_rule(role: str, role_rules: dict | None) -> str | dict | None:
    """解析角色规则：规则缺失时整体拒绝（防越权兜底）"""
    if not role_rules or "roles" not in role_rules:
        return None
    roles = role_rules["roles"]
    return roles.get(role, role_rules.get("default", "deny"))


def _inject_where_ast(sql: str, condition: str) -> str:
    """★ sqlglot AST 层注入 WHERE，不靠字符串替换。
    彻底解决医疗版 str.replace("WHERE", ...) 在子查询/CTE 中注入错误位置的问题。
    同时检测条件列名是否已存在，避免重复注入。"""
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
        condition_expr = sqlglot.parse_one(condition, dialect="postgres")

        # ★ 去重：如果 WHERE 里已经有同名列，跳过注入
        col_name = _extract_column_name(condition)
        where = tree.find(exp.Where)
        if where and col_name:
            existing_cols = {c.name for c in where.find_all(exp.Column) if hasattr(c, 'name')}
            if col_name in existing_cols:
                return _fix_sqlglot_output(tree.sql(dialect="postgres"))

        if where:
            where.set("this", exp.And(this=where.this, expression=condition_expr))
        else:
            tree.set("where", exp.Where(this=condition_expr))

        return _fix_sqlglot_output(tree.sql(dialect="postgres"))
    except Exception:
        # sqlglot 解析失败则降级为简单注入
        upper = sql.upper()
        if "WHERE" in upper:
            idx = upper.index("WHERE") + 5
            return sql[:idx] + f" {condition} AND" + sql[idx:]
        elif "LIMIT" in upper:
            idx = upper.index("LIMIT")
            return sql[:idx] + f" WHERE {condition} " + sql[idx:]
        elif "ORDER" in upper:
            idx = upper.index("ORDER")
            return sql[:idx] + f" WHERE {condition} " + sql[idx:]
        else:
            return f"SELECT * FROM ({sql}) AS _filtered WHERE {condition}"


def _fix_sqlglot_output(sql: str) -> str:
    """修复 sqlglot 输出中 PostgreSQL 不兼容的语法。
    例如 sqlglot 会把 INTERVAL '3 months' 改写为 INTERVAL '3' MONTHS（复数不合法）。"""
    return re.sub(
        r"INTERVAL\s+'(\d+)'\s+(DAYS|HOURS|MONTHS|YEARS|WEEKS|MINUTES|SECONDS)",
        r"INTERVAL '\1 \2'",
        sql,
        flags=re.IGNORECASE,
    )


def _extract_column_name(condition: str) -> str | None:
    """从注入条件中提取列名，用于去重检测。例如 'department_id = 3' → 'department_id'"""
    try:
        col = sqlglot.parse_one(condition, dialect="postgres").find(exp.Column)
        return col.name if col and hasattr(col, 'name') else None
    except Exception:
        return None
