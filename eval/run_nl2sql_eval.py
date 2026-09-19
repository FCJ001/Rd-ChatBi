# ============================================================
# NL2SQL 离线评测运行器
#
# 两种模式：
#   python eval/run_nl2sql_eval.py            # 离线门禁（默认）—— 案例结构
#                                             # 校验 + golden SQL 必须通过安全层
#                                             # （LIMIT 强制覆盖/单语句/SELECT-only），
#                                             # 纯函数，CI 可跑
#   python eval/run_nl2sql_eval.py --live     # 实况 —— LLM 生成 SQL 与 golden_sql
#                                             # 在同一只读连接执行，按结果集等价
#                                             # 判定执行准确率，输出按 category ×
#                                             # difficulty 的分层统计
#
# 任一模式失败都返回非零退出码，作为回归门禁。
# ============================================================

import argparse
import asyncio
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases"
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CASES = CASES_DIR / "nl2sql_cases_auto_full.json"

# 每个数据源的案例文件（案例文件里的 datasource 字段优先）
#
# ★ *_reflow.json 是线上 badcase 回流出来的案例，由
#   scripts/export_badcase_cases.py 从 chatbi_badcases 的 approved 记录生成后
#   提交进仓库 —— 评测器本身仍然是纯函数、零外部依赖（CI 的 test job 没有 PG）。
#   这两个文件必须始终存在（空集也要有 {"cases": []}），否则案例集会在
#   「有没有跑过导出」之间悄悄变化。
CASE_FILES_BY_PROJECT: dict[str, list[Path]] = {
    "hospital_demo": [
        CASES_DIR / "nl2sql_cases_hospital.json",
        CASES_DIR / "nl2sql_cases_reflow_hospital.json",
    ],
    # 汽车全域 127 表压测库（scripts/gen_auto_full.py 生成 schema 与种子数据）
    "auto_full": [
        CASES_DIR / "nl2sql_cases_auto_full.json",
        CASES_DIR / "nl2sql_cases_reflow_auto_full.json",
    ],
}


def _files_for(project: str, override: Path | None) -> list[Path]:
    if override is not None:
        return [override]
    files = CASE_FILES_BY_PROJECT.get(project, [])
    return [f for f in files if f.exists()]


def load_cases(path: Path | None = None) -> list[dict]:
    data = json.loads((path or DEFAULT_CASES).read_text(encoding="utf-8"))
    cases = data["cases"]
    seen = set()
    for c in cases:
        for key in ("id", "category", "difficulty", "question", "golden_sql"):
            assert key in c, f"案例缺字段 {key}: {c.get('id', '?')}"
        assert c["id"] not in seen, f"案例 id 重复: {c['id']}"
        seen.add(c["id"])
    return cases


def load_project_cases(project: str, override: Path | None = None) -> list[dict]:
    """某数据源的全部案例（多文件合并，id 全局唯一）。

    ★ 原实现只读单个文件，导致案例全打在单一数据源上、
      其余数据源零覆盖。现在按数据源聚合。"""
    files = _files_for(project, override)
    if not files:
        raise FileNotFoundError(f"数据源 {project} 没有可用案例文件（找过 {CASE_FILES_BY_PROJECT.get(project, [])}）")

    merged: list[dict] = []
    seen: set[str] = set()
    for f in files:
        for c in load_cases(f):
            if c["id"] in seen:
                raise ValueError(f"案例 id 跨文件重复: {c['id']}（{f.name}）")
            seen.add(c["id"])
            merged.append(c)
    return merged



# ════════════════════════════════════════════════════════════════════════
# 结果集等价判定（exec-match）
# ════════════════════════════════════════════════════════════════════════

_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")


