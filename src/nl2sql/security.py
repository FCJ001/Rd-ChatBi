# ============================================================
# SQL 安全校验 — 四层防线
#
# 1. Prompt 层：SCHEMA_DESC 人工裁剪（敏感字段不在元数据中定义）
# 2. 校验层：sqlglot 语句级解析 —— SELECT-only（含 SELECT INTO / 危险函数拒绝）
#            + FORBIDDEN_PATTERNS 兜底（跑在去注释文本上）
#            + 敏感列 AST 检查（数据源 sensitive_columns，列引用级精确匹配）
# 3. 执行层：SELECT-only + LIMIT 强制覆盖 + timeout 10s
#            + filter_result_columns 结果列过滤（SELECT * 不写列名，
#              能穿文本层，靠这一层把敏感列从结果集里剔除）
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
from collections.abc import Collection
from dataclasses import dataclass

import sqlglot
import sqlglot.expressions as exp

# 旧版兜底规则：数据源未配置 sensitive_columns 时仍生效（向后兼容）。
# 新数据源应把敏感列配到 conf/projects/{code}.yaml 的 datasource.sensitive_columns，
# 走 validate_sql 的动态拦截（见 SENSITIVE 检查），不再改这里。
# 通用文本拦截（所有数据源恒生效）：写操作关键字
FORBIDDEN_PATTERNS = [
    re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b", re.IGNORECASE),
]

# 旧版兜底（向后兼容）：数据源【未配置】sensitive_columns 时才生效的表级敏感正则。
# ★ 配置了 sensitive_columns 的数据源走 AST+文本双层精确匹配，不再评估这两条
#   粗正则 —— 把 hospital/ALM 的表名列名硬编码在全局安全层，对其它数据源
#   是无效计算，也违背"敏感列数据驱动"的设计。
#   删除前提：所有存量数据源都配了 sensitive_columns 并重跑过 build 注册落库。
_LEGACY_SENSITIVE_PATTERNS = [
    re.compile(r"\b(outpatient_visits|inpatient_records)\b[^;]*\b(patient_name|patient_phone|id_card|patient_no)\b", re.IGNORECASE),
    re.compile(r"\balm_issues\b[^;]*\b(reporter_phone|vin|customer_name)\b", re.IGNORECASE),
]

# 行数硬上限：LLM 生成/用户注入的任何 LIMIT 都不会超过它
MAX_ROW_LIMIT = 100

# 危险函数黑名单：都是"合法 SELECT"但带副作用/越权读的函数，
# 语句级 SELECT-only 拦不住它们。前缀族单独列出（dblink_* 一大串）。
FORBIDDEN_FUNCTIONS = frozenset({
    # 文件系统 / 服务端文件读
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    # 会话/服务控制（set_config 能改 default_transaction_read_only，
    # 尝试关掉第四层只读防线；pg_sleep 是资源放大）
    "set_config", "setseed", "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_terminate_backend", "pg_cancel_backend",
    "pg_reload_conf", "pg_rotate_logfile", "pg_logdir_ls",
    # 备份 / WAL / 复制（需高权限，越权面）
    "pg_backup_start", "pg_backup_stop", "pg_switch_wal", "pg_walfile_name",
    "pg_create_restore_point", "pg_start_backup", "pg_stop_backup",
    # 大对象读写（lo_import 可把服务端文件搬进库）
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_creat", "lo_create", "lo_unlink",
})
FORBIDDEN_FUNCTION_PREFIXES = ("dblink", "pg_advisory")

# 方言级黑名单：解析方言跟随数据源后（P0），危险函数也按方言叠加 ——
# "合法 SELECT 带副作用"的函数各族方言不同，PG 清单拦不住 MySQL/duckdb 的越权读。
# 只叠加不删减：换方言时 PG 函数名单仍生效（防御纵深）。
DIALECT_FORBIDDEN_FUNCTIONS: dict[str, frozenset] = {
    "mysql": frozenset({
        "load_file",            # 读服务端文件
        "sleep", "benchmark",   # 资源放大 / 时序盲注
        "get_lock", "release_lock", "is_free_lock", "is_used_lock",
        "sys_exec", "sys_eval", # lib_mysqludf_sys 系 UDF
    }),
    "duckdb": frozenset({
        "read_csv", "read_csv_auto", "read_parquet", "parquet_scan",
        "read_json", "read_json_auto",  # 任意文件读
        "glob",
    }),
    "sqlite": frozenset({"load_extension", "readfile", "writefile"}),
    "tsql": frozenset({"openrowset", "opendatasource", "openquery"}),
    "oracle": frozenset({"dbms_random"}),
}
DIALECT_FORBIDDEN_PREFIXES: dict[str, tuple] = {
    "tsql": ("xp_",),      # xp_cmdshell 一族
    "oracle": ("utl_",),   # utl_file 一族
}

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


