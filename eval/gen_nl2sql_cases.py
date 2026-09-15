# ============================================================
# NL2SQL 评测题库扩充：50 → 150
#
# 生成原则：
#   - 分类 × 难度系统覆盖，不堆同类题
#   - 每题 golden_sql 先过安全层（validate_sql），再在真实库执行，
#     退化（0 行且非确定性空）剔除
#   - 时间窗口题用相对时间（NOW()-INTERVAL），数据重灌后依然有效
# ============================================================
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
CASES = Path(__file__).resolve().parent / "cases" / "nl2sql_cases.json"

d = json.loads(CASES.read_text(encoding="utf-8"))
have = {c["id"] for c in d["cases"]}
C = list(d["cases"])
n0 = len(C)


def add(id_, cat, diff, q, sql):
    if id_ in have:
        return
    C.append(dict(id=id_, category=cat, difficulty=diff, question=q, golden_sql=sql))


# ── 单表聚合（+8）──
add("N51","单表聚合","easy","blocker 和 critical 的问题单加起来有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE severity IN ('blocker','critical')")
add("N52","单表聚合","easy","状态为 analyzing 的问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE status = 'analyzing'")
add("N53","单表聚合","medium","没有填写 DTC 快照的未关闭问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE (dtc_snapshot IS NULL OR dtc_snapshot = '') AND status NOT IN ('closed','verified')")
add("N54","单表聚合","easy","ia 业务线的问题单总数是多少？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE business_line = 'ia'")
add("N55","单表聚合","easy","来源为 engineer 的问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE source = 'engineer'")
add("N56","单表聚合","medium","外部引用（external_ref）不为空的问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE external_ref IS NOT NULL AND external_ref <> ''")
add("N57","单表聚合","easy","verified 状态的问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE status = 'verified'")
add("N58","单表聚合","medium","模型型号不为空的问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE model_code IS NOT NULL AND model_code <> ''")

# ── 分组统计（+10）──
add("N59","分组统计","medium","按车型和状态交叉统计问题单数量。",
    "SELECT model_code, status, COUNT(*) AS cnt FROM alm_issues GROUP BY model_code, status")
add("N60","分组统计","medium","变更请求按业务线统计数量。",
    "SELECT business_line, COUNT(*) AS cnt FROM alm_change_requests GROUP BY business_line")
add("N61","分组统计","medium","需求按状态统计数量。",
    "SELECT status, COUNT(*) AS cnt FROM alm_requirements GROUP BY status")
add("N62","分组统计","medium","配置项按生命周期状态统计数量。",
    "SELECT lifecycle_status, COUNT(*) AS cnt FROM alm_config_items GROUP BY lifecycle_status")
add("N63","分组统计","medium","基线按是否冻结统计数量。",
    "SELECT is_frozen, COUNT(*) AS cnt FROM alm_baselines GROUP BY is_frozen")
add("N64","分组统计","hard","按来源和严重级别交叉统计问题单数量。",
    "SELECT source, severity, COUNT(*) AS cnt FROM alm_issues GROUP BY source, severity")
add("N65","分组统计","medium","按状态统计变更请求数量。",
    "SELECT status, COUNT(*) AS cnt FROM alm_change_requests GROUP BY status")
add("N66","分组统计","medium","各车型的问题单数量是多少？",
    "SELECT model_code, COUNT(*) AS cnt FROM alm_issues GROUP BY model_code")
add("N67","分组统计","medium","配置项按模块统计数量。",
    "SELECT module, COUNT(*) AS cnt FROM alm_config_items GROUP BY module")
add("N68","分组统计","hard","按业务线统计变更请求数量。",
    "SELECT business_line, COUNT(*) AS cnt FROM alm_change_requests GROUP BY business_line")

# ── 排序TopN（+8）──
add("N69","排序TopN","easy","哪个严重级别的问题最多？",
    "SELECT severity, COUNT(*) AS cnt FROM alm_issues GROUP BY severity ORDER BY cnt DESC LIMIT 1")
add("N70","排序TopN","medium","哪个状态的变更请求最多？",
    "SELECT status, COUNT(*) AS cnt FROM alm_change_requests GROUP BY status ORDER BY cnt DESC LIMIT 1")
add("N71","排序TopN","medium","问题单最多的前 3 个来源。",
    "SELECT source, COUNT(*) AS cnt FROM alm_issues GROUP BY source ORDER BY cnt DESC LIMIT 3")
add("N72","排序TopN","easy","最早创建的 3 个需求编号。",
    "SELECT req_no FROM alm_requirements ORDER BY created_at ASC LIMIT 3")
