# ============================================================
# 图表推荐 + ECharts option 构建单元测试
# 覆盖：to_echarts_option（bar/line/pie/scatter/heatmap/table）
# ============================================================

from src.nl2sql.echarts_builder import to_echarts_option

DEPT_DATA = [
    {"department": "内科", "visit_cnt": 1103},
    {"department": "外科", "visit_cnt": 897},
    {"department": "儿科", "visit_cnt": 856},
    {"department": "32产科", "visit_cnt": 742},
]


def _config(chart_type, **kw):
    base = {"chart_type": chart_type, "title": "科室门诊量", "x_column": "department", "y_column": "visit_cnt"}
    base.update(kw)
    return base


def test_bar_option():
    option = to_echarts_option(DEPT_DATA, _config("bar"))
    assert option["series"][0]["type"] == "bar"
    assert "内科" in option["xAxis"]["data"]
    assert option["series"][0]["data"] == [1103, 897, 856, 742]


def test_line_option():
    trend = [
        {"month": "2025-01", "cnt": 100},
        {"month": "2025-02", "cnt": 130},
        {"month": "2025-03", "cnt": 120},
    ]
    option = to_echarts_option(trend, {"chart_type": "line", "title": "月度趋势", "x_column": "month", "y_column": "cnt"})
    assert option["series"][0]["type"] == "line"
    assert option["series"][0]["smooth"] is True


def test_pie_option():
    option = to_echarts_option(DEPT_DATA, _config("pie"))
    assert option["series"][0]["type"] == "pie"
    names = [d["name"] for d in option["series"][0]["data"]]
    assert "内科" in names


def test_scatter_option():
    data = [{"fee": 10, "cnt": 30}, {"fee": 20, "cnt": 50}]
    option = to_echarts_option(data, {"chart_type": "scatter", "title": "费用与门诊量", "x_column": "fee", "y_column": "cnt"})
    assert option["series"][0]["type"] == "scatter"


def test_heatmap_option():
    data = [
        {"department": "内科", "month": "1月", "cnt": 10},
        {"department": "外科", "month": "1月", "cnt": 20},
        {"department": "内科", "month": "2月", "cnt": 15},
    ]
    option = to_echarts_option(data, _config("heatmap", x_column="month", y_column="department", color_column="cnt"))
    assert option["series"][0]["type"] == "heatmap"
    assert option["visualMap"]["max"] == 20


def test_table_fallback():
    option = to_echarts_option(DEPT_DATA, _config("table"))
    assert option["chart_type"] == "table"
    assert option["data"][0]["department"] == "内科"


def test_unknown_column_falls_back():
    # LLM 给了不存在的列名 → 自动回退到首/尾列
    option = to_echarts_option(DEPT_DATA, _config("bar", x_column="not_exist", y_column="nope"))
    assert "内科" in option["xAxis"]["data"]
