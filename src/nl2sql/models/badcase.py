# ============================================================
# 线上 badcase / 待审评测案例 —— 评测集的真相来源
#
# 与 nl2sql_* 那批表的区别：那批是「构建出来的元数据」（conf/projects/*.yaml
# 解析后灌进 PG/Milvus/ES，--rebuild 会清空重建）；本表是「线上回流的数据」，
# ★ 绝不能出现在 build_nl2sql_meta.py 的 _rebuild_pg 里，否则重建元数据会
#   顺手清掉全部回流积累。
#
# 三个采集来源（source 字段）：
#   api_error  查询失败/被拒时服务端自动落库（见 pipeline.py / router.py）
#   manual     前端「答得不对」按钮（见 badcase_router.py）
#   history    从会话历史批量挖（见 scripts/mine_badcases.py）
#
# ★ 不存结果行 data/columns：与 ctx_store 同一取向（结果集不过河），
#   审核人判断对错的依据是 predicted_sql + 问题本身，不是数据。
# ★ 不存 user_id：header 认证模式下它可伪造，且属于个人信息。
# ============================================================

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from src.core.base_model import BaseModel


class Badcase(BaseModel):
    __tablename__ = "chatbi_badcases"
    # ★ 去重靠 (datasource_id, fingerprint) 唯一约束 + ON CONFLICT upsert：
    #   同一条问题反复失败只累加 seen_count，人工写的 golden_sql 永不被覆盖。
    #   必须带 datasource_id —— 同一句话在 hospital_demo 和 rd_agent 是两个案例。
    __table_args__ = (
        UniqueConstraint("datasource_id", "fingerprint", name="uq_badcase_ds_fp"),
    )

    datasource_id = Column(BigInteger, nullable=False, index=True,
                           comment="数据源ID（bi_datasources.id），多数据源隔离依据")
    datasource_code = Column(String(50), nullable=False, index=True,
                             comment="数据源编码（冗余，导出/脚本免 JOIN）")
    source = Column(String(100), nullable=False, index=True,
                    comment="采集来源，多个来源相遇时按 + 累积，如 api_error+history")

    question = Column(Text, nullable=False, comment="用户原始问题")
    resolved_question = Column(Text, nullable=False, default="",
                               comment="多轮改写后的独立问题（挖案例优先用它）")
    session_id = Column(String(128), nullable=False, default="",
                        comment="会话ID（人工回溯上下文用；不存 user_id）")
    user_role = Column(String(50), nullable=False, default="",
                       comment="角色编码快照 —— 行级过滤与结果强相关，缺了复现不出来")
    role_params = Column(JSON, default=dict,
                         comment="行级过滤参数快照 {dept_id, owner_domain_id, business_line}")

    predicted_sql = Column(Text, nullable=False, default="",
                           comment="系统生成的 SQL（已过安全层与角色行级过滤）")
    error_type = Column(String(40), nullable=False, default="", index=True,
                        comment="db_error/timeout/role_denied/llm_error/empty_result/truncated/unsatisfied")
    error_message = Column(String(500), nullable=False, default="",
                           comment="★ 仅用户可见的脱敏错误；原始 DB 错误只进日志，靠 trace_id 关联")
    row_count = Column(Integer, nullable=False, default=0,
                       comment="结果行数。★ 上限被 MAX_ROW_LIMIT 钳到 100，==100 是「被截断」")
    sql_fix_rounds = Column(Integer, nullable=False, default=0,
                            comment="LLM 纠错轮数（>0 通常指向召回类 badcase）")
    trace_id = Column(String(32), nullable=False, default="", index=True,
                      comment="全链路 trace_id —— 回捞服务端原始错误日志的唯一钥匙")

    fingerprint = Column(String(64), nullable=False, index=True,
                         comment="去重指纹 sha256(datasource_id|归一化问题)")
    seen_count = Column(Integer, nullable=False, default=1,
                        comment="同一指纹出现次数（热度，审核排序用）")

    status = Column(String(20), nullable=False, index=True,
                    comment="pending / approved / rejected / exported")
    golden_sql = Column(Text, nullable=False, default="",
                        comment="审核人写的标准答案 —— approve 的必要条件")
    category = Column(String(50), nullable=False, default="",
                      comment="分层维度（必须是评测器的 6 个中文类之一）")
    difficulty = Column(String(10), nullable=False, default="",
                        comment="easy / medium / hard（★ 合法值受离线门禁检查）")
    reviewer = Column(String(100), nullable=False, default="",
                      comment="审核人标识。★ 只来自 ADMIN_TOKEN/JWT，绝不信 X-User-*")
    reviewed_at = Column(DateTime, nullable=True, comment="审核时间")
    reject_reason = Column(String(200), nullable=False, default="", comment="驳回原因")
    note = Column(Text, nullable=False, default="", comment="备注")
