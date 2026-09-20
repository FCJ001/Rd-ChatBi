# ============================================================
# NL2SQL 评测纯函数单测（CI 门禁的一部分）
# 覆盖：exec-match 结果集等价比较 + 离线案例门禁
# ============================================================

import sys
from datetime import date, datetime, timezone
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
sys.path.insert(0, str(_EVAL_DIR))

from run_nl2sql_eval import (  # noqa: E402
    AMBIGUOUS,
    CASE_FILES_BY_PROJECT,
    EXACT,
    MISMATCH,
    check_layer_gates,
    classify_results,
    compare_results,
    load_cases,
    load_project_cases,
    run_offline_gate,
)


# ══ 三态判定：为什么需要 AMBIGUOUS ══════════════════════════════════
# 背景：exec-match 用多重集相等判定，前提是"这个问题有唯一答案"。golden 带
# LIMIT N 而候选行 ⊋ N 行时前提不成立（"最近10条"在最新时间戳上并列 11 行，
# 哪 10 条是任意的）。旧判定把这种情况记成 ✗，等于把"判分器问了没有唯一答案
# 的问题"记成"模型答错了"——分数系统性偏低且不可归因。

def test_exact_and_mismatch():
    assert classify_results([{"a": 1}], [{"a": 1}]) == EXACT
    assert classify_results([{"a": 1}], [{"a": 2}]) == MISMATCH


def test_subset_relationship_is_ambiguous_not_failure():
    """LIMIT 截断点不同 → 子集关系 → 无唯一答案，既不算过也不算错"""
    golden = [{"id": i} for i in range(10)]
    pred = [{"id": i} for i in range(20)]      # pred ⊃ golden
    assert classify_results(golden, pred) == AMBIGUOUS
    assert classify_results(pred, golden) == AMBIGUOUS  # 反向亦然


def test_partial_overlap_is_mismatch_not_ambiguous():
    """★ 关键反例：有交集但不构成包含 → 是真实的条件差异，必须判失败。
    若把"有重叠"当宽松通过，判分就等于放弃。"""
    golden = [{"id": i} for i in range(10)]
    pred = [{"id": i} for i in range(5, 15)]   # 交集 5 行，但不互相包含
    assert classify_results(golden, pred) == MISMATCH


def test_disjoint_is_mismatch():
    assert classify_results([{"a": 1}], [{"a": 2}]) == MISMATCH


def test_empty_side_is_not_ambiguous():
    """空集不判 AMBIGUOUS：空集 ⊆ 任何集合，但它更应该走"golden 返回空集"
    的既有告警路径，而不是被静默豁免。"""
    assert classify_results([], [{"a": 1}]) == MISMATCH
    assert classify_results([{"a": 1}], []) == MISMATCH


def test_identical_results_match():
    golden = [{"cnt": 42}]
    pred = [{"count": 42}]
    # 列名不同不参与比较
    assert compare_results(golden, pred)


# ══ 列子集匹配：golden 单列 / 模型多给列 ══════════════════════════════
# 实测依据（AUF40「上个月和上上个月相比变化多少」）：
#   golden = [{diff: 0}]  1 列 1 行
#   pred   = [{上月: 7728, 上月: 7728, diff: 0}]  3 列 1 行
# 数值完全正确，旧实现把行内值排序抹平列结构后判成 mismatch。

def test_extra_columns_are_ignored():
    golden = [{"diff": 0}]
    pred = [{"last_month_cnt": 7728, "prev_month_cnt": 7728, "diff_result": 0}]
    assert compare_results(golden, pred)
    assert classify_results(golden, pred) == EXACT


def test_extra_columns_do_not_mask_wrong_value():
    """★ 列子集匹配不能变成"有重叠就放过"：golden 的值必须在 pred 里找得到"""
    golden = [{"diff": 0}]
    pred = [{"last_month_cnt": 7728, "prev_month_cnt": 7728, "diff_result": 5}]
    assert classify_results(golden, pred) == MISMATCH