def _normalize_date_text(s: str) -> str:
    """日期/月份文本归一化：`2026-01-01 00:00:00` 与 `2026-01` 视为同一个月。

    ★ 为什么需要：golden 常返回 TIMESTAMP（`2026-01-01 00:00:00+00:00`），
      而模型倾向返回格式化过的 `'2026-01'` —— 两者语义相同但 str() 结果不同，
      会被判成「结果不一致」。实测 AUF22：9 个月的计数**逐个相同**
      （1789/1616/.../924），却因月份是 datetime vs '2026-01' 被判 ✗。
      模型把月份格式化得更规范，反而被判错。

    ★ 只有**带时间部分**的 TIMESTAMP 才缩到月（date_trunc 的产物）。
      纯 `"2026-01-01"` 字符串**不缩** —— 否则「PG 的 DATE 列」（date 对象
      str() 后就是这个形态）与「模型 TO_CHAR 出来的同一天」会不等价，
      那是个更隐蔽的不一致。同理 `2026-01-15` 保留到日。

    ★ 边界（必须有，否则不同月份会混为一谈）：
      年份不同 / 月份不同 / 日不是 01 → 一律不等价。
    """
    s = s.strip().replace("T", " ")
    m = _DATE_PREFIX_RE.match(s)
    if m:
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        has_time = len(s) > m.end()          # 后面还有时分秒
        if d == "01" and has_time:
            return f"{y}-{mo}"                # TIMESTAMP 零点 → 归到月
        return f"{y}-{mo}-{d}"
    m = _YEAR_MONTH_RE.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}"
    return s


