# ============================================================
# 值召回（ES）测试 —— 真实枚举值 vs 同义词
#
# 背景：`sync: true` 的注释一直写着「从 DB 拉取枚举值写入 ES」，但代码只灌了
# YAML 里手写的 alias。结果是模型知道「科室」对应哪个字段，却不知道科室的
# 合法取值，会编出 WHERE name='心内科' 这种查空的 SQL（SQL 完全合法、结果为空，
# 是最难排查的一类问题）。这次补齐真实值，并用 source 字段区分两类记录。
# ============================================================

import pytest

from src.nl2sql.entities import ValueInfo
from src.nl2sql.repositories.es_value_repo import ESValueRepository


# ── ValueInfo 默认值 ─────────────────────────────────────────

def test_value_info_defaults_to_alias():
    """不传 source 时按同义词处理 —— 保守默认：宁可少给一个真值，
    也不要把同义词当真值喂给模型"""
    assert ValueInfo(id="t.c.v", value="v", column_id="t.c").source == "alias"


# ── 查询构造 ─────────────────────────────────────────────────

def test_query_matches_both_kinds():
    """★ 两类记录都要参与检索。

    只查 db 值会漏掉最关键的场景：库里 status 存英文 'paid'，
    用户说「已缴费」—— standard 分词下两者字面上毫无交集，
    必须靠 alias「已缴费」命中 status 这一列，才能把它自己的真实值 'paid' 带出来。
    所以这里刻意**不加** source 过滤。"""
    q = ESValueRepository._build_query("已缴费")
    assert "bool" not in q, "不应按 source 过滤，否则中文同义词命中不了英文值"
    assert q["match"]["value"]["query"] == "已缴费"
    assert q["match"]["value"]["operator"] == "or"


# ── 端到端（假 ES）───────────────────────────────────────────

class _FakeES:
    """够用的 ES 替身：支持第①步的 match 查询和第②步的 terms+source 查询。

    ★ 第①步按**子串**匹配，不是精确相等：真实 ES 的分词器会把
      关键词和文档都切碎，只要共享词元就命中（如「缴费」命中「已缴费」）。
      替身用精确相等会漏掉这类命中，导致测试与线上行为不一致。
    """

    def __init__(self, docs):
        self.docs = docs            # list[dict]

    class _Indices:
        async def exists(self, index):
            return True

    indices = _Indices()

    async def search(self, index, body):
        q = body["query"]
        size = body.get("size", 10)
        if "match" in q:                    # 第①步：子串命中
            kw = q["match"]["value"]["query"]
            hits = [d for d in self.docs if kw in d["value"] or d["value"] in kw]
        else:                               # 第②步：terms column_id + source=db
            must = q["bool"]["must"]
            cols = set(next(m["terms"]["column_id"] for m in must if "terms" in m))
            hits = [d for d in self.docs
                    if d["column_id"] in cols and d.get("source") == "db"]
        return {"hits": {"hits": [{"_source": d} for d in hits[:size]]}}


async def test_search_returns_both_kinds_with_source():
    """两类混在同一索引，检索都返回，靠 source 区分"""
    es = _FakeES([
        {"id": "departments.name.db.内科", "value": "内科",
         "column_id": "departments.name", "source": "db"},
        {"id": "departments.name.alias.科室", "value": "科室",
         "column_id": "departments.name", "source": "alias"},
    ])
    repo = ESValueRepository(es, prefix="chatbi")

    # 搜到真实值：这是该列的合法取值
    got = await repo.search("内科")
    assert [v.value for v in got] == ["内科"]
    assert got[0].source == "db"
    # 搜到同义词：同时把该列的真实值一并带回来（见下一个用例）


async def test_search_brings_real_values_of_matched_column():
    """★★ 这是本次修复的核心。

    用户说中文「已缴费」，库里 status 存的是英文 'paid' —— 字面对不上，
    ES 永远匹配不到 'paid'。所以拿到命中的列之后，必须**再把这些列的
    真实值取回来**，否则模型知道要查 status，却仍写 WHERE status='已缴费'。
    """
    es = _FakeES([
        {"id": "outpatient_visits.status.alias.已缴费", "value": "已缴费",
         "column_id": "outpatient_visits.status", "source": "alias"},
        {"id": "outpatient_visits.status.alias.缴费", "value": "缴费",
         "column_id": "outpatient_visits.status", "source": "alias"},
        {"id": "outpatient_visits.status.db.paid", "value": "paid",
         "column_id": "outpatient_visits.status", "source": "db"},
        {"id": "outpatient_visits.status.db.unpaid", "value": "unpaid",
         "column_id": "outpatient_visits.status", "source": "db"},
        {"id": "departments.name.db.内科", "value": "内科",
         "column_id": "departments.name", "source": "db"},   # 别的列，不该被带出来
    ])
    repo = ESValueRepository(es, prefix="chatbi")
    # size 给足：真实值会被提到前面，size 太小会把同义词挤掉
    got = await repo.search("已缴费", size=10)

    db = {v.value for v in got if v.source == "db"}
    alias = {v.value for v in got if v.source == "alias"}
    assert db == {"paid", "unpaid"}, "必须把命中列的真实值带回来"
    assert alias == {"已缴费", "缴费"}
    assert "内科" not in {v.value for v in got}, "不应带回无关列的值"


async def test_search_legacy_docs_still_returned():
    """升级前写入的老文档（无 source 字段）不能被静默丢掉"""
    es = _FakeES([
        {"id": "t.c.旧词", "value": "旧词", "column_id": "t.c"},   # 无 source
    ])
    repo = ESValueRepository(es, prefix="chatbi")
    got = await repo.search("旧词")
    assert [v.value for v in got] == ["旧词"]
    assert got[0].source == "alias", "缺 source 应按同义词处理"
