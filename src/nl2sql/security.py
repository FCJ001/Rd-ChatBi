# ============================================================
# SQL 安全校验 — 四层防线
#
# 1. Prompt 层：SCHEMA_DESC 人工裁剪（敏感字段不在元数据中定义）
# 2. 正则层：FORBIDDEN_PATTERNS
# 3. 执行层：SELECT-only + LIMIT 强制覆盖 + timeout 10s
# 4. 数据库层：只读副本
#
# ★ LIMIT 在 AST 层强制覆盖：语句内所有 LIMIT/FETCH 字面量超过上限的
#   压到上限，非字面量（子查询/变量）直接覆盖为常量，顶层缺 LIMIT 补上
#   —— 旧的 `"LIMIT" not in sql.upper()` 判断挡不住 LIMIT 99999999、
#   也挡不住列名含 limit / LIMIT 出现在 CTE 内的情况
# ★ apply_role_filter 规则数据驱动：role_rules 来自 bi_datasources.role_rules，
#   每个数据源（项目）可配置自己的角色模型，代码零改动
# ★ 行级过滤的运行时参数不走字符串拼接：列名校验为合法标识符，
#   参数值经 sqlglot 字面量编码（单引号自动转义），杜绝注入
# ============================================================

import re

import sqlglot
import sqlglot.expressions as exp

FORBIDDEN_PATTERNS = [
    re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b", re.IGNORECASE),
    re.compile(r"\b(outpatient_visits|inpatient_records)\b[^;]*\b(patient_name|patient_phone|id_card|patient_no)\b", re.IGNORECASE),
    re.compile(r"\balm_issues\b[^;]*\b(reporter_phone|vin|customer_name)\b", re.IGNORECASE),
]

# 行数硬上限：LLM 生成/用户注入的任何 LIMIT 都不会超过它
MAX_ROW_LIMIT = 100

# 列名（含 table.column 形式）合法标识符 —— role_rules 配置也当不可信输入校验
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
# 字符串参数最大长度：超长参数直接拒绝，不做转义兜底
_MAX_PARAM_LEN = 256


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


def _clamp_row_limit(tree: exp.Expression) -> None:
    """把语句内所有 LIMIT / FETCH FIRST 的行数压到 MAX_ROW_LIMIT。

    - 字面量超限 → 覆盖为 MAX_ROW_LIMIT；
    - 非字面量（子查询、绑定参数、算术表达式）→ 无法静态确认上限，直接覆盖；
    - 嵌套子查询里的 LIMIT 一并处理，防止外层限行、内层全表扫描的的资源放大。"""
    for node in tree.find_all(exp.Limit):
        lit = node.expression
        value: int | None = None
        if isinstance(lit, exp.Literal) and lit.is_int:
            try:
                value = int(lit.this)
            except (TypeError, ValueError):
                value = None
        if value is None or value > MAX_ROW_LIMIT:
            node.set("expression", exp.Literal.number(MAX_ROW_LIMIT))
    for fetch in tree.find_all(exp.Fetch):
        count = fetch.args.get("count")
        if isinstance(count, exp.Literal) and count.is_int:
            try:
                value = int(count.this)
            except (TypeError, ValueError):
                value = None
            if value is None or value > MAX_ROW_LIMIT:
                fetch.set("count", exp.Literal.number(MAX_ROW_LIMIT))