def _strip_sql_comments(sql: str) -> str:
    """去掉 SQL 文本里的 `--` 行注释与 `/* */` 块注释（PG 支持嵌套块注释），
    字符串字面量内的注释符原样保留（引号成对转义已处理）。

    供文本层正则检查使用：`-- 尾注` 延续敏感词会误伤合法查询，
    反过来依赖注释分割关键词的混淆写法在 PG 里解析不出合法标识符，
    但正则跑在去注释文本上可以同时消掉这两类干扰。"""
    out: list[str] = []
    in_str: str | None = None
    depth = 0  # 嵌套块注释深度
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == in_str:
                if i + 1 < n and sql[i + 1] == in_str:  # '' / "" 转义引号
                    out.append(sql[i + 1])
                    i += 2
                    continue
                in_str = None
            i += 1
            continue
        if depth:
            if ch == "/" and i + 1 < n and sql[i + 1] == "*":
                depth += 1
                i += 2
                continue
            if ch == "*" and i + 1 < n and sql[i + 1] == "/":
                depth -= 1
                i += 2
                continue
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = ch
            out.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":  # 保留换行，避免相邻 token 粘连
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            depth = 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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


def _references_sensitive_column(tree: exp.Expression, names: Collection[str]) -> bool:
    """AST 层敏感列检查：遍历所有 Column 节点比对列名（大小写不敏感）。

    比文本正则精确 —— 只匹配真实列引用，注释里提到敏感列名不会误伤；
    也是文本层的兜底升级：任何绕过文本正则的写法（未来方言 tokenizer
    变化、引号变体等）只要能解析成 AST，列名必然暴露在 Column 节点上。
    注意 `SELECT *` 不含 Column 节点，仍由执行层 filter_result_columns 兜底。"""
    blocked = {c.casefold() for c in names if c}
    if not blocked:
        return False
    return any(col.name.casefold() in blocked for col in tree.find_all(exp.Column))


def _forbidden_function_name(tree: exp.Expression, dialect: str = "postgres") -> str | None:
    """AST 层危险函数检查。PG 的非内置函数在 sqlglot 里解析为
    exp.Anonymous（node.name 即函数名），内置安全函数（COUNT/SUM…）
    是具名 Func 类，天然不在黑名单里。命中返回函数名，未命中返回 None。
    dialect 额外叠加方言黑名单（DIALECT_FORBIDDEN_FUNCTIONS）。"""
    blocked = FORBIDDEN_FUNCTIONS | DIALECT_FORBIDDEN_FUNCTIONS.get(dialect, frozenset())
    prefixes = FORBIDDEN_FUNCTION_PREFIXES + DIALECT_FORBIDDEN_PREFIXES.get(dialect, ())
    for node in tree.walk():
        name: str | None = None
        if isinstance(node, exp.Anonymous):
            name = node.name
        elif isinstance(node, exp.Func):
            name = node.sql_name()
        if not name:
            continue
        norm = name.casefold().strip('"')
        if norm in blocked:
            return name
        if any(norm.startswith(p) for p in prefixes):
            return name
    return None


