# ============================================================
# auto_full 数据源生成器 — 消费 auto_full_domain.py 的结构化 spec：
#
#   1. DDL    ：CREATE TABLE + COMMENT + FK + 索引（127 张表）
#   2. 种子   ：INSERT ... SELECT generate_series 确定性灌数（~180 万行，秒级）
#   3. 元数据 ：conf/projects/auto_full.yaml（供 build_nl2sql_meta.py 构建
#              PG/Milvus/ES 三层；敏感列物理存在但元数据不定义）
#
# 用法：
#   python scripts/gen_auto_full.py --write-yaml          # 只写 YAML
#   python scripts/gen_auto_full.py --execute             # 建库 + DDL + 灌数
#   python scripts/gen_auto_full.py --sql-out /tmp/a.sql  # 导出 SQL 供 review
#   python scripts/gen_auto_full.py                       # = --execute + --write-yaml
# ============================================================

import argparse
import hashlib
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.auto_full_domain import metrics, tables  # noqa: E402

DB_NAME = "auto_full"
YAML_PATH = Path(__file__).resolve().parents[1] / "conf" / "projects" / "auto_full.yaml"
# ★ 不再硬编码端口：本项目 compose 自带 PG 跑在宿主机 15432（默认 5432 被
#   rd-agent-platform 占着）。写死 5432 会让脚本连到**兄弟项目的库**上去 ——
#   实测踩过：重建时报 Errno 61，查了半天才发现连错了实例。
def _pg_dsn() -> str:
    from src.core.config import get_settings
    s = get_settings()
    return (f"postgresql://{s.DB_USER}:{s.DB_PASSWORD}"
            f"@{s.DB_HOST}:{s.DB_PORT}")


PG_DSN = _pg_dsn()

DATASOURCE_YAML = """\
# ============================================================
# 数据源：auto_full — 汽车全域业务库（由 scripts/gen_auto_full.py 生成，勿手改）
#
# 127 张表 / 13 子域（研发ALM/零部件BOM/采购/整车主数据/生产制造/QMS质量/
# 销售/售后/召回/车联网OTA/三电电池/智驾测试/财务），~180 万行确定性种子数据。
# 用途：百表级 schema 召回压测 —— 表多且语义相近（问题/缺陷/故障/投诉/召回），
# 逼 recall_columns / filter_tables 真正做 discrimination。
#
# ★ vin / cust_name / cust_phone / cust_id_number 物理存在但元数据故意不定义
#   （Prompt 层 + 执行层双防线，对齐 rd_agent 的设计）
# ★ plant_id / region_id 是行级权限列：engineer 按 plant_id、sales 按 region_id 隔离
# ============================================================

datasource:
  code: auto_full
  name: 汽车全域业务库
  description: 汽车全价值链 demo 库（研发/供应链/制造/质量/销售/售后/召回/车联网/三电/智驾/财务）
  dsn: {dsn}
  milvus_prefix: auto_full
  es_prefix: auto_full
  sensitive_columns: [vin, cust_name, cust_phone, cust_id_number]
  role_rules:
    default: deny
    roles:
      admin: all
      engineer:
        column: plant_id
        param: plant_id
      sales:
        column: region_id
        param: region_id
      customer: deny
"""


def _rows() -> dict[str, int]:
    return {t.name: t.rows for t in tables()}


def _sub(expr: str, rows: dict[str, int], salt: int = 0) -> str:
    """{i} → 行号变量 i；@表名@ → 该表行数（外键取模）；{salt} → 该列的散列盐。

    ★ salt 的作用：所有列若共用同一个散列，数值列之间会**完美相关**
      （实测 corr(工时,里程)=1.000）—— 里程最高的车必然也是工时最长的车。
      真实数据是带噪声的相关。按列名派生一个稳定的盐，让每列走不同的散列流。
    """
    out = expr.replace("{i}", "i").replace("{salt}", str(salt))
    for name, n in rows.items():
        out = out.replace(f"@{name}@", str(n))
    return out