def test_row_count_must_match_for_exact():
    golden = [{"a": 1}, {"a": 2}]
    pred = [{"a": 1}]
    assert classify_results(golden, pred) != EXACT


def test_one_to_one_matching_not_greedy():
    """★ 必须一对一消耗匹配：pred 的一行不能被 golden 的多行重复认领。
    golden 两行都是 {1}，pred 只有一行 {1} → 行数不同，不可能 EXACT。"""
    golden = [{"a": 1}, {"a": 1}]
    pred = [{"a": 1}]
    assert classify_results(golden, pred) != EXACT


# ══ 日期等价归一化 ═══════════════════════════════════════════════════
# 实测依据（AUF22「今年以来每个月的交付量」）：9 个月的计数逐个相同
# （1789/1616/1789/.../924），唯一差异是 golden 返回 datetime、模型返回
# '2026-01' 字符串 —— 模型把月份格式化得更规范，反而被判错。

def test_datetime_and_month_string_are_equivalent():
    assert compare_results([{"m": datetime(2026, 1, 1, tzinfo=timezone.utc)}],
                           [{"month": "2026-01"}])


def test_timestamp_midnight_equals_month_string():
    """TIMESTAMP 零点（date_trunc 产物）与 'YYYY-MM' 等价"""
    assert compare_results([{"d": datetime(2026, 1, 1)}], [{"m": "2026-01"}])
    assert compare_results([{"d": datetime(2026, 1, 1, tzinfo=timezone.utc)}],
                           [{"m": "2026-01"}])


def test_plain_date_string_not_shortened():
    """★ 只有带时间部分的 TIMESTAMP 才缩到月。

    纯 '2026-01-01' 字符串不缩 —— 它就是 PG DATE 列的 str() 形态，
    缩短它会让「PG 的 DATE 列」与「模型 TO_CHAR 的同一天」不等价。
    这条防的是归一化过头导致的不一致。"""
    assert compare_results([{"d": date(2026, 1, 1)}], [{"x": "2026-01-01"}])
    # DATE 对象与 "2026-01" 不等价（日被明确写了）
    assert not compare_results([{"d": date(2026, 1, 1)}], [{"x": "2026-01"}])


def test_different_months_stay_different():
    """★ 归一的边界：不同月不能被混为一谈"""
    assert not compare_results([{"m": datetime(2026, 1, 1)}], [{"m": "2026-02"}])
    assert not compare_results([{"m": datetime(2026, 1, 1)}], [{"m": "2026-01-15"}])


def test_non_date_strings_untouched():
    assert compare_results([{"s": "open"}], [{"s": "open"}])
    assert not compare_results([{"s": "open"}], [{"s": "closed"}])


# ══ 分层门限：单一总分门限会把「某一类完全不可用」平均掉 ══════════════
# 依据：auto_full 总体 66% 看着还行，拆开是单表聚合 100% / 明细查询 0%。

def test_layer_gate_flags_category_below_expect():
    stats = {"单表聚合": [True] * 6, "明细查询": [False] * 6}
    failures = check_layer_gates("auto_full", stats)
    assert len(failures) == 1
    assert "明细查询" in failures[0]
    assert "0/6" in failures[0]


def test_layer_gate_passes_when_all_categories_full():
    stats = {"单表聚合": [True] * 6, "明细查询": [True] * 6}
    assert check_layer_gates("auto_full", stats) == []


def test_layer_gate_skips_small_samples():
    """题数低于 min_n 不出结论 —— 否则 2 题错 1 题就是 -50pp，噪音当信号"""
    assert check_layer_gates("auto_full", {"明细查询": [True, False]}) == []