def validate_sql(
    sql: str,
    sensitive_columns: Collection[str] | None = None,
    dialect: str = "postgres",
) -> tuple[bool, str]:
    """校验 SQL 安全性。返回 (is_valid, validated_sql_or_error)

    dialect: sqlglot 方言（postgres/mysql/duckdb…），由调用方从业务库连接推断后传入；
    解析与回写用同一方言，避免"用 PG 方言解析 MySQL 语法"造成的误判/失真。

    用 sqlglot 做语句级解析，彻底替代 startswith("SELECT")：
    - ★ 强制单条语句：asyncpg prepared statement 不支持多命令，且多语句是注入面。
       LLM 面对复合提问可能用分号拼接多条 SELECT，直接拒绝并给出明确提示。
    - 语句类型必须是 SELECT（含 WITH ... SELECT，sqlglot 中 WITH 挂在 Select 节点上）；
       SELECT ... INTO 会建表，同样拒绝。
    - ★ 危险函数拦截：pg_read_file / set_config / dblink / lo_import 等
       "合法 SELECT" 形态的副作用函数，AST 层按函数名拒绝。
    - ★ LIMIT 强制覆盖：不管原语句写没写、写多大，返回的 SQL 行数上限必为
       MAX_ROW_LIMIT —— 返回值是重写后的 SQL，调用方必须执行返回值而非原始 SQL。
    - ★ sensitive_columns（数据源敏感列，来自 bi_datasources）：AST 列引用 +
       去注释文本双层匹配，显式引用即拒绝。注意这只挡"写了列名"的查询；
      `SELECT *` 不含列名，由执行层的 filter_result_columns 兜底（两层缺一不可）。
    """
    stripped = sql.strip().rstrip(";")
    stripped = _remove_trailing_line_comment(stripped)
    if not stripped:
        return False, "SQL 为空"

    try:
        statements = sqlglot.parse(stripped, dialect=dialect)
    except Exception:
        return False, "SQL 解析失败"
    if not statements:
        return False, "SQL 解析失败"

    if len(statements) > 1:
        return False, "只允许单条 SELECT 语句，请把多个查询拆开"

    tree = statements[0]
    if not isinstance(tree, exp.Select):
        return False, "只允许 SELECT 查询"
    if tree.args.get("into") is not None:
        return False, "只允许 SELECT 查询"

    # 文本层检查跑在去注释 SQL 上：注释里的敏感词不再误伤合法查询
    comment_free = _strip_sql_comments(stripped)
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(comment_free):
            return False, "查询包含禁止的操作或字段"

    if sensitive_columns:
        # 配置了敏感列 → AST 精确匹配（列引用级），旧版粗正则不参与
        if _references_sensitive_column(tree, sensitive_columns):
            return False, "查询包含敏感字段，已被拦截"
    else:
        # 未配置 sensitive_columns 的数据源 → 旧版表级兜底正则
        for pattern in _LEGACY_SENSITIVE_PATTERNS:
            if pattern.search(comment_free):
                return False, "查询包含禁止的操作或字段"

    func = _forbidden_function_name(tree, dialect)
    if func:
        return False, f"查询包含不允许调用的函数 {func}"

    _clamp_row_limit(tree)
    if tree.args.get("limit") is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(MAX_ROW_LIMIT)))

    return True, _fix_sqlglot_output(tree.sql(dialect=dialect))


@dataclass(frozen=True)
class RoleFilterResult:
    """行级过滤结果。

    ★ 为什么需要第三个状态（filtered）：仅靠 allowed + SQL 无法区分
      「业务上真的没有数据」和「行级权限把它滤掉了」—— 两者都是 0 行。
      实测后果：engineer 查 plant_id=7（授权 3），行权注入 plant_id=7 AND
      plant_id=3 得到 0 行，摘要却告诉用户「该工厂暂无整车明细数据，可能未接入」
      —— 既是**信息泄露**（能推断某个域存不存在），又会让人去报一个假的数据
      缺失工单。必须把"被权限过滤"这个事实一路传到摘要。
    """
    allowed: bool
    sql: str                 # 允许时是改写后的 SQL；拒绝时是拒绝原因
    filtered: bool = False   # True = 已注入行级条件（结果为空可能是被过滤，而非真的没数据）

    def __iter__(self):
        """支持 `allowed, sql = ...` 解包。

        ★ 保留元组解包是为了不惊动 20 处既有调用点与测试；但**新代码应该
          用属性访问** —— filtered 这个新状态正是靠属性才拿得到，继续解包
          就等于主动丢掉它（那正是本次要修的缺陷）。"""
        return iter((self.allowed, self.sql))