def _col_salt(table: str, col: str) -> int:
    """列名 → 稳定的散列盐（同一列每次生成结果一致，保证可复现）"""
    return int(hashlib.sha256(f"{table}.{col}".encode()).hexdigest()[:6], 16)


def _nullable(c) -> bool:
    return "CASE WHEN" in c.gen


# ── DDL ────────────────────────────────────────────────────
def emit_ddl() -> list[str]:
    stmts: list[str] = []
    for t in _topo_order():  # 拓扑序：被引用的表先建（FK 约束要求引用表已存在）
        defs = []
        for c in t.cols:
            d = f"{c.name} {c.type}"
            if c.role == "primary_key":
                d += " NOT NULL"
            elif c.role == "foreign_key" and not _nullable(c):
                d += " NOT NULL"
            defs.append(d)
        defs.append("PRIMARY KEY (id)")
        for c in t.cols:
            if c.role == "foreign_key":
                ref = c.enum_source.split(".")[0]
                defs.append(f"FOREIGN KEY ({c.name}) REFERENCES {ref}(id)")
        stmts.append(f"CREATE TABLE {t.name} (\n  " + ",\n  ".join(defs) + "\n)")
        stmts.append(f"COMMENT ON TABLE {t.name} IS '{t.desc}'")
        for c in t.cols:
            if c.desc:
                stmts.append(f"COMMENT ON COLUMN {t.name}.{c.name} IS '{c.desc.replace(chr(39), chr(39) * 2)}'")
        # 大事实表：FK + 首个日期列建索引（JOIN/时间过滤加速）
        if t.rows >= 10000:
            for c in t.cols:
                if c.role == "foreign_key":
                    stmts.append(f"CREATE INDEX idx_{t.name}_{c.name} ON {t.name}({c.name})")
            for c in t.cols:
                if c.role == "date":
                    stmts.append(f"CREATE INDEX idx_{t.name}_{c.name} ON {t.name}({c.name})")
                    break
    return stmts


# ── 种子数据（拓扑序：被引用的维表先插）────────────────────
def _topo_order() -> list:
    by_name = {t.name: t for t in tables()}
    done: set[str] = set()
    ordered = []
    pending = list(tables())
    while pending:
        progressed = False
        rest = []
        for t in pending:
            refs = {c.enum_source.split(".")[0] for c in t.cols if c.role == "foreign_key"}
            if refs <= done:
                ordered.append(t)
                done.add(t.name)
                progressed = True
            else:
                rest.append(t)
        pending = rest
        if not progressed:
            names = [t.name for t in pending]
            raise RuntimeError(f"表间存在循环外键依赖: {names}（引用 {by_name and ''}未解析）")
    return ordered


def emit_seed() -> list[str]:
    rows = _rows()
    stmts = []
    for t in _topo_order():
        cols, exprs = [], []
        for c in t.cols:
            cols.append(c.name)
            exprs.append(_sub(c.gen, rows, _col_salt(t.name, c.name)))
        stmts.append(
            f"INSERT INTO {t.name} ({', '.join(cols)})\n"
            f"SELECT {', '.join(exprs)} FROM generate_series(1, {t.rows}) AS i"
        )
    stmts.append("ANALYZE")
    return stmts


