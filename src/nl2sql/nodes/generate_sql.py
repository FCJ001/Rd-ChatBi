# ============================================================
# Node ⑥ — LLM 生成 SQL
# ============================================================

import sqlglot
import sqlglot.expressions as exp
import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from src.nl2sql.llm_text import safe_ainvoke, strip_code_fence
from src.nl2sql.state import DataAgentState
from src.nl2sql.context import DataAgentContext
from src.nl2sql.prompt_loader import load_prompt

# 拒答时把纠错预算顶掉用的哨兵值。★ 必须 ≥ graph.MAX_SQL_FIX_ROUNDS，
# 否则 _route_after_validate 会判"还有预算"再跑一轮 correct_sql 帮倒忙。
# 断言在 tests/test_refusal_shortcircuit.py，改预算时测试会拦下来。
REFUSAL_FIX_ROUNDS = 1_000_000


def is_sql_text(text: str) -> bool:
    """输出是否是 SQL：用 sqlglot 解析成单条 SELECT 才算。

    ★ 不用 `startswith("select")` 判断（旧实现踩过）：前缀会骗人 ——
      ```` ```sql\\nSELECT 1``` ```` / `-- 说明\\nSELECT 1` / `以下 SQL：\\nSELECT…`
      这些带围栏、带前导注释、带前言的合法 SQL 全被判成拒答，而模型输出恰好
      就是这三种形态（prompt 里"不要 markdown 代码块"只是约定，不是保证）。
      反过来，模型拒答「…我不能生成删除数据的 SQL 语句」在 sqlglot 里
      解析失败 → 判为非 SQL，正是我们要的。

    调用方（generate_sql / validate_sql / engine）在判别前都已剥过围栏 + 散文
    （llm_text.clean_model_output），这里再兜一次剥皮，保证任何入口传进来都稳。

    空串返回 False：走「SQL 为空」的既有路径，拒答短路不抢这个分支。"""
    src = strip_code_fence(text or "").strip()
    if not src:
        return False
    try:
        statements = sqlglot.parse(src)
    except Exception:
        return False
    return bool(statements) and all(
        isinstance(s, exp.Select) and s.args.get("into") is None
        for s in statements if s is not None
    )


async def generate_sql(state: DataAgentState, ctx: DataAgentContext) -> dict:
    """LLM 根据筛选后的表/列/指标生成 SQL"""
    writer = ctx.get("writer")
    if writer:
        writer({"type": "progress", "step": "生成SQL", "status": "running"})

    try:
        llm = ctx["llm"]

        # 序列化表信息
        tables_list = []
        for t in state["table_infos"]:
            cols = [{"name": c.name, "type": c.type, "role": c.role,
                     "description": c.description, "examples": c.examples[:5] if c.examples else None}
                    for c in t.columns]
            tables_list.append({"name": t.name, "role": t.role,
                               "description": t.description, "columns": cols})
        table_infos_str = yaml.dump(tables_list, allow_unicode=True, default_flow_style=False)

        # 序列化指标
        metrics_list = [{"name": m.name, "description": m.description} for m in state["metric_infos"]]
        metric_infos_str = yaml.dump(metrics_list, allow_unicode=True, default_flow_style=False)

        date_info = state.get("date_info", {})
        db_info = state.get("db_info", {})

        # 时间锚点（P1）：锚点行由 add_context 预计算；state 没有时兜底现场全量算。
        # 渲染统一走 render_anchor_lines —— 头部硬规则单一来源，不再手拼简化版。
        from src.nl2sql.time_anchors import format_anchor_block, render_anchor_lines
        anchors = date_info.get("anchors")
        if anchors:
            time_anchors_str = render_anchor_lines(anchors)
        else:
            time_anchors_str = format_anchor_block()

        # 数据时间上界：告诉 LLM"库里的数据到哪天为止"。
        # ★ 防的是实测缺陷：数据上界 09-16，用户问"这个月17号"，LLM 照样生成
        #   查 09-17 的 SQL，查到空，摘要再把空解读成"销量归零、环比断崖下滑"。
        #   把上界摆出来，它至少有机会在 prompt 层意识到时间段超出数据范围。
        from src.nl2sql.time_bounds import render_time_bounds
        time_bounds_str = render_time_bounds(state.get("time_bounds") or [])

        # few-shot 示例（P1 主线 B）：相似问答对参考口径
        from src.nl2sql.example_store import format_examples_block
        few_shot_str = format_examples_block(state.get("few_shot_examples") or [])

        dialect = db_info.get("dialect", "PostgreSQL")
        db_str = f"{dialect} {db_info.get('version', '')}".strip()

        # 加载并填充 prompt
        system_prompt = load_prompt("generate_sql")
        system_prompt = system_prompt.replace("{table_infos}", table_infos_str)
        system_prompt = system_prompt.replace("{metric_infos}", metric_infos_str)
        system_prompt = system_prompt.replace("{date}", date_info.get("date", ""))
        system_prompt = system_prompt.replace("{quarter}", date_info.get("quarter", ""))
        system_prompt = system_prompt.replace("{dialect}", dialect)
        system_prompt = system_prompt.replace("{db}", db_str)
        system_prompt = system_prompt.replace("{time_anchors}", time_anchors_str)
        system_prompt = system_prompt.replace("{time_bounds}", time_bounds_str)
        system_prompt = system_prompt.replace("{few_shot}", few_shot_str)

        response = await safe_ainvoke(llm, [
            SystemMessage(content=system_prompt),
            HumanMessage(content=state["query"]),
        ])

        sql = strip_code_fence(response.content)

        from src.core.logger import logger
        logger.info(f"[generate_sql] 生成 SQL ({len(sql)} 字符): {sql[:200]}")

        # ★ 拒答短路：模型输出了自然语言说明而非 SQL（典型：用户要敏感字段，
        #   第一层防线把敏感列藏了，模型如实回答「没有这个字段」）。
        #   此时纠错只会帮倒忙 —— 纠错节点同样看不见被隐藏的列，会编造
        #   phone_number 这类别名硬凑（实测踩坑）。直接把模型说明作为
        #   用户可见结果返回，并烧掉纠错预算让图尽快终结。
        if sql.strip() and not is_sql_text(sql):
            msg = sql.strip()[:300]
            logger.info(f"[generate_sql] 模型拒答（非 SQL 输出）: {msg[:80]}")
            if writer:
                writer({"type": "progress", "step": "生成SQL", "status": "error"})
                writer({"type": "result", "data": {
                    "success": False,
                    "error": f"该请求无法生成查询：{msg}",
                }})
            return {"sql": sql, "error": f"模型拒答：{msg}",
                    "sql_fix_rounds": REFUSAL_FIX_ROUNDS}

        if writer:
            writer({"type": "progress", "step": "生成SQL", "status": "success"})
        return {"sql": sql}
    except Exception as e:
        if writer:
            writer({"type": "progress", "step": "生成SQL", "status": "error"})
        raise
