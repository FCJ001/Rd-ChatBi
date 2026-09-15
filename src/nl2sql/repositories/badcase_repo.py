# ============================================================
# badcase 待审队列仓库 —— 本仓库第一个带写方法的 repository
#
# 为什么写方法放在这里而不是 scripts/：采集是在请求路径上发生的
# （pipeline.py / router.py），需要一个可复用的 upsert；而现有
# 元数据 repo 的写操作全在 build_nl2sql_meta.py 里，是因为那些是
# 离线构建动作，语义不同。
#
# ★ 整个闭环的枢纽是 upsert_badcase 的 ON CONFLICT：
#   同一条问题反复失败只累加 seen_count，**人工写的 golden_sql /
#   category / difficulty / status 永不被机器覆盖**。审核成果是稀缺
#   资源，自动采集绝不能把它冲掉。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.nl2sql.badcase_store import fingerprint, normalize_question
from src.nl2sql.models import Badcase

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPORTED = "exported"


@dataclass
class BadcaseRecord:
    """对外视图 —— 不把 ORM 对象漏给上层（与 PgMetaRepository 同一约定）"""
    id: int
    datasource_id: int
    datasource_code: str
    source: str
    question: str
    resolved_question: str = ""
    session_id: str = ""
    user_role: str = ""
    role_params: dict = field(default_factory=dict)
    predicted_sql: str = ""
    error_type: str = ""
    error_message: str = ""
    row_count: int = 0
    sql_fix_rounds: int = 0
    trace_id: str = ""
    fingerprint: str = ""
    seen_count: int = 1
    status: str = STATUS_PENDING
    golden_sql: str = ""
    category: str = ""
    difficulty: str = ""
    reviewer: str = ""
    reviewed_at: datetime | None = None
    reject_reason: str = ""
    note: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BadcaseRepository:
    def __init__(self, db: AsyncSession, datasource_id: int | None = None):
        self.db = db
        self.datasource_id = datasource_id

    # ── 写：采集 ──────────────────────────────────────────────
    async def upsert_badcase(
        self,
        *,
        datasource_id: int,
        datasource_code: str,
        source: str,
        question: str,
        resolved_question: str = "",
        session_id: str = "",
        user_role: str = "",
        role_params: dict | None = None,
        predicted_sql: str = "",
        error_type: str = "",
        error_message: str = "",
        row_count: int = 0,
        sql_fix_rounds: int = 0,
        trace_id: str = "",
        note: str = "",
    ) -> tuple[int, int]:
        """落一条待审案例。返回 (id, seen_count)。

        ★ 冲突时只做四件事：seen_count+1、累积来源、更新机器产出、保持人工成果。
          （见下方 set_ 里的注释 —— 这是整个闭环最需要保护的不变量）
        """
        fp = fingerprint(datasource_id, question)
        values = {
            "datasource_id": datasource_id,
            "datasource_code": datasource_code,
            "source": source,
            "question": question.strip(),
            "resolved_question": (resolved_question or "").strip(),
            "session_id": session_id or "",
            "user_role": user_role or "",
            "role_params": role_params or {},
            "predicted_sql": predicted_sql or "",
            "error_type": error_type or "",
            "error_message": (error_message or "")[:500],
            "row_count": row_count or 0,
            "sql_fix_rounds": sql_fix_rounds or 0,
            "trace_id": trace_id or "",
            "fingerprint": fp,
            "seen_count": 1,
            "status": STATUS_PENDING,
            "note": note or "",
        }

        stmt = pg_insert(Badcase).values(**values)
        excluded = stmt.excluded
        # ★ 来源要「累积」而不是被首个写入者占住：同一条问题可能先由服务端自动
        #   采集（api_error），之后被用户标记（manual）、又被历史挖掘命中（history）。
        #   只存第一个的话，另外几条采集路径会**悄无声息地消失** —— 看表的人
        #   以为历史挖掘没跑，实际是写进来了但没留痕。
        merged_source = case(
            (
                Badcase.source.contains(excluded.source),
                Badcase.source,
            ),
            else_=Badcase.source.concat("+").concat(excluded.source),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_badcase_ds_fp",
            set_={
                "seen_count": Badcase.seen_count + 1,
                "updated_at": func.now(),
                "source": merged_source,
                # ★ 人工成果优先：已写过 golden_sql 的行，机器一个字都不许改。
                #   未审核的行才刷新机器产出（predicted_sql / 改写问题 / 错误文本）。
                #   CASE WHEN 的写法比 coalesce 直白，且能一眼看出保护边界。
                "resolved_question": case(
                    (Badcase.status == STATUS_PENDING, excluded.resolved_question),
                    else_=Badcase.resolved_question,
                ),
                "predicted_sql": case(
                    (Badcase.status == STATUS_PENDING, excluded.predicted_sql),
                    else_=Badcase.predicted_sql,
                ),
                "error_type": case(
                    (Badcase.status == STATUS_PENDING, excluded.error_type),
                    else_=Badcase.error_type,
                ),
                "error_message": case(
                    (Badcase.status == STATUS_PENDING, excluded.error_message),
                    else_=Badcase.error_message,
                ),
                "row_count": case(
                    (Badcase.status == STATUS_PENDING, excluded.row_count),
                    else_=Badcase.row_count,
                ),
                "trace_id": case(
                    (Badcase.status == STATUS_PENDING, excluded.trace_id),
                    else_=Badcase.trace_id,
                ),
                # 以下四列在任何情况下都不覆盖，显式写出来是为了防止将来
                # 有人往 set_ 里加字段时顺手覆盖掉人工成果
                "status": Badcase.status,
                "golden_sql": Badcase.golden_sql,
                "category": Badcase.category,
                "difficulty": Badcase.difficulty,
            },
        ).returning(Badcase.id, Badcase.seen_count)

        row = (await self.db.execute(stmt)).one()
        await self.db.commit()
        return int(row[0]), int(row[1])

    # ── 写：审核 ──────────────────────────────────────────────
    async def update_review(
        self,
        case_id: int,
        *,
        status: str | None = None,
        golden_sql: str | None = None,
        category: str | None = None,
        difficulty: str | None = None,
        reject_reason: str | None = None,
        note: str | None = None,
        reviewer: str = "",
        sensitive_columns: list[str] | None = None,
    ) -> BadcaseRecord | None:
        """审核动作。

        ★ 置为 approved 时在**同一事务内**重跑校验：approve 是「这条线上记录
          能不能进评测集」的唯一闸口。把校验只放在 router 里是不够的 ——
          脚本、后台任务、以后新增的入口都会绕过它。校验不过直接 raise，
          事务里什么都没改，不会留下半截状态。
        """
        row = await self.db.get(Badcase, case_id)
        if row is None:
            return None
        if self.datasource_id is not None and row.datasource_id != self.datasource_id:
            return None

        # ★ 先算出「改完之后是什么样」再校验，校验通过才落到 ORM 对象上。
        #   反过来（先赋值再校验）有两个问题：① 会话里留下未提交的脏状态，
        #   下次查询触发 autoflush 时可能把不合法的值写进库；
        #   ② 抛异常后会话进入 aborted 事务，同一 session 的后续操作全报错。
        next_golden = golden_sql if golden_sql is not None else row.golden_sql
        next_category = category if category is not None else row.category
        next_difficulty = difficulty if difficulty is not None else row.difficulty

        if status is not None and status == STATUS_APPROVED:
            from src.nl2sql.badcase_store import validate_case

            validate_case(
                question=row.question,
                golden_sql=next_golden,
                category=next_category,
                difficulty=next_difficulty,
                sensitive_columns=sensitive_columns,
            )

        # 校验过了，全部失败路径已排除 —— 从这里开始才动对象
        row.golden_sql = next_golden
        row.category = next_category
        row.difficulty = next_difficulty
        if reject_reason is not None:
            row.reject_reason = reject_reason
        if note is not None:
            row.note = note

        if status is not None:
            row.status = status
            row.reviewed_at = datetime.now()
            row.reviewer = reviewer or row.reviewer

        await self.db.commit()
        await self.db.refresh(row)
        return _to_record(row)

    async def mark_exported(self, case_ids: list[int]) -> int:
        """导出成功落到仓库后再调用 —— ★ 顺序很重要：文件没提交就标 exported
        会造出「已导出但仓库里没有」的案例黑洞。"""
        if not case_ids:
            return 0
        rows = (await self.db.execute(
            select(Badcase).where(Badcase.id.in_(case_ids))
        )).scalars().all()
        for r in rows:
            r.status = STATUS_EXPORTED
        await self.db.commit()
        return len(rows)

    # ── 读 ────────────────────────────────────────────────────
    async def list_cases(
        self,
        *,
        status: str | None = None,
        error_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BadcaseRecord], int]:
        """返回 (本页记录, 满足条件的总数)。默认按热度降序 —— 反复踩的坑先审。"""
        conds = []
        if self.datasource_id is not None:
            conds.append(Badcase.datasource_id == self.datasource_id)
        if status:
            conds.append(Badcase.status == status)
        if error_type:
            conds.append(Badcase.error_type == error_type)

        total = (await self.db.execute(
            select(func.count()).select_from(Badcase).where(*conds)
        )).scalar_one()

        rows = (await self.db.execute(
            select(Badcase).where(*conds)
            .order_by(Badcase.seen_count.desc(), Badcase.id.desc())
            .limit(limit).offset(offset)
        )).scalars().all()
        return [_to_record(r) for r in rows], int(total)

    async def get_case(self, case_id: int) -> BadcaseRecord | None:
        row = await self.db.get(Badcase, case_id)
        if row is None:
            return None
        if self.datasource_id is not None and row.datasource_id != self.datasource_id:
            return None
        return _to_record(row)

    async def list_approved(self, datasource_id: int | None = None) -> list[BadcaseRecord]:
        """导出用：全部 approved 案例（含 id 升序，导出结果稳定可 diff）"""
        conds = [Badcase.status == STATUS_APPROVED]
        ds_id = datasource_id if datasource_id is not None else self.datasource_id
        if ds_id is not None:
            conds.append(Badcase.datasource_id == ds_id)
        rows = (await self.db.execute(
            select(Badcase).where(*conds).order_by(Badcase.id)
        )).scalars().all()
        return [_to_record(r) for r in rows]

    async def stats(self) -> dict:
        """审核队列概览 —— 积压必须能被看到，否则这张表会变成垃圾场。

        ★ 按 datasource_id 过滤：注册了多个数据源时，审核页是按数据源切换的，
          不过滤会让每个数据源都显示全库的计数（看到的积压和列表对不上）。
        """
        conds = []
        if self.datasource_id is not None:
            conds.append(Badcase.datasource_id == self.datasource_id)
        by_status = dict((await self.db.execute(
            select(Badcase.status, func.count()).where(*conds).group_by(Badcase.status)
        )).all())
        by_type = dict((await self.db.execute(
            select(Badcase.error_type, func.count()).where(*conds).group_by(Badcase.error_type)
        )).all())
        return {"by_status": by_status, "by_error_type": by_type,
                "total": sum(by_status.values())}


def _to_record(m: Badcase) -> BadcaseRecord:
    return BadcaseRecord(
        id=m.id,
        datasource_id=m.datasource_id,
        datasource_code=m.datasource_code or "",
        source=m.source or "",
        question=m.question or "",
        resolved_question=m.resolved_question or "",
        session_id=m.session_id or "",
        user_role=m.user_role or "",
        role_params=m.role_params or {},
        predicted_sql=m.predicted_sql or "",
        error_type=m.error_type or "",
        error_message=m.error_message or "",
        row_count=m.row_count or 0,
        sql_fix_rounds=m.sql_fix_rounds or 0,
        trace_id=m.trace_id or "",
        fingerprint=m.fingerprint or "",
        seen_count=m.seen_count or 1,
        status=m.status or STATUS_PENDING,
        golden_sql=m.golden_sql or "",
        category=m.category or "",
        difficulty=m.difficulty or "",
        reviewer=m.reviewer or "",
        reviewed_at=m.reviewed_at,
        reject_reason=m.reject_reason or "",
        note=m.note or "",
        created_at=m.created_at,
        updated_at=m.updated_at,
    )
