# ============================================================
# demo 数据初始化 — 创建 chatbi_demo 医院运营库 + 种子数据
#
# 模拟一年的门诊挂号/住院记录，供 NL2SQL 查询演示。
#
# 用法：
#   python scripts/init_demo_data.py              # 建库建表 + 种子数据（幂等）
#   python scripts/init_demo_data.py --drop       # 先删表再重建
#
# ★ 敏感字段（patient_name/patient_phone/id_card）仅入 demo 库，
#   不进 NL2SQL 元数据（Prompt 层防线，见 conf/nl2sql_meta.yaml）
# ============================================================

import argparse
import asyncio
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg

from src.core.config import get_settings

# ── 科室维表 ────────────────────────────────────────────────────

DEPARTMENTS = [
    # (name, category, building, floor)
    ("内科", "内科系统", "门诊楼", 2),
    ("外科", "外科系统", "门诊楼", 3),
    ("儿科", "内科系统", "门诊楼", 1),
    ("妇产科", "专科", "门诊楼", 2),
    ("骨科", "外科系统", "门诊楼", 3),
    ("耳鼻喉科", "专科", "门诊楼", 4),
    ("口腔科", "专科", "门诊楼", 4),
    ("眼科", "专科", "门诊楼", 4),
    ("皮肤科", "专科", "门诊楼", 5),
    ("中医科", "内科系统", "门诊楼", 5),
]

# 各科室门诊量基准权重（内科/儿科最热，模拟图片示例 TOP10 分布）
DEPT_WEIGHTS = [12, 9, 9, 7, 7, 5, 5, 4, 3, 2]

VISIT_TYPES = ["初诊", "复诊", "急诊"]
VISIT_TYPE_WEIGHTS = [0.45, 0.35, 0.20]
DOCTORS = ["王建国", "李明华", "张淑芬", "陈志远", "刘洋", "赵红梅", "孙国庆", "周丽娟"]
STATUSES = ["paid", "unpaid", "cancelled"]
STATUS_WEIGHTS = [0.8, 0.1, 0.1]
REG_FEES = [10.0, 15.0, 20.0, 30.0, 50.0, 100.0]

INPATIENT_STATUSES = ["discharged", "settled", "in_treatment"]
INPATIENT_STATUS_WEIGHTS = [0.6, 0.3, 0.1]


async def ensure_database(server: asyncpg.connect, dbname: str) -> None:
    """demo 库不存在则创建"""
    exists = await server.fetchval(
        "SELECT 1 FROM pg_database WHERE datname = $1", dbname
    )
    if not exists:
        print(f"创建数据库 {dbname} ...")
        await server.execute(f'CREATE DATABASE "{dbname}"')
    else:
        print(f"数据库 {dbname} 已存在")


