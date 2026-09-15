# ============================================================
# 待审队列 → eval/cases/*.json（导出的唯一出口）
#
# 干什么：把 status=approved 的 badcase 渲染成评测器吃的案例文件
#
# ★ 为什么是「导出成文件 + git 提交」而不是让评测器直接读 DB：
#   ① CI 的 test job 没有 postgres service，离线门禁是刻意设计的纯函数；
#   ② 评测集是仓库资产 —— 进了 git，新增/修改了哪几道题才会出现在 PR diff 里；
#   ③ DB 会被清空/回滚/换环境，文件不会。
#
# 用法：
#   python scripts/export_badcase_cases.py                     # 只预览（默认安全）
#   python scripts/export_badcase_cases.py --datasource hospital_demo
#   python scripts/export_badcase_cases.py --write             # 真正落盘
#   python scripts/export_badcase_cases.py --seed-from-export  # 把现有案例灌进表
#
# ★ 先落盘、后标 exported：文件没提交就把状态改成 exported，会造出
#   「已导出但仓库里查无此题」的黑洞。所以 --write 成功后只打印提示，
#   状态标记交给 --mark-exported，由人在 git commit 之后执行。
# ============================================================

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.infra.datasources import get_datasource  # noqa: E402
from src.infra.db import AsyncSessionLocal        # noqa: E402
from src.nl2sql.badcase_export import build_export  # noqa: E402

CASES_DIR = REPO_ROOT / "eval" / "cases"

# 数据源 → 回流案例文件。★ 与 eval/run_nl2sql_eval.py 的
# CASE_FILES_BY_PROJECT 里的 *_reflow 条目必须一致（tests 会锁这一点）。
REFLOW_FILE_BY_DS = {
    "rd_agent": CASES_DIR / "nl2sql_cases_reflow.json",
    "hospital_demo": CASES_DIR / "nl2sql_cases_reflow_hospital.json",
}

# 存量案例文件：seed 时灌进表，标 exported（它们已经在线上了）
SEED_FILES_BY_DS = {
    "rd_agent": CASES_DIR / "nl2sql_cases.json",
    "hospital_demo": CASES_DIR / "nl2sql_cases_hospital.json",
}


def _load_existing(path: Path) -> dict:
    """读已存在的回流文件；读不出来就当空（首次运行）"""
    if not path.exists():
        return {"cases": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"cases": []}


def _merge_existing(path: Path, new_cases: list[dict]) -> tuple[list[dict], list[str]]:
    """与文件里已有的案例按 (question, golden_sql) 合并去重。

    ★ 为什么按内容去重而不是 id：id 是导出时的序号（R001/R002…），
      中间删掉一条就会整体前移 —— 按 id 合并会让所有案例「看起来都变了」，
      diff 噪声淹没真实变化。内容没变就不该出现在 diff 里。
    """
    existing = _load_existing(path)["cases"]
    seen = {(c.get("question"), c.get("golden_sql")) for c in existing}
    merged = list(existing)
    added: list[str] = []
    for c in new_cases:
        key = (c["question"], c["golden_sql"])
        if key in seen:
            continue
        seen.add(key)
        merged.append({k: v for k, v in c.items() if not k.startswith("_")})
        added.append(c["question"][:40])
    # 重新编号，保证 id 稳定可读
    for i, c in enumerate(merged, start=1):
        c["id"] = f"R{i:03d}"
    return merged, added


async def seed_from_export() -> None:
    """把现有 eval/cases/*.json 灌进表并标 exported。

    ★ 让这张表从第一天就是「评测集的真相来源」：90 条存量案例和后续回流
      案例在同一个地方，而不是一半在文件里、一半在 DB 里。
    """
    from src.nl2sql.repositories import BadcaseRepository, STATUS_EXPORTED

    async with AsyncSessionLocal() as db:
        repo = BadcaseRepository(db)
        total = 0
        for code, path in SEED_FILES_BY_DS.items():
            if not path.exists():
                print(f"  跳过 {code}：{path.name} 不存在")
                continue
            ds = await get_datasource(code)
            if ds is None:
                print(f"  跳过 {code}：数据源未注册")
                continue
            cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
            for c in cases:
                case_id, seen = await repo.upsert_badcase(
                    datasource_id=ds.id,
                    datasource_code=ds.code,
                    source="seed",
                    question=c["question"],
                    resolved_question=c["question"],
                    predicted_sql="",
                    error_type="",
                    error_message="",
                    note=f"存量案例种子（{path.name} / {c['id']}）",
                )
                # seed 的行直接标为已审核+已导出，且带上 golden 与分层
                await repo.update_review(
                    case_id,
                    status=STATUS_EXPORTED,
                    golden_sql=c["golden_sql"],
                    category=c["category"],
                    difficulty=c["difficulty"],
                    reviewer="seed",
                )
                total += 1
            print(f"  {code}: 灌入 {len(cases)} 条（{path.name}）")
        print(f"\n共 {total} 条存量案例进表（seen_count>1 表示与已有待审项合并了）")


