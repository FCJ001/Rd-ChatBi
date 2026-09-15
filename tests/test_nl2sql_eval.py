# ============================================================
# NL2SQL 评测纯函数单测（CI 门禁的一部分）
# 覆盖：exec-match 结果集等价比较 + 离线案例门禁
# ============================================================

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
sys.path.insert(0, str(_EVAL_DIR))

from run_nl2sql_eval import (  # noqa: E402
    CASE_FILES_BY_PROJECT,
    compare_results,
    load_cases,
    load_project_cases,
    run_offline_gate,
)


def test_identical_results_match():
    golden = [{"cnt": 42}]
    pred = [{"count": 42}]
    # 列名不同不参与比较
    assert compare_results(golden, pred)


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
    assert passed > len(load_cases()), "多数据源案例应多于单个默认文件"


def test_every_datasource_has_cases():
    """★ 每个数据源都必须有案例：原实现 50 条全打在 rd_agent 上，
    hospital_demo（默认演示数据源）零覆盖 —— 这是评测集的真实盲区"""
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