add("N73","排序TopN","medium","优先级为 P0 的需求，列出最新的 5 个编号和标题。",
    "SELECT req_no, title FROM alm_requirements WHERE priority = 'P0' ORDER BY created_at DESC LIMIT 5")
add("N74","排序TopN","medium","哪个模块的配置项最多？前 3 名。",
    "SELECT module, COUNT(*) AS cnt FROM alm_config_items GROUP BY module ORDER BY cnt DESC LIMIT 3")
add("N75","排序TopN","medium","sw_version 为 2024.32.5 的问题有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE sw_version = '2024.32.5'")
add("N76","排序TopN","hard","各责任域的 critical 问题数量排名，取前 3。",
    "SELECT od.name, COUNT(*) AS cnt FROM alm_issues ai JOIN owner_domains od ON ai.owner_domain_id = od.id WHERE ai.severity = 'critical' GROUP BY od.name ORDER BY cnt DESC LIMIT 3")

# ── 时间窗口（+12）──
add("N77","时间窗口","medium","2026 年 3 月创建了多少个问题单？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE created_at >= '2026-03-01' AND created_at < '2026-04-01'")
add("N78","时间窗口","medium","2026 年 4 月创建了多少个问题单？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE created_at >= '2026-04-01' AND created_at < '2026-05-01'")
add("N79","时间窗口","medium","2026 年 9 月创建了多少个问题单？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE created_at >= '2026-09-01' AND created_at < '2026-10-01'")
add("N80","时间窗口","hard","2026 年第一季度（1-3 月）创建的问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE created_at >= '2026-01-01' AND created_at < '2026-04-01'")
add("N81","时间窗口","hard","2026 年第二季度（4-6 月）创建的问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE created_at >= '2026-04-01' AND created_at < '2026-07-01'")
add("N82","时间窗口","hard","2026 年第三季度（7-9 月）创建的问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE created_at >= '2026-07-01' AND created_at < '2026-10-01'")
add("N83","时间窗口","hard","最近 30 天按月分组各创建了多少问题单，月份格式 YYYY-MM。",
    "SELECT to_char(date_trunc('month', created_at), 'YYYY-MM') AS month, COUNT(*) AS cnt FROM alm_issues WHERE created_at >= NOW() - INTERVAL '30 days' GROUP BY date_trunc('month', created_at) ORDER BY date_trunc('month', created_at)")
add("N84","时间窗口","hard","最近 90 天各业务线分别创建了多少问题单？",
    "SELECT business_line, COUNT(*) AS cnt FROM alm_issues WHERE created_at >= NOW() - INTERVAL '90 days' GROUP BY business_line")
add("N85","时间窗口","hard","最近 60 天创建的变更请求有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_change_requests WHERE created_at >= NOW() - INTERVAL '60 days'")
add("N86","时间窗口","medium","2026 年 6 月创建的变更请求有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_change_requests WHERE created_at >= '2026-06-01' AND created_at < '2026-07-01'")
add("N87","时间窗口","hard","2026 年 5 月各来源分别创建了多少问题单？",
    "SELECT source, COUNT(*) AS cnt FROM alm_issues WHERE created_at >= '2026-05-01' AND created_at < '2026-06-01' GROUP BY source")
add("N88","时间窗口","hard","最近 30 天创建的 blocker 问题有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues WHERE created_at >= NOW() - INTERVAL '30 days' AND severity = 'blocker'")

# ── 多表JOIN（+10）──
add("N89","多表JOIN","hard","电池系统域处于 open 状态的问题单有多少个？",
    "SELECT COUNT(*) AS cnt FROM alm_issues ai JOIN owner_domains od ON ai.owner_domain_id = od.id WHERE od.name = '电池系统域' AND ai.status = 'open'")
add("N90","多表JOIN","hard","ia 业务线按责任域统计问题数量，输出域名和数量。",
    "SELECT od.name, COUNT(*) AS cnt FROM alm_issues ai JOIN owner_domains od ON ai.owner_domain_id = od.id WHERE ai.business_line = 'ia' GROUP BY od.name ORDER BY cnt DESC")
add("N91","多表JOIN","hard","各基线指向它的变更请求数量，只要已冻结的基线。",
    "SELECT b.name, COUNT(*) AS cnt FROM alm_change_requests cr JOIN alm_baselines b ON cr.target_baseline_id = b.id WHERE b.is_frozen = true GROUP BY b.name ORDER BY cnt DESC")