def test_layer_gate_ignores_difficulty_dimension():
    """★ 实测踩过：难度维度被当成类别，按 100% 门限报一堆假失败。

    根因是用了排除法 `"/" not in k` —— 但英文难度键（easy/medium/hard）
    本身不含 "/"，排除法挡不住。必须用正面白名单（案例里的 category 集合）。"""
    stats = {
        "单表聚合": [True] * 6,          # 真类别，满分 → 不报
        "easy": [True] * 16 + [False] * 6,   # 难度维度 → 不该出现在门禁里
        "hard": [False] * 7,                 # 同上
    }
    assert check_layer_gates("auto_full", stats) == []


def test_layer_gate_ignores_internal_and_difficulty_keys():
    """_overall / "类别/难度" 复合键同样不能进判定 —— 复合键和类别键是
    同一组结果的两种视角，重复计数会让门限失真。"""
    stats = {"_overall": [True], "明细查询": [True] * 6, "明细查询/hard": [True] * 6}
    assert check_layer_gates("auto_full", stats) == []


# ══ 漂移时间 lint：恒空案例会「0 = 0 判过」 ═══════════════════════════
# 实测：AUF24「昨天的产线安灯事件有多少次」自上线起恒返回 0 却一直判通过 ——
# 种子数据的时间上界停在生成脚本跑的那天，now() 永远在它之后。
# COUNT(*) 形态更隐蔽：它永远返回 1 行（值为 0），按行数检测抓不到。

def test_lint_flags_drifting_time_expressions():
    from run_nl2sql_eval import _lint_case_quality

    case = {"id": "X1", "golden_sql":
            "SELECT COUNT(*) FROM t WHERE created_at >= date_trunc('day', now())"}
    out = _lint_case_quality("auto_full", case, "")
    assert any("漂移" in w for w in out), out


def test_lint_does_not_flag_fixed_dates():
    """固定日期字面量的案例不随时间漂移，不该被告警 —— lint 只报真问题，
    报太宽会让人忽略它（实测 7 条告警里只有 4 条真的恒空）。"""
    from run_nl2sql_eval import _lint_case_quality

    case = {"id": "X2", "golden_sql":
            "SELECT COUNT(*) FROM t WHERE created_at >= '2026-08-01' AND created_at < '2026-09-01'"}
    out = _lint_case_quality("auto_full", case, "")
    assert not any("漂移" in w for w in out), out


def test_lint_can_report_both_problems():
    """漂移时间 + 无 ORDER BY 的 LIMIT 可以同时命中，不能因为先返回一个就丢掉另一个"""
    from run_nl2sql_eval import _lint_case_quality

    case = {"id": "X3", "golden_sql":
            "SELECT id FROM t WHERE created_at > now() LIMIT 20"}
    out = _lint_case_quality("auto_full", case, "")
    assert any("漂移" in w for w in out), out
    assert any("ORDER BY" in w for w in out), out


def test_row_order_insensitive():
    golden = [{"bl": "ev", "c": 3}, {"bl": "ia", "c": 5}]
    pred = [{"biz": "ia", "n": 5}, {"biz": "ev", "n": 3}]
    assert compare_results(golden, pred)


def test_numeric_precision_tolerance():
    golden = [{"avg": 10.123456}]
    pred = [{"avg": 10.123460}]
    assert compare_results(golden, pred)          # 4 位舍入后相等
    assert not compare_results(golden, [{"avg": 10.2}])


def test_decimal_and_string_normalized():
    from decimal import Decimal

    golden = [{"name": " 电池域 ", "ratio": Decimal("0.5")}]
    pred = [{"d": "电池域", "r": 0.5}]
    assert compare_results(golden, pred)


def test_missing_rows_detected():
    golden = [{"c": 1}, {"c": 2}]
    pred = [{"c": 1}]
    assert not compare_results(golden, pred)


def test_offline_gate_passes():
    """离线门禁覆盖**全部**注册数据源的案例文件（不只默认那一个）"""
    passed, failed, failures = run_offline_gate()
    assert failed == 0, "\n".join(failures)
    expected = sum(len(load_project_cases(p)) for p in CASE_FILES_BY_PROJECT)
    assert passed == expected


