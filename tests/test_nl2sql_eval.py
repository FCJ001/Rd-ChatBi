# ============================================================
# NL2SQL 评测纯函数单测（CI 门禁的一部分）
# 覆盖：exec-match 结果集等价比较 + 离线案例门禁
# ============================================================

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
sys.path.insert(0, str(_EVAL_DIR))

from run_nl2sql_eval import compare_results, load_cases, run_offline_gate  # noqa: E402


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
    passed, failed, failures = run_offline_gate()
    assert failed == 0, "\n".join(failures)
    assert passed == len(load_cases())