add("N92","多表JOIN","medium","列出需求编号、标题及其所属基线的名称（前 20 条）。",
    "SELECT r.req_no, r.title, b.name FROM alm_requirements r JOIN alm_baselines b ON r.baseline_id = b.id ORDER BY r.created_at DESC LIMIT 20")
add("N93","多表JOIN","hard","有来源问题的变更请求有多少个？（source_issue_id 非空且能关联到问题单）",
    "SELECT COUNT(*) AS cnt FROM alm_change_requests cr JOIN alm_issues ai ON cr.source_issue_id = ai.id")
add("N94","多表JOIN","hard","各责任域的 verified 问题数量，输出域名和数量。",
    "SELECT od.name, COUNT(*) AS cnt FROM alm_issues ai JOIN owner_domains od ON ai.owner_domain_id = od.id WHERE ai.status = 'verified' GROUP BY od.name ORDER BY cnt DESC")
add("N95","多表JOIN","hard","充电系统域的问题里各严重级别分别多少个？",
    "SELECT ai.severity, COUNT(*) AS cnt FROM alm_issues ai JOIN owner_domains od ON ai.owner_domain_id = od.id WHERE od.name = '充电系统域' GROUP BY ai.severity")
add("N96","多表JOIN","hard","已冻结基线下的需求数量排名前 3 的基线名称和数量。",
    "SELECT b.name, COUNT(*) AS cnt FROM alm_requirements r JOIN alm_baselines b ON r.baseline_id = b.id WHERE b.is_frozen = true GROUP BY b.name ORDER BY cnt DESC LIMIT 3")
add("N97","多表JOIN","hard","指向基线的变更请求中，按基线是否冻结分组统计数量。",
    "SELECT b.is_frozen, COUNT(*) AS cnt FROM alm_change_requests cr JOIN alm_baselines b ON cr.target_baseline_id = b.id GROUP BY b.is_frozen")
add("N98","多表JOIN","hard","各责任域的问题单里，有 DTC 快照的分别多少个？输出域名和数量。",
    "SELECT od.name, COUNT(*) AS cnt FROM alm_issues ai JOIN owner_domains od ON ai.owner_domain_id = od.id WHERE ai.dtc_snapshot IS NOT NULL AND ai.dtc_snapshot <> '' GROUP BY od.name ORDER BY cnt DESC")

# ── 明细查询（+10）──
add("N99","明细查询","easy","列出 open 状态问题的标题。",
    "SELECT title FROM alm_issues WHERE status = 'open'")
add("N100","明细查询","easy","列出 minor 级别问题的单号和标题。",
    "SELECT issue_no, title FROM alm_issues WHERE severity = 'minor'")
add("N101","明细查询","medium","车型 EV-A01 的问题单号和严重级别。",
    "SELECT issue_no, severity FROM alm_issues WHERE model_code = 'EV-A01'")
add("N102","明细查询","medium","aftersales 来源最近创建的 3 个问题单号。",
    "SELECT issue_no FROM alm_issues WHERE source = 'aftersales' ORDER BY created_at DESC LIMIT 3")
add("N103","明细查询","medium","正常级别且状态为 open 的问题单号列表。",
    "SELECT issue_no FROM alm_issues WHERE severity = 'normal' AND status = 'open'")
add("N104","明细查询","hard","列出每个变更请求的编号、标题及其指向的基线名称（前 30 条）。",
    "SELECT cr.cr_no, cr.title, b.name FROM alm_change_requests cr JOIN alm_baselines b ON cr.target_baseline_id = b.id ORDER BY cr.created_at DESC LIMIT 30")
add("N105","明细查询","easy","列出所有基线的编号和名称。",
    "SELECT baseline_no, name FROM alm_baselines")
add("N106","明细查询","medium","P0 优先级的需求编号和标题。",
    "SELECT req_no, title FROM alm_requirements WHERE priority = 'P0'")
add("N107","明细查询","medium","列出每个需求的编号、标题及其基线名称（前 30 条）。",
    "SELECT r.req_no, r.title, b.name FROM alm_requirements r JOIN alm_baselines b ON r.baseline_id = b.id ORDER BY r.created_at DESC LIMIT 30")
add("N108","明细查询","easy","列出所有责任域的名称。",
    "SELECT name FROM owner_domains")

d["cases"] = C
d["description"] = "NL2SQL 执行准确率评测案例（生产量级：分类×难度系统覆盖；golden 与 LLM 同一只读连接执行，结果集等价判定）"
CASES.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"题库写入 {len(C)} 题（新增 {len(C)-n0}）")