async def main(drop: bool = False):
    settings = get_settings()

    # 连接维护库（连 postgres 库执行 CREATE DATABASE）
    server = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database="postgres",
    )
    await ensure_database(server, settings.DEMO_DB_NAME)
    await server.close()

    # 连接 demo 库建表 + 写数据
    conn = await asyncpg.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DEMO_DB_USER, password=settings.DEMO_DB_PASSWORD,
        database=settings.DEMO_DB_NAME,
    )

    if drop:
        print("删除旧表 ...")
        await conn.execute("DROP TABLE IF EXISTS outpatient_visits, inpatient_records, departments CASCADE")

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(50) UNIQUE NOT NULL,
            category VARCHAR(20) NOT NULL,
            building VARCHAR(20) NOT NULL,
            floor INTEGER NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS outpatient_visits (
            id BIGSERIAL PRIMARY KEY,
            visit_no VARCHAR(20) UNIQUE NOT NULL,
            patient_no VARCHAR(20) NOT NULL,
            patient_name VARCHAR(50) NOT NULL,
            patient_phone VARCHAR(20) NOT NULL,
            id_card VARCHAR(18) NOT NULL,
            department_id BIGINT NOT NULL REFERENCES departments(id),
            visit_date DATE NOT NULL,
            visit_type VARCHAR(10) NOT NULL,
            registration_fee NUMERIC(8,2) NOT NULL,
            doctor_name VARCHAR(50) NOT NULL,
            status VARCHAR(10) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS inpatient_records (
            id BIGSERIAL PRIMARY KEY,
            record_no VARCHAR(20) UNIQUE NOT NULL,
            patient_no VARCHAR(20) NOT NULL,
            patient_name VARCHAR(50) NOT NULL,
            id_card VARCHAR(18) NOT NULL,
            department_id BIGINT NOT NULL REFERENCES departments(id),
            admit_date DATE NOT NULL,
            discharge_date DATE,
            stay_days INTEGER,
            total_cost NUMERIC(12,2),
            status VARCHAR(15) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """)

    dept_count = await conn.fetchval("SELECT COUNT(*) FROM departments")
    if dept_count == 0:
        rows = await conn.executemany(
            "INSERT INTO departments (name, category, building, floor) VALUES ($1,$2,$3,$4)",
            DEPARTMENTS,
        )
        print(f"科室维表: {len(DEPARTMENTS)} 行")
    else:
        print(f"科室维表已存在 {dept_count} 行，跳过")

    visit_count = await conn.fetchval("SELECT COUNT(*) FROM outpatient_visits")
    if visit_count == 0:
        dept_ids = [r[0] for r in await conn.fetch("SELECT id FROM departments ORDER BY id")]
        await _seed_visits(conn, dept_ids)
    else:
        print(f"门诊数据已存在 {visit_count} 行，跳过")

    inpatient_count = await conn.fetchval("SELECT COUNT(*) FROM inpatient_records")
    if inpatient_count == 0:
        dept_ids = [r[0] for r in await conn.fetch("SELECT id FROM departments ORDER BY id")]
        await _seed_inpatients(conn, dept_ids)
    else:
        print(f"住院数据已存在 {inpatient_count} 行，跳过")

    await conn.close()
    print("\n初始化完成！")


async def _seed_visits(conn: asyncpg.Connection, dept_ids: list[int]):
    """种子：最近一年门诊挂号明细，按科室权重生成"""
    total_target = 120_000  # 一年总量，约 300+ / 天
    today = date.today()
    start = today - timedelta(days=365)

    # 按天 × 科室权重生成
    days = (today - start).days
    total_weight = sum(DEPT_WEIGHTS)
    batch = []
    seq = 0
    for d in range(days):
        day = start + timedelta(days=d)
        # 周末门诊量减半
        day_factor = 0.4 if day.weekday() >= 5 else 1.0
        # 轻微的季度波动 + 随机抖动
        month_factor = 1.0 + 0.15 * ((day.month % 3) - 1) / 2
        for i, dept_id in enumerate(dept_ids):
            n = int(total_target / days * DEPT_WEIGHTS[i] / total_weight * day_factor * month_factor)
            for _ in range(n):
                seq += 1
                vt = random.choices(VISIT_TYPES, weights=VISIT_TYPE_WEIGHTS)[0]
                batch.append((
                    f"V{day:%Y%m%d}{seq:07d}",
                    f"P{random.randint(100000, 999999)}",
                    random.choice(["张伟", "王芳", "李娜", "刘强", "陈静", "杨洋", "黄敏", "吴涛"]),
                    f"138{random.randint(10000000, 99999999)}",
                    "".join(random.choice("0123456789X") for _ in range(18)),
                    dept_id, day, vt,
                    random.choice(REG_FEES),
                    random.choice(DOCTORS),
                    random.choices(STATUSES, weights=STATUS_WEIGHTS)[0],
                    datetime.combine(day, datetime.min.time()) + timedelta(
                        hours=random.randint(7, 17), minutes=random.randint(0, 59)
                    ),
                ))
                if len(batch) >= 5000:
                    await _insert_visits(conn, batch)
                    batch.clear()
    if batch:
        await _insert_visits(conn, batch)
    print(f"门诊挂号: {seq} 行")


async def _insert_visits(conn, batch):
    await conn.executemany(
        """INSERT INTO outpatient_visits
           (visit_no, patient_no, patient_name, patient_phone, id_card,
            department_id, visit_date, visit_type, registration_fee,
            doctor_name, status, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
        batch,
    )


async def _seed_inpatients(conn: asyncpg.Connection, dept_ids: list[int]):
    """种子：最近一年住院记录"""
    total_target = 6_000
    today = date.today()
    start = today - timedelta(days=365)
    days = (today - start).days
    total_weight = sum(DEPT_WEIGHTS)
    batch = []
    seq = 0
    for d in range(days):
        day = start + timedelta(days=d)
        for i, dept_id in enumerate(dept_ids):
            if random.random() > total_target / days * DEPT_WEIGHTS[i] / total_weight:
                continue
            seq += 1
            # 90% 已出院（ discharge = admit + stay_days），其余在院
            status = random.choices(INPATIENT_STATUSES, weights=INPATIENT_STATUS_WEIGHTS)[0]
            stay = random.randint(3, 25)
            if status == "in_treatment" and day > today - timedelta(days=30):
                discharge, stay_days = None, None
            else:
                discharge = min(day + timedelta(days=stay), today)
                stay_days = (discharge - day).days
            cost = round(random.uniform(3000, 80000), 2) if stay_days else None
            batch.append((
                f"I{day:%Y%m%d}{seq:06d}",
                f"P{random.randint(100000, 999999)}",
                random.choice(["张伟", "王芳", "李娜", "刘强", "陈静", "杨洋", "黄敏", "吴涛"]),
                "".join(random.choice("0123456789X") for _ in range(18)),
                dept_id, day, discharge, stay_days, cost, status,
                datetime.combine(day, datetime.min.time()),
            ))
            if len(batch) >= 5000:
                await _insert_inpatients(conn, batch)
                batch.clear()
    if batch:
        await _insert_inpatients(conn, batch)
    print(f"住院记录: {seq} 行")


async def _insert_inpatients(conn, batch):
    await conn.executemany(
        """INSERT INTO inpatient_records
           (record_no, patient_no, patient_name, id_card, department_id,
            admit_date, discharge_date, stay_days, total_cost, status, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
        batch,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--drop", action="store_true", help="先删表再重建")
    args = parser.parse_args()
    asyncio.run(main(drop=args.drop))