def validate_sql(sql: str) -> tuple[bool, str]:
    """校验 SQL 安全性。返回 (is_valid, validated_sql_or_error)

    用 sqlglot 做语句级解析，彻底替代 startswith("SELECT")：
    - ★ 强制单条语句：asyncpg prepared statement 不支持多命令，且多语句是注入面。
       LLM 面对复合提问可能用分号拼接多条 SELECT，直接拒绝并给出明确提示。
    - 语句类型必须是 SELECT（含 WITH ... SELECT，sqlglot 中 WITH 挂在 Select 节点上）。
    - ★ LIMIT 强制覆盖：不管原语句写没写、写多大，返回的 SQL 行数上限必为
       MAX_ROW_LIMIT —— 返回值是重写后的 SQL，调用方必须执行返回值而非原始 SQL。
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

    tree = statements[0]
    if not isinstance(tree, exp.Select):
        return False, "只允许 SELECT 查询"

    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(stripped):
            return False, "查询包含禁止的操作或字段"

    _clamp_row_limit(tree)
    if tree.args.get("limit") is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(MAX_ROW_LIMIT)))

    return True, _fix_sqlglot_output(tree.sql(dialect="postgres"))


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

    ★ param 规则的取值来自认证上下文（可被伪造的请求头/表单），按不可信输入处理：
      列名必须是合法标识符，值经 sqlglot 字面量编码成 AST 再注入 ——
      不再 f-string 拼接，`dept_id = "1' OR '1'='1"` 只会等值比较到那个字符串。
    """
    params = params or {}
    rule = _resolve_role_rule(role, role_rules)

    if rule == "all":
        return True, sql
    if rule is None or rule == "deny":
        return False, f"当前角色 {role} 无数据查询权限"

    condition: str | exp.Expression | None = None
    if isinstance(rule, dict):
        if rule.get("column") and rule.get("param"):
            value = params.get(rule["param"])
            if value is None:
                return False, f"角色 {role} 需要提供参数 {rule['param']}，才能按数据隔离查询"
            condition = _build_param_condition(rule["column"], value)
            if condition is None:
                # 列名不合法 / 参数类型不支持 / 字符串超长 → fail-closed 拒绝
                return False, f"角色 {role} 的过滤参数 {rule['param']} 不合法，已拒绝查询"
        elif rule.get("value"):
            condition = rule["value"]

    if condition:
        sql = _inject_where_ast(sql, condition)
    return True, sql


def _build_param_condition(column: object, value: object) -> exp.Expression | None:
    """列名 + 运行时参数 → 安全等值条件 AST。不合法返回 None（调用方拒绝）。

    - 列名限定合法标识符（防 role_rules 配置被写入 `a; DROP ...` 之类内容）；
    - 数值 / 字符串经 exp.convert 编码，sqlglot 生成时自动转义单引号；
    - bool 归一为 1/0；其余类型（list/dict/None…）不支持，拒绝。"""
    if not isinstance(column, str) or not _IDENTIFIER_RE.match(column):
        return None
    if isinstance(value, bool):
        return exp.EQ(this=exp.column(column), expression=exp.convert(int(value)))
    if isinstance(value, (int, float)):
        return exp.EQ(this=exp.column(column), expression=exp.convert(value))
    if isinstance(value, str):
        if not value or len(value) > _MAX_PARAM_LEN:
            return None
        return exp.EQ(this=exp.column(column), expression=exp.convert(value))
    return None


def _resolve_role_rule(role: str, role_rules: dict | None) -> str | dict | None:
    """解析角色规则：规则缺失时整体拒绝（防越权兜底）"""
    if not role_rules or "roles" not in role_rules:
        return None
    roles = role_rules["roles"]
    return roles.get(role, role_rules.get("default", "deny"))


def _inject_where_ast(sql: str, condition: str | exp.Expression) -> str:
    """★ sqlglot AST 层注入 WHERE，不靠字符串替换。
    condition 可传表达式 AST（参数规则，已编码）或 SQL 片段字符串（value 规则，受信配置）。
    彻底解决医疗版 str.replace("WHERE", ...) 在子查询/CTE 中注入错误位置的问题。
    同时检测条件列名是否已存在，避免重复注入。"""
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
        if isinstance(condition, exp.Expression):
            condition_expr = condition
        else:
            condition_expr = sqlglot.parse_one(condition, dialect="postgres")

        # ★ 去重：如果 WHERE 里已经有同名列，跳过注入
        col_name = _extract_column_name(condition_expr)
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


def _extract_column_name(condition: str | exp.Expression) -> str | None:
    """从注入条件中提取列名，用于去重检测。例如 'department_id = 3' → 'department_id'"""
    try:
        if isinstance(condition, exp.Expression):
            col = condition.find(exp.Column)
        else:
            col = sqlglot.parse_one(condition, dialect="postgres").find(exp.Column)
        return col.name if col and hasattr(col, 'name') else None
    except Exception:
        return None