def test_every_datasource_has_cases():
    """★ 每个数据源都必须有案例：原实现全打在单一数据源上、
    其余数据源零覆盖 —— 这是评测集的真实盲区"""
    for project, files in CASE_FILES_BY_PROJECT.items():
        assert any(f.exists() for f in files), f"数据源 {project} 没有案例文件"
        assert load_project_cases(project), f"数据源 {project} 案例为空"


def test_case_ids_unique_across_files():
    """跨文件 id 不能重复（合并后统计会串）"""
    ids = []
    for project in CASE_FILES_BY_PROJECT:
        ids += [c["id"] for c in load_project_cases(project)]
    assert len(ids) == len(set(ids))


def test_unknown_project_raises():
    import pytest

    with pytest.raises(FileNotFoundError):
        load_project_cases("no_such_project")


def test_reflow_files_exist():
    """★ 回流案例文件必须始终存在（空集也要有 {"cases": []}）。

    _files_for 会按 f.exists() 过滤 —— 文件不存在时评测器静默少读一批案例，
    案例集会在「有没有跑过导出」之间悄悄变化，而门禁照样全绿。
    这里把它钉死：少一个文件就红。
    """
    for project, files in CASE_FILES_BY_PROJECT.items():
        reflow = [f for f in files if "reflow" in f.name]
        assert reflow, f"数据源 {project} 没有配置回流案例文件"
        for f in reflow:
            assert f.exists(), (
                f"回流案例文件缺失: {f.name} —— "
                f"跑 python scripts/export_badcase_cases.py --write 生成"
            )
            import json

            json.loads(f.read_text(encoding="utf-8"))  # 必须是合法 JSON


def test_reflow_ids_do_not_collide():
    """★ 回流案例 id 用 R 前缀，不能与存量 N*/H* 撞 ——
    load_project_cases 撞 id 是直接 raise 的，会让整个离线门禁报错"""
    for project in CASE_FILES_BY_PROJECT:
        ids = [c["id"] for c in load_project_cases(project)]
        reflow_ids = [i for i in ids if i.startswith("R")]
        legacy = [i for i in ids if not i.startswith("R")]
        assert not (set(reflow_ids) & set(legacy)), f"{project}: 回流 id 与存量 id 冲突"


# ══ 舍入等价：判分器必须承认「ROUND 不改变答案」 ══════════════════════
# 背景（AUF2/AUF5 实测，auto_full 2026-09-19）：模型写 ROUND(AVG(score), 2)
# 得 7.00，golden 是裸 AVG = 7.0009 —— 旧判定按 4 位精度硬比必判 ✗，等于
# 惩罚模型输出得更可读。单表聚合类因此从 100% 掉到 50~67%，且每次挂的题
# 不一样（模型哪天不写 ROUND 就过）—— 分数在掷硬币。


def test_rounding_equivalence_pred_rounds_golden():
    """AUF5：模型 ROUND(AVG,2)=7.00 vs golden 裸 AVG=7.00086875 → 同一答案"""
    golden = [{"avg_score": 7.0008687500000000}]
    assert classify_results(golden, [{"avg_score": 7.0}]) == EXACT
    assert classify_results(golden, [{"avg_score": 7.001}]) == EXACT  # ROUND(...,3)


def test_rounding_equivalence_with_extra_context_column():
    """AUF2：模型多带一列 COUNT(*)（列超集本就放行），avg 还做了 ROUND"""
    golden = [{"avg_soh": 86.5008888888888889}]
    pred = [{"avg_soh": 86.5, "record_cnt": 4192}]
    assert classify_results(golden, pred) == EXACT


def test_rounding_equivalence_golden_has_round_pred_raw():
    """反方向：golden 里写了 ROUND(...,2)，模型给了原始值 → 也算同一答案"""
    assert classify_results([{"a": 7.0}], [{"a": 7.0008687}]) == EXACT