def apply_role_filter(
    sql: str,
    role: str,
    role_rules: dict | None = None,
    params: dict | None = None,
) -> RoleFilterResult:
    """
    数据驱动的角色行级过滤。返回 RoleFilterResult(allowed, sql, filtered)。

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
        return RoleFilterResult(allowed=True, sql=sql)
    if rule is None or rule == "deny":
        return RoleFilterResult(allowed=False, sql=f"当前角色 {role} 无数据查询权限")

    condition: str | exp.Expression | None = None
    if isinstance(rule, dict):
        if rule.get("column") and rule.get("param"):
            value = params.get(rule["param"])
            if value is None:
                return RoleFilterResult(
                    allowed=False,
                    sql=f"角色 {role} 需要提供参数 {rule['param']}，才能按数据隔离查询")
            condition = _build_param_condition(rule["column"], value)
            if condition is None:
                # 列名不合法 / 参数类型不支持 / 字符串超长 → fail-closed 拒绝
                return RoleFilterResult(
                    allowed=False,
                    sql=f"角色 {role} 的过滤参数 {rule['param']} 不合法，已拒绝查询")
        elif rule.get("value"):
            condition = rule["value"]
        else:
            # 规则不完整（漏配 param/value，或空 dict）→ fail-closed。
            # 配置笔误不能等价于权限全开
            return RoleFilterResult(
                allowed=False, sql=f"角色 {role} 的过滤规则配置不完整，已拒绝查询")

    if condition:
        injected = _inject_where_ast(sql, condition)
        if injected is None:
            # AST 注入失败 → fail-closed 拒绝，绝不能放行未过滤的 SQL
            return RoleFilterResult(allowed=False, sql="行级过滤条件注入失败，已拒绝查询")
        # ★ filtered=True：下游据此区分「业务真的没数据」与「被权限滤掉了」
        return RoleFilterResult(allowed=True, sql=injected, filtered=True)
    return RoleFilterResult(allowed=True, sql=sql)


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


def _inject_where_ast(sql: str, condition: str | exp.Expression) -> str | None:
    """★ sqlglot AST 层注入 WHERE，不靠字符串替换。
    condition 可传表达式 AST（参数规则，已编码）或 SQL 片段字符串（value 规则，受信配置）。

    ★ 无条件 AND 注入：即使 SQL 里已经出现了同名列（LLM 可能被用户诱导写出
      `department_id = 7` 这样的任意值），也必须叠加授权值条件——
      "列名出现过" ≠ "过滤值正确"，去重跳过就是越权通道。
      重复 AND 同列不同值只会让结果为空集（deny 语义），是安全方向的失败。
      注入失败返回 None（调用方 fail-closed 拒绝），不做字符串拼接降级。
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
        if isinstance(condition, exp.Expression):
            condition_expr = condition
        else:
            condition_expr = sqlglot.parse_one(condition, dialect="postgres")

        where = tree.find(exp.Where)
        if where:
            where.set("this", exp.And(this=where.this, expression=condition_expr))
        else:
            tree.set("where", exp.Where(this=condition_expr))

        return _fix_sqlglot_output(tree.sql(dialect="postgres"))
    except Exception:
        return None


def _fix_sqlglot_output(sql: str) -> str:
    """修复 sqlglot 输出中 PostgreSQL 不兼容的语法。
    例如 sqlglot 会把 INTERVAL '3 months' 改写为 INTERVAL '3' MONTHS（复数不合法）。"""
    return re.sub(
        r"INTERVAL\s+'(\d+)'\s+(DAYS|HOURS|MONTHS|YEARS|WEEKS|MINUTES|SECONDS)",
        r"INTERVAL '\1 \2'",
        sql,
        flags=re.IGNORECASE,
    )


def filter_result_columns(
    columns: list[str],
    rows: list[dict],
    sensitive_columns: Collection[str] | None = None,
) -> tuple[list[str], list[dict]]:
    """执行层防线：从查询结果里剔除敏感列（堵 SELECT * 泄露）。

    元数据层故意不定义敏感列（Prompt 层防线），但物理表里它们真实存在，
    `SELECT *` 不含列名、能穿过文本层校验，行数据会原样返回给用户和摘要 LLM。
    sensitive_columns 来自 bi_datasources（= 物理存在但元数据隐藏的列），
    大小写不敏感匹配（PG 未加引号的标识符统一折叠为小写）。"""
    if not sensitive_columns:
        return columns, rows
    blocked = {c.casefold() for c in sensitive_columns if c}
    if not blocked:
        return columns, rows
    kept = [c for c in columns if c.casefold() not in blocked]
    if len(kept) == len(columns):
        return columns, rows
    filtered_rows = [
        {k: v for k, v in row.items() if k.casefold() not in blocked}
        for row in rows
    ]
    return kept, filtered_rows