async def export_one(code: str, write: bool, mark: bool) -> tuple[int, int]:
    ds = await get_datasource(code)
    if ds is None:
        print(f"  ✗ 数据源 {code} 未注册或未启用")
        return 0, 0

    async with AsyncSessionLocal() as db:
        payload = await build_export(db, ds)

    out_path = REFLOW_FILE_BY_DS.get(code)
    if out_path is None:
        print(f"  ✗ {code} 没有配置回流文件（见 REFLOW_FILE_BY_DS）")
        return 0, 0

    if payload["skipped"]:
        # ★ 有脏数据就拒绝导出：一条不过校验的案例会让整个离线门禁变红，
        #   放进去等于把判据本身污染了。
        print(f"  ✗ {code}: {len(payload['skipped'])} 条 approved 案例未过校验，拒绝导出：")
        for s in payload["skipped"][:10]:
            print(f"      id={s['id']} {s['question']!r} —— {s['reason']}")
        return 0, len(payload["skipped"])

    merged, added = _merge_existing(out_path, payload["cases"])
    print(f"  {code}: approved {payload['total']} 条 → 合并后文件共 {len(merged)} 条，"
          f"本次新增 {len(added)} 条")
    for q in added[:10]:
        print(f"      + {q}")

    if not write:
        return len(added), 0

    body = {
        "description": (
            f"{code} 回流评测案例 —— 由 scripts/export_badcase_cases.py 从 "
            f"chatbi_badcases 的 approved 记录生成。请勿手工编辑："
            f"改这里会被下次导出覆盖，请改库里的记录。"
        ),
        "datasource": code,
        "cases": merged,
    }
    out_path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  ✓ 已写入 {out_path.relative_to(REPO_ROOT)}")

    if mark:
        from src.nl2sql.repositories import BadcaseRepository

        async with AsyncSessionLocal() as db:
            n = await BadcaseRepository(db).mark_exported(
                [c["_badcase_id"] for c in payload["cases"]]
            )
        print(f"  ✓ 已将 {n} 条标为 exported")
    else:
        print(f"  ℹ 未标 exported —— 请先 git add/commit，再跑一次加 --mark-exported")
    return len(added), 0


async def main(datasources: list[str], write: bool, mark: bool, seed: bool) -> int:
    if seed:
        await seed_from_export()
        return 0

    total_added = total_bad = 0
    for code in datasources:
        added, bad = await export_one(code, write, mark)
        total_added += added
        total_bad += bad

    print(f"\n新增 {total_added} 条" + ("，未写盘（加 --write）" if not write else ""))
    if total_bad:
        print(f"✗ {total_bad} 条案例未通过校验，导出中止 —— 先修数据再导")
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="把 approved badcase 导出成评测案例文件")
    ap.add_argument("--datasource", default="all",
                    help="数据源编码，默认 all = REFLOW_FILE_BY_DS 里的全部")
    ap.add_argument("--write", action="store_true", help="写入文件（默认只预览）")
    ap.add_argument("--mark-exported", action="store_true",
                    help="写盘后把记录标为 exported（★ 请在 git commit 之后执行）")
    ap.add_argument("--seed-from-export", action="store_true",
                    help="把现有 eval/cases/*.json 灌进表并标 exported（一次性）")
    args = ap.parse_args()

    codes = list(REFLOW_FILE_BY_DS) if args.datasource == "all" else [args.datasource]
    sys.exit(asyncio.run(main(codes, args.write, args.mark_exported,
                              args.seed_from_export)))