def test_rounding_equivalence_does_not_mask_real_difference():
    """边界：真实数值差异不是彼此的舍入产物，必须仍然 MISMATCH。
    ★ 100.5 特别值得钉住：round(100.5, 0) 银行家舍入 = 100，
      反方向若放开 k=0 会凭空接受 0.5 的偏差。"""
    assert classify_results([{"a": 1234.56}], [{"a": 1234.99}]) == MISMATCH
    assert classify_results([{"a": 100.0}], [{"a": 100.5}]) == MISMATCH
    assert classify_results([{"a": 105}], [{"a": 107}]) == MISMATCH


def test_rounding_equivalence_ignores_strings_and_dates():
    """字符串/日期不走数值舍入：'营业' 与 '营业中' 不会因为「长得像」而等价"""
    assert classify_results([{"a": 1234.56}], [{"a": "1234.56"}]) == MISMATCH
    assert classify_results([{"s": "营业"}], [{"s": "营业中"}]) == MISMATCH


# ══ 评测路径的值召回注入 ════════════════════════════════════════════
# 背景（AUF1 实测）：run_query 是静态 schema 直连生成的弱路径，ES 里的
# 枚举真实取值到不了模型眼前，WHERE 字面量全凭先验（库里 '营业'、模型
# 写 '营业中'）。评测必须与在线路径看到同一个 prompt。


def _fake_tables():
    from src.nl2sql.entities import ColumnInfo, TableInfo

    status = ColumnInfo(id="sal_dealers.status", name="status", type="VARCHAR(20)",
                        role="dimension", description="营业状态")
    name = ColumnInfo(id="sal_dealers.name", name="name", type="VARCHAR(100)",
                      role="dimension", description="经销商名称")
    return [TableInfo(id="sal_dealers", name="sal_dealers", role="fact",
                      description="经销商", columns=[status, name])]


def test_schema_with_recalled_values_mounts_db_and_alias_labels():
    """db 值标「真实值」、alias 标「同义词」—— 与在线 merge_info 同款标注"""
    from src.nl2sql.entities import ValueInfo

    from run_nl2sql_eval import _schema_with_recalled_values

    tables = _fake_tables()
    values = [
        ValueInfo(id="sal_dealers.status.db.营业", value="营业",
                  column_id="sal_dealers.status", source="db"),
        ValueInfo(id="sal_dealers.name.alias.经销商", value="经销商",
                  column_id="sal_dealers.name", source="alias"),
    ]
    schema = _schema_with_recalled_values(tables, values)
    assert "营业（真实值）" in schema
    assert "经销商（同义词，非库中取值）" in schema


def test_schema_with_recalled_values_restores_examples():
    """★ tables 被整个评测循环复用：挂上去的值用完必须恢复，
    否则前一题召回的值泄漏进后一题的 prompt"""
    from src.nl2sql.entities import ValueInfo

    from run_nl2sql_eval import _schema_with_recalled_values

    tables = _fake_tables()
    values = [ValueInfo(id="sal_dealers.status.db.营业", value="营业",
                        column_id="sal_dealers.status", source="db")]
    _schema_with_recalled_values(tables, values)
    status_col = tables[0].columns[0]
    assert status_col.examples == [], "examples 未恢复，值会跨案例泄漏"


def test_schema_with_recalled_values_unknown_column_is_noop():
    """召回值指向元数据外的列（ES 脏数据/元数据重同步过）→ 静默跳过"""
    from src.nl2sql.entities import ValueInfo

    from run_nl2sql_eval import _schema_with_recalled_values

    tables = _fake_tables()
    values = [ValueInfo(id="ghost_table.x.db.值", value="值",
                        column_id="ghost_table.x", source="db")]
    schema = _schema_with_recalled_values(tables, values)
    assert "值（真实值）" not in schema
