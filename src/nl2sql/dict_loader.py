# ============================================================
# jieba 用户词典加载 —— 把业务词喂给分词器
#
# 问题：默认词典不认识「门诊量」「问题单」这类业务词，会切碎或切错
#   （实测「未关闭」被切成「关闭」，语义直接反了）。
# 做法：启动时把元数据里的表名、字段别名、指标名注册成用户词典，
#   让第①步 extract_keywords 从一开始就切对。
#
# ★ 必须走 jieba 的公开 API（add_word / load_userdict）而不是设
#   jieba.dt 属性 —— jieba 是否用 jieba.dt 取决于其内部初始化模式，
#   直接改属性在部分环境静默无效。
#
# ★ 进程级一次性：多次调用只生效第一次（词典是全局状态）。
# ★ fail-soft：元数据库没起来/表不存在时返回 0，不影响服务启动
#   （词典只提升召回质量，不是正确性依赖）。
# ============================================================

from __future__ import annotations

import jieba

from src.core.logger import logger

_MIN_WORD_LEN = 2      # 单字不入词典（会破坏分词粒度）
_MAX_WORD_LEN = 20     # 超长词多半是描述文本，不是业务词
_MAX_WORDS = 20000     # 硬上限：词典过大拖慢分词

_loaded = False


def _collect_words(tables, columns, metrics) -> set[str]:
    """只收**业务词**。

    ★ 不要收表名/列名（table_name / column_name）——它们是英文标识符
      （outpatient_visits、visit_date），加进中文词典既无意义，还会让
      词表膨胀、分词变慢。真正需要的是 description 和 aliases 里的中文。
    """
    words: set[str] = set()
    for t in tables:
        if t.description:
            words.add(t.description)
    for c in columns:
        for w in [c.description or "", *(c.aliases or [])]:
            if w:
                words.add(w)
    for m in metrics:
        words.add(m.metric_name)
        for w in (m.aliases or []):
            if w:
                words.add(w)
    return {w.strip() for w in words if _MIN_WORD_LEN <= len(w.strip()) <= _MAX_WORD_LEN}


async def load_jieba_userdict() -> int:
    """把全部启用数据源的业务词注册进 jieba。返回注册词数（0 = 降级）。"""
    global _loaded
    if _loaded:
        return 0

    from sqlalchemy import select

    from src.infra.db import AsyncSessionLocal
    from src.nl2sql.models import Nl2sqlColumn, Nl2sqlMetric, Nl2sqlTable

    words: set[str] = set()
    async with AsyncSessionLocal() as db:
        tables = (await db.execute(select(Nl2sqlTable))).scalars().all()
        columns = (await db.execute(select(Nl2sqlColumn))).scalars().all()
        metrics = (await db.execute(select(Nl2sqlMetric))).scalars().all()

    words |= _collect_words(tables, columns, metrics)
    if not words:
        logger.info("[jieba_dict] 元数据为空，跳过用户词典")
        _loaded = True
        return 0

    for word in list(words)[:_MAX_WORDS]:
        # freq 用默认，tag 给 n（名词）：保证能被 extract_keywords 的
        # allowPOS=("n", ...) 白名单保留
        jieba.add_word(word, tag="n")

    _loaded = True
    return min(len(words), _MAX_WORDS)


def reset_loaded_flag() -> None:
    """测试辅助：允许重复加载"""
    global _loaded
    _loaded = False