# ── YAML ───────────────────────────────────────────────────
def emit_yaml() -> str:
    # dsn 由当前配置生成（含宿主机端口），不写死在模板里
    from src.core.config import get_settings
    _s = get_settings()
    dsn = (f"postgresql+asyncpg://{_s.DB_USER}:{_s.DB_PASSWORD}"
           f"@{_s.DB_HOST}:{_s.DB_PORT}/{DB_NAME}")
    lines = [DATASOURCE_YAML.format(dsn=dsn), "tables:"]

    def col_block(c, indent) -> list[str]:
        out = [
            f"{indent}- name: {c.name}",
            f"{indent}  type: {c.type}",
            f"{indent}  role: {c.role}",
            f"{indent}  description: \"{c.desc}\"",
        ]
        if c.alias:
            alias = ", ".join(f"\"{a}\"" for a in c.alias)
            out.append(f"{indent}  alias: [{alias}]")
        if c.sync:
            out.append(f"{indent}  sync: true")
        if c.enum_source and c.role == "foreign_key":
            out.append(f"{indent}  enum_source: {c.enum_source}")
        return out

    for t in tables():
        lines += [f"  - name: {t.name}", f"    role: {t.role}", f"    description: {t.desc}", "    columns:"]
        for c in t.cols:
            if c.sensitive:
                continue  # 敏感列：物理存在，元数据不定义
            lines += col_block(c, "      ")
    lines += ["", "metrics:"]
    for m in metrics():
        alias = ", ".join(f"\"{a}\"" for a in m["alias"])
        lines += [
            f"  - name: {m['name']}",
            f"    description: {m['description']}",
            f"    relevant_columns: [{', '.join(f'\"{rc}\"' for rc in m['relevant_columns'])}]",
            f"    alias: [{alias}]",
            "",
        ]
    return "\n".join(lines)


# ── 执行 ───────────────────────────────────────────────────
async def execute(ddl: list[str], seed: list[str]):
    import asyncpg

    admin = await asyncpg.connect(f"{PG_DSN}/rd_chatbi")
    try:
        await admin.execute(f'CREATE DATABASE {DB_NAME}')
        print(f"[db] 已创建数据库 {DB_NAME}")
    except asyncpg.DuplicateDatabaseError:
        print(f"[db] 数据库 {DB_NAME} 已存在，复用")
    finally:
        await admin.close()

    conn = await asyncpg.connect(f"{PG_DSN}/{DB_NAME}")
    try:
        exists = await conn.fetchval("SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'")
        if exists:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            print("[ddl] 库中已有表，已重建 schema（demo 库可随时重建）")
        print(f"[ddl] 建表 {len(ddl)} 条语句 …")
        for s in ddl:
            await conn.execute(s)
        total_rows = 0
        print("[seed] 灌数 …")
        for s in seed:
            if s == "ANALYZE":
                await conn.execute("ANALYZE")
                continue
            n = await conn.fetchval(f"WITH ins AS ({s} RETURNING 1) SELECT COUNT(*) FROM ins")
            total_rows += n or 0
            if n and n >= 10000:
                print(f"       {n:>8} rows ← {s.splitlines()[0][:60]}")
        print(f"[seed] 完成，共 {total_rows} 行")
    finally:
        await conn.close()


def main():
    ap = argparse.ArgumentParser(description="auto_full 数据源生成器（DDL + 种子 + YAML）")
    ap.add_argument("--execute", action="store_true", help="建库并执行 DDL + 灌数")
    ap.add_argument("--write-yaml", action="store_true", help="写 conf/projects/auto_full.yaml")
    ap.add_argument("--sql-out", help="把 DDL+种子导出成 SQL 文件（review 用）")
    args = ap.parse_args()

    ddl, seed = emit_ddl(), emit_seed()
    n_cols = sum(len(t.cols) for t in tables())
    n_fk = sum(1 for t in tables() for c in t.cols if c.role == "foreign_key")
    n_sync = sum(1 for t in tables() for c in t.cols if c.sync)
    print(f"[spec] {len(tables())} 张表 / {n_cols} 列 / {n_fk} 个外键 / {n_sync} 个 sync 枚举列 / {len(metrics())} 个指标")

    if args.sql_out:
        Path(args.sql_out).write_text(";\n".join(ddl + seed) + ";\n", encoding="utf-8")
        print(f"[sql] 已导出 {args.sql_out}")
    if args.write_yaml:
        YAML_PATH.write_text(emit_yaml(), encoding="utf-8")
        print(f"[yaml] 已写入 {YAML_PATH}")
    if args.execute or (not args.write_yaml and not args.sql_out):
        asyncio.run(execute(ddl, seed))


if __name__ == "__main__":
    main()
