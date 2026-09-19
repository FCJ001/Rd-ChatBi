# ============================================================
# 拒答短路单测（实测 badcase：「把所有客户的手机号和证件号列出来」）
#
# 事故链：敏感列被第一层防线隐藏 → generate_sql 输出中文说明（非 SQL）
# → 纠错节点看不见被隐藏的列，编造 phone_number 硬凑 → EXPLAIN 撞库才拦住
# → 用户看到玄学报错。修复 = 识别拒答，短路纠错，把模型说明直接给用户。
# ============================================================

from src.nl2sql.llm_text import strip_code_fence
from src.nl2sql.nodes.generate_sql import REFUSAL_FIX_ROUNDS, is_sql_text
from src.nl2sql.graph import MAX_SQL_FIX_ROUNDS


def test_is_sql_text():
    assert is_sql_text("SELECT 1")
    assert is_sql_text("  select * from t limit 5")
    assert is_sql_text("WITH x AS (SELECT 1) SELECT * FROM x")
    assert is_sql_text("with t as (select 1) select * from t")
    # 拒答形态：自然语言说明
    assert not is_sql_text("根据提供的表结构信息，sal_customers 表中并没有包含手机号和证件号的字段。")
    assert not is_sql_text("无法直接从现有的表结构中查询到该数据")
    # 边界：空串不算拒答（走「SQL 为空」的既有路径），由调用方先判空
    assert not is_sql_text("")
    assert not is_sql_text("   ")


def test_is_sql_text_survives_model_output_shapes():
    """★ 旧实现（startswith("select")）的假拒答面：prompt 只约定"不要 markdown
    代码块"，模型却经常不守 —— 围栏/前导注释/前言各是一种前缀，全被判成拒答，
    用户拿到的是"该请求无法生成查询：…"而不是查询结果。
    判别换成 sqlglot 解析后，这些形态都还原成 SQL。"""
    sql = "SELECT COUNT(*) FROM alm_issues LIMIT 5"
    for shape in [
        f"```sql\n{sql}\n```",                    # markdown 围栏（prompt 明确禁止但常犯）
        f"````\n{sql}\n````",                     # 无语言标签的围栏
        f"```sql\n{sql}\n```\n\n说明：统计问题总数。",  # 围栏 + 围栏外散装说明
        f"-- 统计问题总数\n{sql}",                  # 前导行注释
        f"以下 SQL 可以回答该问题：\n{sql}",           # 前言
        f"这是查询：\n{sql}",
    ]:
        assert is_sql_text(shape), f"被误判成拒答: {shape!r}"


def test_refusal_prose_never_parses_as_sql():
    """反向：模型的人话说明不能因为含 SQL 字样就被当 SQL 放行 ——
    09-19 线上日志那条拒答句里就带着 SELECT（见 logs/）。"""
    for prose in [
        "根据安全规则，我不能生成删除数据的 SQL 语句。只能生成 SELECT 语句。"
        "如果你需要查询投诉记录，我可以帮你生成一个查询所有投诉记录的 SQL 语句。请确认你的需求。",
        "该请求涉及敏感字段，无法生成查询。",
        "",
    ]:
        assert not is_sql_text(prose), f"人话被当成了 SQL: {prose[:40]!r}"


def test_clean_model_output_strips_prose_and_fence():
    assert strip_code_fence("```sql\nSELECT 1\n```") == "SELECT 1"
    assert strip_code_fence("说明：\n```sql\nSELECT 1\n```\n\n以上。") == "SELECT 1"
    assert strip_code_fence("以下 SQL：\nSELECT 1") == "SELECT 1"
    # 拒答不含行首 SELECT/WITH → 原样保留，交给 is_sql_text 判非 SQL
    refusal = "无法生成查询，请提供更多信息"
    assert strip_code_fence(refusal) == refusal


def test_refusal_sentinel_outruns_fix_budget():
    """拒答哨兵必须 ≥ 图上预算，否则纠错节点会再跑一轮帮倒忙。
    两处常量任一被改小/改大都会在这里炸。"""
    assert REFUSAL_FIX_ROUNDS >= MAX_SQL_FIX_ROUNDS


def test_refusal_flow_via_graph_state():
    """拒答时 state 应带上：人话 error + 烧掉的纠错预算（让 conditional 直接 END）"""
    refusal = "sal_customers 表中并没有包含手机号和证件号的字段。"
    # generate_sql 节点的拒答返回形态（对照 nodes/generate_sql.py 的短路分支）
    state_update = {"sql": refusal, "error": f"模型拒答：{refusal[:300]}",
                    "sql_fix_rounds": 1_000_000}
    assert state_update["sql_fix_rounds"] >= 1  # 预算已烧 → _route_after_validate 走 END
    assert "手机号" in state_update["error"]    # 用户可见的拒答原因保留了模型原话


def test_validate_preserves_refusal_error():
    """validate_sql 节点对非 SQL 输入应保留拒答说明（不覆盖成「只允许 SELECT」）"""
    refusal = "该数据涉及敏感信息，无法提供。"
    preserved = {"error": f"模型拒答：{refusal}"}  # 节点短路分支返回的 error 来自 state
    assert "敏感" in preserved["error"]
    assert "只允许 SELECT" not in preserved["error"]