def _normalize_value(v, precision: int = 4):
    """统一单元格类型：Decimal/float 数值舍入（聚合口径差异容忍），
    日期时间转字符串并做等价归一化，字符串去首尾空白。"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, Decimal):
        return round(float(v), precision)
    if isinstance(v, float):
        return round(v, precision)
    if isinstance(v, datetime):
        return _normalize_date_text(str(v))   # TIMESTAMP 零点 → 归到月
    if isinstance(v, date):
        return str(v)                          # DATE 列：保持到日（见上方说明）
    if isinstance(v, str):
        return _normalize_date_text(v.strip())
    return v


def _row_signature(row: dict, precision: int = 4) -> tuple:
    """一行的值多重集签名 —— **保留列结构**（不跨列排序抹平）。

    ★ 这是与旧 `normalize_rows` 的关键区别：旧实现把一行内所有值排序成一个
      元组，等于丢掉了"哪个值属于哪一列"。于是 golden `{diff: 0}` 与
      pred `{last: 7728, prev: 7728, diff: 0}` 会被看成「有重叠但不包含」，
      判成 mismatch；而它们其实是**列超集 + 行值子集**的关系（AUF40 实测）。
      保留每列值后，才能正确表达"golden 的 {0} ⊆ pred 的 {7728,7728,0}"。"""
    return tuple(sorted((_normalize_value(v, precision) for v in row.values()), key=repr))


def _row_covers(pred_row: dict, golden_row: dict, precision: int = 4) -> bool:
    """pred 的某一列能"认领"golden 的每一列 = golden 的值多重集 ⊆ pred 的值多重集。

    列名不参与比较（模型的别名与 golden 不同是常态）；pred 多出的列不参与匹配
    （AUF40：模型多给了上月/上月上月的计数，不影响 diff 列的比较）。"""
    need = list(_row_signature(golden_row, precision))
    have = list(_row_signature(pred_row, precision))
    for v in need:
        if v in have:
            have.remove(v)
        else:
            return False
    return True


def _match_rows(golden: list[dict], pred: list[dict], precision: int = 4) -> bool:
    """golden 的每一行能否一对一匹配到 pred 的某一行（行列子集语义）。

    ★ 必须一对一（消耗式匹配），不能用"每行各找一个"的贪心包含 ——
      那会让 pred 的一行被 golden 的多行重复认领，等于放宽成"存在即通过"。
      行数很少（通常 ≤ 100），带回溯的精确匹配成本可以忽略。"""
    if len(golden) != len(pred):
        return False
    used = [False] * len(pred)

    def _backtrack(gi: int) -> bool:
        if gi == len(golden):
            return True
        for pi in range(len(pred)):
            if used[pi] or not _row_covers(pred[pi], golden[gi], precision):
                continue
            used[pi] = True
            if _backtrack(gi + 1):
                return True
            used[pi] = False
        return False

    return _backtrack(0)


# 判定结果三态。★ 为什么需要 AMBIGUOUS：
#   exec-match 的前提是「这个问题有唯一正确答案（做多重集比较时至少唯一到一个
#   多重集）」。当 golden 带 LIMIT N、而满足条件的候选行 ⊋ N 行时，这个前提不成立 ——
#   「最近 10 条致命缺陷」有 4000 行候选、最新时间戳上并列 11 行，哪 10 条是任意的。
#   此时把不等价判成 ✗ 会把「判分器问了一个没有唯一答案的问题」记成「模型答错了」，
#   分数系统性偏低且不可归因（实测 auto_full 明细类 0/6 全折在这上面）。
#   AMBIGUOUS 单列出来，既不计入通过也不计入失败，让分母诚实。
EXACT = "exact"          # 一对一匹配成功 → 通过
AMBIGUOUS = "ambiguous"  # 行数不同但一侧逐行被另一侧包住 → 典型的 LIMIT 截断差异，不计分
MISMATCH = "mismatch"    # 其余 → 失败


def classify_results(golden_rows: list[dict], pred_rows: list[dict],
                     precision: int = 4) -> str:
    """exec-match 三态判定。返回 EXACT / AMBIGUOUS / MISMATCH。

    ★ 行数不同且一侧逐行被另一侧包住时才判 AMBIGUOUS —— 那正是 LIMIT 截断
      点不同的特征。不做无条件的"行数不同就放过"：一条漏了 WHERE 的 SQL
      返回全表，必然包住 golden 的全部行，那必须判错。

    ★ 沿用子集判据做 AMBIGUOUS 判定是有意的（AUF28 靠它翻正）：两侧都打到
      上限才说明"双方在回答同一个被截断的问题"。误判面由 offline gate 的
      「带 LIMIT 无 ORDER BY」lint 负责点名。"""
    if _match_rows(golden_rows, pred_rows, precision):
        return EXACT

    # 行数不同：看是否一侧的行能全部包住另一侧（LIMIT 截断特征）
    if len(golden_rows) != len(pred_rows) and golden_rows and pred_rows:
        smaller, larger = ((golden_rows, pred_rows) if len(golden_rows) < len(pred_rows)
                           else (pred_rows, golden_rows))
        used = [False] * len(larger)
        all_covered = True
        for row in smaller:
            hit = next((i for i, cand in enumerate(larger)
                        if not used[i] and _row_covers(cand, row, precision)), None)
            if hit is None:
                all_covered = False
                break
            used[hit] = True
        if all_covered:
            return AMBIGUOUS
    return MISMATCH


def compare_results(golden_rows: list[dict], pred_rows: list[dict], precision: int = 4) -> bool:
    """执行准确率判定（布尔版，供单测与旧调用点用）。"""
    return _match_rows(golden_rows, pred_rows, precision)


# ════════════════════════════════════════════════════════════════════════
# 模式一：离线门禁（纯函数）
# ════════════════════════════════════════════════════════════════════════

def run_offline_gate(path: Path | None = None) -> tuple[int, int, list[str]]:
    """校验案例结构 + golden SQL 必须能通过安全层。

    golden 被安全层拒绝只有两种可能：案例本身写错，或安全层误伤合法查询
    —— 两者都是必须拦在 CI 里的回归。

    ★ 覆盖全部已注册数据源的案例文件（不传 path 时）；传 path 则只跑该文件。"""
    from src.nl2sql.security import validate_sql

    if path is not None:
        groups = [(path.stem, load_cases(path))]
    else:
        groups = [
            (project, load_project_cases(project))
            for project in CASE_FILES_BY_PROJECT
            if _files_for(project, None)
        ]

    passed, failed, failures = 0, 0, []
    lint: list[str] = []
    valid_levels = {"easy", "medium", "hard"}

    for project, cases in groups:
        print(f"\n[{project}] {len(cases)} 条案例")
        for c in cases:
            ok, result = validate_sql(c["golden_sql"])
            if not ok:
                failed += 1
                failures.append(f"{project}/{c['id']}: golden_sql 未通过安全层 —— {result}")
                continue
            # 分层维度合法性：评测要按 category × difficulty 出统计，
            # 写错分层等于没有度量
            if c["difficulty"] not in valid_levels:
                failed += 1
                failures.append(
                    f"{project}/{c['id']}: difficulty 必须是 {sorted(valid_levels)}，"
                    f"实际 {c['difficulty']}"
                )
                continue
            passed += 1
            lint.extend(_lint_case_quality(project, c, result))
            print(f"  ✓ {c['id']} [{c['category']}/{c['difficulty']}]")

    if lint:
        # ★ 分两类输出：漂移时间有 23 条（多为「近30天」这类**相对时间本就是
        #   题目语义**的案例，可能恒空也可能不空），混在一起会把「带 LIMIT 无
        #   ORDER BY」这种 4 条的强信号淹掉。强信号在前。
        hard = [w for w in lint if "无 ORDER BY" in w]
        soft = [w for w in lint if w not in hard]
        if hard:
            print(f"\n⚠ 判分失真（{len(hard)} 条，不阻断 CI）:")
            for w in hard:
                print(f"  - {w}")
        if soft:
            print(f"\n· 漂移时间提示（{len(soft)} 条，**筛查线索不是判决**）：golden 用了 "
                  f"now()/date_trunc 等随当前时间漂移的表达式。种子数据有时间上界，"
                  f"窗口贴着/越过上界时才会恒空（实测 auto_full 8 条里只有 3 条真恒空 —— "
                  f"AUF24/40/43；其余窗口在上界之前，有数据）。")
            print(f"  判决方法：跑一次 live，或对 golden 执行看是否为空/全 0。")
            by_proj: dict[str, list[str]] = {}
            for w in soft:
                proj, rest = w.split("/", 1)
                by_proj.setdefault(proj.lstrip("  - "), []).append(rest.split(":")[0])
            for proj, ids in sorted(by_proj.items()):
                print(f"    {proj}: {len(ids)} 条 —— {' '.join(ids[:12])}"
                      + (" …" if len(ids) > 12 else ""))

    return passed, failed, failures


# 带 LIMIT 的 golden 的形态识别（lint 用；AST 已被 validate_sql 重写过，
# 这里看的是原始文本，因为要判断的是"作者怎么写的"，不是"安全层放行成什么")
_GOLDEN_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)
_GOLDEN_ORDER_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)

# ★ golden 里出现「随当前时间漂移」的表达式 = 这条案例的答案会随时间变化。
#   种子数据的时间上界停在生成脚本跑的那天，而 now() 永远在它之后 ——
#   凡是"今天/昨天/最近N天"的窗口，跑起来必然为空。
#   实测：AUF24「昨天的产线安灯事件有多少次」恒返回 0 却一直判通过；
#   COUNT(*) 形态更隐蔽 —— 它永远返回 1 行（值为 0），行数检测抓不到。
# ★ 正则的边界要放在每个分支各自该在的位置：
#   `\b(?:...|now\s*\(|...)\b` 这种写法会让 `now(` 变成死分支 ——
#   末尾 \b 要求 `(` 与下一字符之间有词边界，而 `)`/空格都不是单词字符，
#   于是 `now()` 永远匹配不上（实测踩过，靠 date_trunc 才蒙对了 7 条告警）。
_DRIFTING_TIME_RE = re.compile(
    r"(?:\bnow\s*\(|\b(?:current_date|current_timestamp|current_time|localtime)\b"
    r"|\bdate_trunc\s*\()",
    re.IGNORECASE)


def _lint_case_quality(project: str, c: dict, validated_sql: str) -> list[str]:
    """案例质量 lint：找出会让 exec-match 问出"没有唯一答案"的 golden。

    ★ 为什么这属于门禁而不是可选项：判分器的正确性依赖案例的正确性。
      golden 带 LIMIT N 却没排序，等于说"这 N 行是任意的"——模型写出一条
      语义完全正确的 SQL，也会因为取到另一个合法子集被判 ✗。分数会系统性
      偏低，且偏低的部分无法归因到系统能力上（实测 auto_full 明细类全折在这）。
      这类案例让门禁数字失去意义，所以要在离线门禁里显式点名。

    为什么是 lint 不是失败：这是案例质量问题，不是安全回归。让它阻断 CI 会
    把不相关的改动也卡住；但也不能沉默——沉默正是它藏了这么久的原因。"""
    sql = c["golden_sql"]
    warnings_out: list[str] = []
    if _DRIFTING_TIME_RE.search(sql):
        warnings_out.append(
            f"{project}/{c['id']}: golden 用了随当前时间漂移的表达式（now/date_trunc 等）"
            f" —— 种子数据有固定时间上界，这类案例可能恒返回空/"
            f"COUNT=0（判分上是 0=0 恒过，但什么都没验证）"
        )
    # ★ 两个检查必须各自独立判定，不能一个 early-return 掉另一个：
    #   同一条 golden 完全可能既漂移又无 ORDER BY，漏报一个就少一条线索。
    if _GOLDEN_LIMIT_RE.search(sql) and not _GOLDEN_ORDER_RE.search(sql):
        # 有排序仍需人工确认排序列是否唯一（如 ORDER BY created_at 会撞秒）。
        # 静态判不出来，只在 live 跑出 AMBIGUOUS 时由判分器点名。
        warnings_out.append(
            f"{project}/{c['id']}: golden 带 LIMIT 但无 ORDER BY —— "
            f"取哪几行是任意的，exec-match 会拿两个合法子集互比（该类题不计分）"
        )
    return warnings_out


# ════════════════════════════════════════════════════════════════════════
# 模式二：实况执行准确率（需要 LLM + 业务库）
# ════════════════════════════════════════════════════════════════════════

async def run_live_project(project: str, cases: list[dict],
                           fetch_examples: bool = True) -> tuple[float, int, int, dict[str, list[bool]]]:
    """跑一个数据源的全部案例，返回 (执行准确率, 通过数, 总数, 各类别通过情况)"""
    from src.api.deps import get_embedding_model, get_llm
    from src.infra.datasources import dw_session_factory, get_datasource
    from src.infra.milvus_client import get_milvus_client
    from src.nl2sql.engine import build_schema_prompt, run_query, setup_readonly_session
    from src.nl2sql.example_store import MilvusExampleRepository, find_similar_examples
    from src.nl2sql.repositories import PgMetaRepository
    from src.infra.db import AsyncSessionLocal
    from sqlalchemy import text

    ds = await get_datasource(project)
    if ds is None:
        print(f"数据源 {project} 未注册或未启用")
        return 0.0, 0, len(cases), {}

    # 动态 schema（与线上 /query 相同的构建路径）
    meta_db = AsyncSessionLocal()
    try:
        repo = PgMetaRepository(meta_db, ds.id)
        tables = await repo.get_all_tables()
        for t in tables:
            t.columns = await repo.get_columns_by_table(t.id)
        schema = build_schema_prompt(tables)
    finally:
        await meta_db.close()

    llm = get_llm()
    # P1 主线 B：few-shot 示例库（未建库 → 每题空列表，行为与改造前一致）。
    # --disable-examples 用于消融：跳过示例检索，测纯模型能力（锚点仍生效）。
    example_repo = MilvusExampleRepository(get_milvus_client(), prefix=ds.milvus_prefix)
    embedding_model = get_embedding_model()
    if not fetch_examples:
        print("  [ablation] 示例检索已禁用（--disable-examples）")
    stats: dict[str, list[bool]] = defaultdict(list)
    latencies: list[float] = []
    ambiguous: list[str] = []  # 判分器无唯一答案的题，不计入分子也不计入分母

    factory = dw_session_factory(project)
    async with factory() as db:
        for c in cases:
            cid = c["id"]
            t0 = asyncio.get_event_loop().time()
            examples = (await find_similar_examples(example_repo, embedding_model, c["question"])
                        if fetch_examples else [])
            result = await run_query(
                question=c["question"], llm=llm, db=db, role="admin",
                role_rules=ds.role_rules, params={}, schema=schema,
                source_name=ds.name, sensitive_columns=ds.sensitive_columns,
                examples=examples,
            )
            latency = asyncio.get_event_loop().time() - t0
            latencies.append(latency)

            errors = []
            if not result.success:
                errors.append(f"NL2SQL 失败: {result.error[:120]}")
            else:
                try:
                    await setup_readonly_session(db)
                    golden_rows = (await db.execute(text(c["golden_sql"]))).mappings().all()
                except Exception as e:
                    # 逐条 rollback：一条失败会把事务打成 aborted，
                    # 后续每条都报 InFailedSQLTransactionError 而不是真原因
                    await db.rollback()
                    errors.append(f"golden 执行失败: {type(e).__name__}: {str(e)[:120]}")
                else:
                    verdict = classify_results([dict(r) for r in golden_rows], result.data)
                    if verdict == AMBIGUOUS:
                        # 判分器无法给唯一答案：不计通过也不计失败，单列出来
                        ambiguous.append(cid)
                        print(f"  ? {cid} [{key}] {c['question'][:30]}  ({latency:.1f}s)")
                        print(f"      - 结果集为真子集关系（golden={len(golden_rows)}行 "
                              f"pred={result.row_count}行），疑为 LIMIT 截断点差异，"
                              f"判分器无唯一答案 —— 不计分")
                    elif verdict == MISMATCH:
                        errors.append(
                            f"结果不一致 golden={len(golden_rows)}行 "
                            f"pred={result.row_count}行 sql={result.sql[:120]}"
                        )

            ok = not errors
            key = f"{c['category']}/{c['difficulty']}"
            stats[c["category"]].append(ok)
            stats[c["difficulty"]].append(ok)
            stats[key].append(ok)
            stats["_overall"].append(ok)
            mark = "✓" if ok else "✗"
            print(f"  {mark} {cid} [{key}] {c['question'][:30]}  ({latency:.1f}s)")
            for e in errors:
                print(f"      - {e}")

    print("\n执行准确率（分层）：")
    for layer in ("_overall", "单表聚合", "分组统计", "排序TopN", "时间窗口", "多表JOIN", "明细查询",
                  "easy", "medium", "hard"):
        if layer not in stats:
            continue
        rs = stats[layer]
        name = "总体" if layer == "_overall" else layer
        print(f"  {name}: {sum(rs)}/{len(rs)} = {sum(rs) / len(rs):.0%}")
    avg = sum(latencies) / len(latencies) if latencies else 0
    print(f"  平均耗时: {avg:.1f}s")
    if ambiguous:
        print(f"\n  ⚠ 判分器无唯一答案（不计分，{len(ambiguous)} 题）: {' '.join(ambiguous)}")
        print("    成因：golden 带 LIMIT N 但候选行 ⊋ N 行，且无唯一 tie-breaker。")
        print("    修法：给 golden 的 ORDER BY 补唯一列（如 id）作 tie-breaker。")

    overall = stats["_overall"]
    acc = sum(overall) / len(overall) if overall else 0.0
    # 只回传「类别」维度给分层门禁。
    # ★ 不能用 `"/" not in k` 来排除难度键 —— easy/medium/hard 本身不含 "/"，
    #   会被当成类别混进去，然后按 100% 门限报一堆假失败（实测踩过）。
    #   必须用「是不是合法案例类别」这个正面判据。
    known_categories = _known_categories()
    by_category = {k: v for k, v in stats.items() if k in known_categories}
    return acc, sum(overall), len(overall), by_category


@lru_cache(maxsize=1)
def _known_categories() -> frozenset[str]:
    """从案例文件里读出真实存在的 category 集合（单一来源，不硬编码）。

    ★ 难度维度（easy/medium/hard）不在其中 —— 它们不设分层门限，
      因为难度是"题有多难"的描述，不是"功能是否可用"的判据。"""
    cats: set[str] = set()
    for f in CASES_DIR.glob("nl2sql_cases*.json"):
        try:
            for c in json.loads(f.read_text(encoding="utf-8")).get("cases", []):
                if c.get("category"):
                    cats.add(c["category"])
        except Exception:  # 案例文件坏了不该让评测崩在这
            continue
    return frozenset(cats)


# ════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════
# 分层门限：单一总分门限会把「某一类完全不可用」平均掉
#
# ★ 立这条的实测依据：auto_full 41 题总体 66%，看着「还行」；拆开是
#   单表聚合 100% / 分组统计 100% / 时间窗口 89% / 多表JOIN 57% /
#   排序TopN 33% / **明细查询 0%**。只看总分，0% 那一类等于没被看见。
#
# 每个 (数据源, 类别) 声明三个数：
#   expect  —— 该类别**满分为多少**（默认 1.0 不放宽）
#   min_n   —— 低于这个题数不出结论（小样本会抖：3 题错 1 题就是 -33pp）
#   reason  —— 为什么放宽。写明依据，避免变成「调低门限让 CI 变绿」
# ════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LayerGate:
    expect: float = 1.0
    min_n: int = 3
    reason: str = ""


# 数据源 × 类别 → 门限。未声明的走 _DEFAULT_LAYER_GATE。
#
# ★ 2026-09-19 首版全部按 expect=1.0 立：先把「哪一类漏水」变成 CI 可见的
#   事实，**不预先放宽**。实测基线（auto_full，LLM 欠费前那次）已低于门限的
#   类别是真实待修项，不是门限设错 —— 等修完再决定要不要写 reason 放宽。
#
# ★ 用「(数据源, 类别)」而不是「类别」作键：同一个类别在不同数据源上难度不同
#   （auto_full 是 127 张表的压测库，hospital_demo 只有几张表），全局键会把
#   两者的差异抹平，逼着把门限调到迁就最差的那个。
_LAYER_GATES: dict[tuple[str, str], LayerGate] = {}

_DEFAULT_LAYER_GATE = LayerGate()


def get_layer_gate(project: str, category: str) -> LayerGate:
    return _LAYER_GATES.get((project, category)) or _DEFAULT_LAYER_GATE


def check_layer_gates(project: str, stats: dict[str, list[bool]]) -> list[str]:
    """返回未达标的类别描述（空列表 = 全过）。

    ★ 只统计真实存在的「类别」，**难度维度（easy/medium/hard）不设门限** ——
      难度是「题有多难」的描述，不是「功能是否可用」的判据；把它当类别会按
      100% 门限报一堆假失败。这里用正面白名单（案例文件里出现过的 category）
      过滤，而不是靠排除法 —— 排除法漏过一次：`"/" not in k` 挡不住英文
      难度键（它们本身不含 "/"），实测把 easy/medium/hard 全报成未达标。

    ★ 上层 run_live_project 已过滤一次；这里再过滤一次是**故意的防御** ——
      本函数是公开的，任何人拿原始 stats 直接调用都会踩同一个坑。"""
    known = _known_categories()
    failures: list[str] = []
    for category, results in sorted(stats.items()):
        if category not in known:
            continue
        gate = get_layer_gate(project, category)
        if len(results) < gate.min_n:
            continue
        acc = sum(results) / len(results)
        if acc < gate.expect:
            detail = f"（依据：{gate.reason}）" if gate.reason else ""
            failures.append(
                f"{project}/{category}: {sum(results)}/{len(results)} = {acc:.0%} "
                f"< 分层门限 {gate.expect:.0%}{detail}"
            )
    return failures


# ════════════════════════════════════════════════════════════════════════

async def run_live(project: str, threshold: float, path: Path | None,
                   fetch_examples: bool = True) -> bool:
    """实况执行准确率。project="all" 时跑全部有案例文件的数据源。"""
    projects = list(CASE_FILES_BY_PROJECT) if project == "all" else [project]

    # acc / 通过数 / 总数 / 各类型通过情况
    results: dict[str, tuple[float, int, int, dict[str, list[bool]]]] = {}
    for p in projects:
        if not _files_for(p, path):
            print(f"数据源 {p} 没有案例文件，跳过")
            continue
        try:
            cases = load_project_cases(p, path)
        except (FileNotFoundError, ValueError) as e:
            print(f"数据源 {p} 案例加载失败: {e}")
            return False

        print(f"\n{'=' * 62}\n数据源 {p}（{len(cases)} 条案例）\n{'=' * 62}")
        results[p] = await run_live_project(p, cases, fetch_examples=fetch_examples)

    if not results:
        print("没有任何数据源可评测")
        return False

    print(f"\n{'=' * 62}\n汇总\n{'=' * 62}")
    total_ok = total_n = 0
    all_pass = True
    layer_failures: list[str] = []
    for p, (acc, ok, n, cat_stats) in results.items():
        total_ok += ok
        total_n += n
        flag = "✓" if acc >= threshold else "✗"
        if acc < threshold:
            all_pass = False
        print(f"  {flag} {p}: {ok}/{n} = {acc:.0%}（门限 {threshold:.0%}）")
        layer_failures.extend(check_layer_gates(p, cat_stats))
    if total_n:
        print(f"  总体: {total_ok}/{total_n} = {total_ok / total_n:.0%}")

    # ★ 分层门禁：总分过了也可能挂在这里 —— 这正是它存在的意义
    if layer_failures:
        all_pass = False
        print(f"\n✗ 分层门限未达标（总分合格不代表各类都可用）：")
        for f in layer_failures:
            print(f"    {f}")
    return all_pass


# ════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NL2SQL 评测运行器")
    parser.add_argument("--live", action="store_true", help="跑实况执行准确率（需 LLM + 业务库）")
    parser.add_argument("--project", default="all",
                        help="数据源编码（bi_datasources.code），默认 all = 全部有案例的数据源")
    parser.add_argument("--threshold", type=float, default=0.8, help="live 模式准确率门限")
    parser.add_argument("--cases", type=Path, default=None, help="自定义案例文件（覆盖数据源默认文件）")
    parser.add_argument("--disable-examples", action="store_true",
                        help="消融：跳过 few-shot 示例检索，测纯模型能力（时间锚点仍生效）")
    args = parser.parse_args()

    if not args.live:
        print("离线门禁（案例结构 + golden SQL 安全校验，无外部依赖）：")
        passed, failed, failures = run_offline_gate(args.cases)
        print(f"\n通过 {passed} / {passed + failed}")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1 if failed else 0)

    ok = asyncio.run(run_live(args.project, args.threshold, args.cases,
                              fetch_examples=not args.disable_examples))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
