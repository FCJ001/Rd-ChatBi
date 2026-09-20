# ============================================================
# auto_full 汽车全域 schema 定义（数据源 auto_full）
#
# 121 张表 / 13 个子域：ALM 研发、零部件 BOM、采购、整车主数据、
# 生产制造、QMS 质量、销售、售后服务、召回、车联网/OTA、三电电池、
# 智驾测试、财务。所有列带中文说明/别名，FK/枚举/度量/日期角色齐全，
# 由 scripts/gen_auto_full.py 消费生成 DDL + 种子数据 + conf/projects/auto_full.yaml。
#
# 设计原则：
#   1. 表多且语义相近（问题/缺陷/故障/投诉/召回 五类"问题域"并存），
#      逼 recall_columns / filter_tables 真正做 discrimination；
#   2. 枚举值大量用中文（品牌/颜色/状态），考验 ES 值召回与
#      「真实值 vs 同义词」机制；个别状态列保留英文值 +
#      prompt 教学说明（对齐 rd_agent 的行为基线）；
#   3. 敏感列（vin / cust_name / cust_phone / cust_id_number）
#      物理存在但元数据故意不定义 —— Prompt 层 + 执行层双防线。
# ------------------------------------------------------------
# gen 说明：字符串里的 {i} 是行号占位（1..n），@表名@ 是该表行数
# 占位（外键取模用），由生成器在发射 SQL 时替换。
# ============================================================

from dataclasses import dataclass, field


@dataclass
class Col:
    name: str
    type: str
    role: str            # primary_key / foreign_key / dimension / measure / date
    desc: str = ""
    alias: list = field(default_factory=list)
    sync: bool = False   # 进 ES 值索引
    enum_source: str | None = None
    gen: str = ""        # 种子 SQL 表达式
    sensitive: bool = False  # 物理存在但不出现在 YAML


@dataclass
class Tab:
    name: str
    role: str            # fact / dim
    desc: str
    rows: int
    cols: list
    label: str = "name"  # 维表展示列（FK enum_source 用）


# ── 常用种子表达式 ──────────────────────────────────────────
# 时间窗：2025-09-01（周一）起 54 个整周（至 2026-09-13），保证"近7天/近30天/上月"都有数据
#
# ★ 周末低谷（数据体检实测缺失项，2026-09-20 修）：旧公式 (i*7)%381 把行均匀
#   铺到每一天，按星期几的分布完全平坦（2018/2087/2087/…）—— 真实业务的
#   周末客流/产量有明显低谷。新公式用行号散列做两层映射：先取第几周
#   （0..53），再按 82/18 落到周中（周一~周五）或周末（周六/日）。
#   周末单日量 ≈ 周中的 55%（18%/2 ÷ 82%/5），按星期几聚合呈「周末双坑」。
#
# ★ 日偏移不掺盐（{salt}）：同一行的所有时间列必须落在同一天 —— TS2 与 TS
#   用同一个日偏移表达式，只加 10~190 分钟，保证 finished_at > intake_at
#   这类行内时序约束。旧 TS2 用 (%380) 与 TS 的 (%381) 不同模，个别行会
#   出现完工早于进厂一天以上的脏数据。
_H = "((({i} * 2654435761) % 4294967296))"
_WEEK = f"(({_H} / 5400) % 54)"
_SLOT = f"({_H} % 100)"
_DAY_OFF = f"({_WEEK} * 7 + CASE WHEN {_SLOT} < 82 THEN {_SLOT} % 5 ELSE 5 + {_SLOT} % 2 END)"
TS = f"TIMESTAMP '2025-09-01' + {_DAY_OFF} * INTERVAL '1 day' + ({{i}} % 24) * INTERVAL '1 hour'"
# 相关时间列（updated/ended/finished…）：与 TS 同日 + 10~190 分钟，保证晚于 TS
TS2 = f"TIMESTAMP '2025-09-01' + {_DAY_OFF} * INTERVAL '1 day' + ({{i}} % 24) * INTERVAL '1 hour' + ((({{i}} * 13) % 180) + 10) * INTERVAL '1 minute'"
DAY = f"DATE '2025-09-01' + {_DAY_OFF} * INTERVAL '1 day'"
MON = "DATE '2025-09-01' + (({i} * 3) % 13) * INTERVAL '1 month'"  # 月度粒度
NO = lambda p, w=7: f"'{p}' || lpad({{i}}::text, {w}, '0')"  # noqa: E731
def ARR(vals, cast=""):  # noqa: E731
    if cast and not cast.endswith("]"):
        cast += "[]"  # ::int → ::int[]（:: 优先级高于下标，标量 cast 会废掉数组下标）
    return f"(ARRAY[{','.join(chr(39)+v+chr(39) for v in vals)}]{cast})[({{i}} % {len(vals)}) + 1]"
# ★ 外键分布：**必须偏斜**。旧实现 `(i-1) % N + 1` 是均匀取模，导致
#   各品牌订单数 2160/2160/2160/1705…、各经销商 938/938/938/938 ——
#   极差只有个位数百分比。真实业务是长尾：少数爆款车型占大头、大店一天
#   50 单小店一周 3 单。均匀分布的直接后果是**「哪个卖得最多」这类题的
#   正确答案不依赖业务理解**（所有组几乎并列）—— 之前 13 道不可判分的
#   排序题，根因就在这里。
#   新实现用幂律映射（Zipf 式）：`1 + floor(u^alpha * (N-1))`，u∈[0,1) 由散列给出。
#   alpha=2.5 实测头部占比 ~36%、首尾比 11×，接近真实长尾。
#   ★ {salt} 占位由 _sub 按**列名**填：所有列共用同一个散列的话，里程最高的车
#     必然也是工时最长的车（实测 corr=1.000）—— 真实数据是带噪声的相关（0.3~0.7）。
#     加盐让每列走不同的散列流，消除这种假相关。
_ALPHA = 2.5
_B = 1000003  # 取值空间分母（质数，配合 Knuth 乘数打散低位）


def _skew(ref: str) -> str:
    """幂律偏斜的外键值（1..N），替代均匀取模"""
    return (f"(1 + floor(power((((({{i}} * 2654435761) % 4294967296) + {{salt}}) % {_B}) / {_B}.0, {_ALPHA}) "
            f"* (@{ref}@ - 1))::int)")


FKG = lambda ref: _skew(ref)  # noqa: E731
FKG_NULL = lambda ref, m=5: f"CASE WHEN {{i}} % {m} = 0 THEN NULL ELSE {_skew(ref)} END"  # noqa: E731

# ★ 数值列生成。旧公式有两个已实测确认的缺陷：
#   ① **值域被缩小**：末尾 `/ scale` 不只是"保留小数位"，它把值域也除了 10/100 ——
#      `NUM(5, 80, 10, 1)` 本该 5~80 小时，实际 0.5~7.9。全库 91 处 NUM 里
#      71 处 scale>1，等于 78% 的数值列小了一个量级：订单金额 0.9~5.9 元、
#      整车指导价 0.8~5.9 万元 —— 业务上都不成立。
#   ② **取值稀疏**：`i*37 % (hi-lo)` 的候选个数恰好是 `hi-lo`，与行数无关。
#      40000 行工单只有 75 个不同工时，"排序取前 N 必然并列几百行。
#   新公式：`lo + (hash % B) * (hi-lo) / B`（Knuth 乘数打散），小数位由 nd 决定
#   —— **nd 才是"保留几位小数"的那个参数**，scale 保留只为兼容既有调用签名。
#   ★ 注意 nd 同时是基数的上限：nd=1 时 [5,80] 最多 751 个值（40000 行仍会并列），
#     所以**小值域的 nd=1 列不适合出排序题**；这不是公式能解决的（调 nd 会让
#     工时显示成 45.23 小时，反而更假）。
NUM = lambda lo, hi, scale=1, nd=1: (  # noqa: E731
    f"round({lo}.0 + (((({{i}} * 2654435761) % 4294967296) + {{salt}}) % {_B}) "
    f"* ({hi}.0 - {lo}.0) / {_B}.0, {nd})"
)
VIN = lambda: "'LSV' || lpad({i}::text, 14, '0')"  # noqa: E731


def PK() -> Col:
    return Col("id", "BIGINT", "primary_key", "主键", gen="{i}")


def FK(name, ref, desc, alias=None, label="name", nullable=False) -> Col:
    return Col(name, "BIGINT", "foreign_key", desc,
               alias=alias or [], enum_source=f"{ref}.{label}",
               gen=FKG_NULL(ref) if nullable else FKG(ref))


def EN(name, values, desc, alias=None, type=None, teach=False) -> Col:
    width = type or f"VARCHAR({max(12, max(len(v) for v in values) * 2)})"
    d = desc
    if teach:
        d += f"，枚举值固定为：{'/'.join(values)}，过滤时必须用这些字面值"
    return Col(name, width, "dimension", d, alias=alias or [], sync=True,
               gen=ARR(values, "::varchar[]"))


def D(name, type, desc, gen, alias=None) -> Col:
    return Col(name, type, "dimension", desc, alias=alias or [], gen=gen)


def M(name, desc, gen, alias=None, type="NUMERIC(14,2)") -> Col:
    return Col(name, type, "measure", desc, alias=alias or [], gen=gen)


def TS_C(name="created_at", desc="创建时间", alias=None) -> Col:
    return Col(name, "TIMESTAMP", "date", desc,
               alias=alias or ["创建日期", "时间", "日期", "提单时间"], gen=TS)


def UPD() -> Col:
    return Col("updated_at", "TIMESTAMP", "date", "更新时间",
               alias=["修改时间"], gen=TS2)


BRANDS = ["比亚迪", "特斯拉", "蔚来", "理想", "小鹏", "吉利", "长安",
          "上汽大众", "一汽丰田", "广汽本田", "鸿蒙智行", "小米汽车", "零跑", "极氪"]
ENERGY = ["纯电", "插电混动", "增程式", "油电混动", "燃油"]
LEVELS = ["轿车", "SUV", "MPV", "跑车"]
COLORS = ["珠光白", "曜岩黑", "星辉银", "赤焰红", "深海蓝", "翡冷翠",
          "暮山紫", "卡其灰", "鎏金橙", "松霜绿", "水墨青", "琥珀棕",
          "雪域白", "时空银", "幻影黑", "晨曦金"]
REGIONS = ["华东", "华南", "华中", "华北", "西南", "东北", "西北"]
CITIES = ["上海", "杭州", "苏州", "南京", "广州", "深圳", "武汉", "长沙", "郑州",
          "北京", "天津", "石家庄", "成都", "重庆", "昆明", "沈阳", "长春", "哈尔滨",
          "西安", "兰州", "乌鲁木齐", "合肥", "南昌", "福州", "厦门", "青岛", "济南"]
SEVERITY = ["轻微", "一般", "严重", "致命"]
PRIORITIES = ["P0", "P1", "P2", "P3"]
YESNO = ["是", "否"]


def tables() -> list[Tab]:
    t: list[Tab] = []

    # ══════════════════════ 全局维表 ══════════════════════
    t += [
        Tab("veh_brands", "dim", "汽车品牌", len(BRANDS), [
            PK(),
            D("name", "VARCHAR(50)", "品牌名称", ARR(BRANDS), alias=["品牌", "厂牌"]),
            D("country", "VARCHAR(30)", "品牌国别", ARR(["中国", "美国", "德国", "日本"]), alias=["国家"]),
            TS_C(), UPD(),
        ]),
        Tab("veh_energy_types", "dim", "动力类型", len(ENERGY), [
            PK(),
            D("name", "VARCHAR(20)", "动力类型名称", ARR(ENERGY), alias=["能源类型", "动力形式", "能源"]),
            D("note", "VARCHAR(100)", "说明", ARR(["zero emission", "PHEV", "EREV", "HEV", "ICE"])),
            TS_C(),
        ]),
        Tab("veh_model_series", "dim", "车系", 45, [
            PK(),
            D("name", "VARCHAR(80)", "车系名称",
              ARR([f"{b} {s}" for b, s in zip(
                  [BRANDS[i % len(BRANDS)] for i in range(45)],
                  ["汉", "唐", "宋", "秦", "海豹", "Model 3", "Model Y", "ET5", "ES6", "EC7",
                   "L6", "L7", "L8", "L9", "G6", "G9", "P7", "银河E8", "星越", "深蓝SL",
                   "启源A07", "朗逸", "途观", "卡罗拉", "RAV4", "雅阁", "CR-V", "问界M5",
                   "问界M7", "问界M9", "SU7", "YU7", "C11", "C10", "T03", "001", "007",
                   "009", "X9", "G3i", "P5", "蔚来EP7", "腾势D9", "Z9", "iCAR 03"])]),
              alias=["车系"]),
            FK("brand_id", "veh_brands", "品牌ID，维表 veh_brands.id → veh_brands.name（品牌名称）；按品牌分组/展示时 JOIN veh_brands 取 name", alias=["品牌", "厂牌"]),
            TS_C("launch_date", "上市时间", alias=["上市日期"]), UPD(),
        ]),
        Tab("veh_models", "dim", "车型", 90, [
            PK(),
            D("name", "VARCHAR(80)", "车型名称",
              ARR([f"2026款 {'Pro' if i % 3 == 0 else 'Max' if i % 3 == 1 else 'Air'} {i}" for i in range(1, 91)]).replace("{i}", "{i}"),
              alias=["车型"]),
            FK("series_id", "veh_model_series", "车系ID，维表 veh_model_series.id → name（车系名称）；按车系分组时 JOIN 取 name", alias=["车系"]),
            FK("energy_type_id", "veh_energy_types", "动力类型ID，维表 veh_energy_types.id → name（动力类型名称）；按动力类型分组时 JOIN 取 name", alias=["动力类型", "能源类型", "能源"]),
            EN("level", LEVELS, "车身形式（轿车/SUV/MPV/跑车）", alias=["车型级别", "级别", "车身类型"]),
            M("msrp", "官方指导价（万元）", NUM(8, 60, 10, 1), alias=["指导价", "售价", "价格"]),
            M("range_km", "续航里程（公里）", NUM(120, 1100, 10, 0), alias=["续航", "里程", "续航里程"], type="INTEGER"),
            D("seat_count", "INTEGER", "座位数", ARR(["2", "4", "5", "6", "7"], "::int"), alias=["座位"]),
            TS_C(), UPD(),
        ]),
        Tab("veh_trims", "dim", "款型配置", 220, [
            PK(),
            D("name", "VARCHAR(100)", "款型名称",
              ARR([f"{100 + (i % 9) * 50}km {'标准版' if i % 4 == 0 else '长续航版' if i % 4 == 1 else '高性能版' if i % 4 == 2 else '旗舰版'}" for i in range(1, 221)]).replace("{i}", "{i}"),
              alias=["款型", "配置版本", "版本"]),
            FK("model_id", "veh_models", "车型ID，维表 veh_models.id → name（车型名称）；按车型分组时 JOIN 取 name", alias=["车型"]),
            M("price", "款型售价（万元）", NUM(9, 55, 10, 2), alias=["售价", "价格"]),
            D("model_year", "INTEGER", "年款", ARR(["2025", "2026"], "::int"), alias=["年款", "年型"]),
            TS_C(), UPD(),
        ]),
        Tab("veh_colors", "dim", "车色", len(COLORS), [
            PK(),
            D("name", "VARCHAR(30)", "颜色名称", ARR(COLORS), alias=["颜色", "车漆", "外观颜色"]),
            EN("tone", ["亮色", "暗色", "彩色"], "色调", alias=["色系"]),
            D("extra_fee", "NUMERIC(10,2)", "选装加价（元）", ARR(["0", "2000", "5000", "8000"], "::numeric")),
        ]),
        Tab("veh_options", "dim", "配置项", 60, [
            PK(),
            D("name", "VARCHAR(100)", "配置项名称",
              ARR(["360全景影像", "座椅加热", "座椅通风", "方向盘加热", "NOA高速领航", "城市领航辅助",
                   "自动泊车", "无线充电", "HUD抬头显示", "电动尾门", "空气悬架", "CDC电磁减振",
                   "激光雷达", "5G车联网", "杜比音响", "Nappa真皮", "隐私玻璃", "电动踏板",
                   "车顶行李架", "外后视镜记忆"] + [f"配置项{i}" for i in range(21, 61)]),
              alias=["配置", "选装", "选装件"]),
            EN("category", ["安全", "舒适", "智能座舱", "智能驾驶", "外观", "三电"], "配置类别", alias=["配置分类", "类别"]),
            EN("is_advanced", YESNO, "是否高阶配置", alias=["高阶"]),
            TS_C(),
        ]),
        Tab("veh_trim_options", "fact", "款型-配置关系", 1200, [
            PK(),
            FK("trim_id", "veh_trims", "款型ID，维表 veh_trims.id → name（款型名称）", alias=["款型"]),
            FK("option_id", "veh_options", "配置项ID，维表 veh_options.id → name（配置项名称）；按配置分组时 JOIN 取 name", alias=["配置", "配置项"]),
            EN("is_standard", YESNO, "是否标配（否则为选装）", alias=["标配", "标配/选装"]),
        ]),
        Tab("sal_regions", "dim", "销售大区", len(REGIONS), [
            PK(),
            D("name", "VARCHAR(20)", "大区名称", ARR(REGIONS), alias=["大区", "区域", "销售区域"]),
            D("manager", "VARCHAR(50)", "大区经理", ARR(["张伟", "王芳", "李强", "刘洋", "陈静", "赵磊", "孙敏"])),
            TS_C(),
        ]),
        Tab("sal_dealers", "dim", "经销商", 160, [
            PK(),
            D("name", "VARCHAR(100)", "经销商名称",
              ARR([f"{c}{'之星' if i % 3 == 0 else '弘毅' if i % 3 == 1 else '鼎盛'}汽车销售服务有限公司" for i, c in
                   enumerate([CITIES[i % len(CITIES)] for i in range(160)])]).replace("{i}", "{i}"),
              alias=["经销商", "门店", "4S店"]),
            FK("region_id", "sal_regions", "大区ID，维表 sal_regions.id → name（大区名称）；按大区分组/展示时 JOIN sal_regions 取 name", alias=["大区", "区域"]),
            D("city", "VARCHAR(30)", "所在城市", ARR(CITIES), alias=["城市"]),
            EN("level", ["4S店", "体验店", "授权服务中心"], "门店类型", alias=["门店类型", "渠道类型"]),
            M("star", "星级评分（1-5）", NUM(3, 5, 10, 1), alias=["星级", "评分"]),
            EN("status", ["营业", "装修中", "停业"], "营业状态", alias=["状态"]),
            TS_C(), UPD(),
        ]),
        Tab("sal_customers", "dim", "客户", 12000, [
            PK(),
            D("cust_no", "VARCHAR(30)", "客户编号", NO("C", 8), alias=["客户号"]),
            Col("cust_name", "VARCHAR(50)", "dimension", "客户姓名（敏感，元数据故意不定义）", gen=ARR(
                ["王伟", "李娜", "张敏", "刘洋", "陈静", "杨帆", "赵磊", "黄蓉", "周杰", "吴倩",
                 "徐强", "孙丽", "马超", "朱婷", "胡军", "郭涛", "林芳", "何平", "高远", "罗丹"] +
                [f"客户{i}" for i in range(21, 121)]), sensitive=True),
            Col("cust_phone", "VARCHAR(20)", "dimension", "客户手机号（敏感，元数据故意不定义）",
                gen="'13' || (ARRAY['0','1','5','6','7','8','9'])[({i} % 7) + 1] || lpad((({i}::bigint * 8675309) % 100000000)::text, 8, '0')", sensitive=True),
            Col("cust_id_number", "VARCHAR(20)", "dimension", "客户证件号（敏感，元数据故意不定义）",
                gen="'310' || lpad({i}::text, 15, '0')", sensitive=True),
            EN("gender", ["男", "女"], "性别"),
            D("city", "VARCHAR(30)", "常驻城市", ARR(CITIES), alias=["城市", "所在城市"]),
            EN("vip_level", ["普通", "银卡", "金卡", "黑金"], "会员等级", alias=["会员", "VIP等级", "等级"]),
            D("birthday", "DATE", "出生日期", "DATE '1965-01-01' + ({i} % 13000) * INTERVAL '1 day'", alias=["生日"]),
            TS_C("registered_at", "注册时间", alias=["注册日期"]), UPD(),
        ]),
        Tab("mfg_plants", "dim", "整车工厂", 6, [
            PK(),
            D("name", "VARCHAR(50)", "工厂名称",
              ARR(["上海临港工厂", "西安高新工厂", "常州基地", "合肥工厂", "肇庆基地", "北京亦庄工厂"]),
              alias=["工厂", "生产基地", "厂区"]),
            D("city", "VARCHAR(30)", "所在城市", ARR(["上海", "西安", "常州", "合肥", "肇庆", "北京"])),
            M("capacity_per_year", "年产能（万辆）", ARR(["30", "25", "20", "24", "15", "10"], "::numeric"), alias=["产能", "年产能"]),
            TS_C(),
        ]),
        Tab("mfg_workshops", "dim", "车间", 24, [
            PK(),
            D("name", "VARCHAR(50)", "车间名称", ARR([f"{'焊装' if i % 5 == 0 else '涂装' if i % 5 == 1 else '总装' if i % 5 == 2 else '冲压' if i % 5 == 3 else '电池' }车间{i}" for i in range(1, 25)]).replace("{i}", "{i}"), alias=["车间"]),
            FK("plant_id", "mfg_plants", "工厂ID，维表 mfg_plants.id → name（工厂名称）；按工厂分组/展示时 JOIN mfg_plants 取 name ★行级权限列（engineer 角色按 plant_id 隔离）", alias=["工厂", "厂区"]),
            TS_C(),
        ]),
        Tab("mfg_lines", "dim", "产线", 40, [
            PK(),
            D("name", "VARCHAR(50)", "产线名称", ARR([f"{'L' if i % 2 else 'K'}{i}线" for i in range(1, 41)]).replace("{i}", "{i}"), alias=["产线", "线体"]),
            FK("workshop_id", "mfg_workshops", "车间ID，维表 mfg_workshops.id → name（车间名称）；按车间分组时 JOIN 取 name", alias=["车间"]),
            EN("status", ["运行", "停机", "维护"], "产线状态", alias=["状态"]),
            TS_C(),
        ]),
        Tab("mfg_stations", "dim", "工位", 140, [
            PK(),
            D("name", "VARCHAR(50)", "工位名称", ARR([f"{'OP' if i % 4 == 0 else '内饰' if i % 4 == 1 else '底盘' if i % 4 == 2 else '终检'}{i:03d}工位" for i in range(1, 141)]).replace("{i}", "{i}").replace("{i:03d}", "lpad({i}::text,3,'0')"), alias=["工位"]),
            FK("line_id", "mfg_lines", "产线ID，维表 mfg_lines.id → name（产线名称）；按产线分组时 JOIN 取 name", alias=["产线", "线体"]),
            EN("type", ["装配", "检测", "拧紧", "焊接", "灌注"], "工位类型", alias=["类型"]),
            TS_C(),
        ]),
        Tab("mfg_shifts", "dim", "班次", 3, [
            PK(),
            D("name", "VARCHAR(20)", "班次名称", ARR(["早班", "中班", "夜班"]), alias=["班次"]),
            D("start_time", "VARCHAR(10)", "开始时刻", ARR(["08:00", "16:00", "00:00"])),
            D("hours", "INTEGER", "工时（小时）", ARR(["8"], "::int")),
        ]),
        Tab("mfg_equipment", "dim", "生产设备", 300, [
            PK(),
            D("name", "VARCHAR(80)", "设备名称", ARR([f"{'机器人' if i % 4 == 0 else '拧紧机' if i % 4 == 1 else 'AGV' if i % 4 == 2 else '视觉检测'}-{i:03d}" for i in range(1, 301)]).replace("{i}", "{i}").replace("{i:03d}", "lpad({i}::text,3,'0')"), alias=["设备", "装备"]),
            FK("workshop_id", "mfg_workshops", "车间ID，维表 mfg_workshops.id → name（车间名称）", alias=["车间"]),
            EN("status", ["正常", "故障", "保养", "停用"], "设备状态", alias=["状态"]),
            D("purchase_date", "DATE", "购置日期", "DATE '2018-01-01' + ({i} % 2500) * INTERVAL '1 day'", alias=["购置日期"]),
            TS_C(), UPD(),
        ]),
        Tab("plm_part_categories", "dim", "零件分类", 32, [
            PK(),
            D("name", "VARCHAR(60)", "分类名称",
              ARR(["动力电池", "电驱系统", "电控系统", "发动机", "变速箱", "底盘", "制动系统",
                   "转向系统", "车轮轮胎", "车身钣金", "内外饰", "座椅", "灯具", "玻璃",
                   "空调系统", "热管理", "线束", "连接器", "传感器", "控制器", "显示屏",
                   "音响", "摄像头", "雷达", "智能驾驶域控", "OTA模块", "充电系统",
                   "高压线束", "软件服务", "标准件", "密封件", "其他"]),
              alias=["零件分类", "类别", "零件类别"]),
            EN("domain", ["三电", "底盘", "车身", "电子电器", "智驾", "通用"], "所属域", alias=["域", "领域"]),
            TS_C(),
        ]),
        Tab("plm_parts", "dim", "零件主数据", 1200, [
            PK(),
            D("part_no", "VARCHAR(40)", "零件号", NO("P", 9), alias=["零件号", "件号"]),
            D("name", "VARCHAR(120)", "零件名称",
              ARR(["电池包总成", "驱动电机", "车载充电机OBC", "DCDC转换器", "空气滤清器", "制动卡钳",
                   "转向机总成", "铝合金轮毂", "前保险杠", "后备箱盖板", "主驾座椅骨架", "LED大灯总成",
                   "前挡风玻璃", "空调压缩机", "电子水泵", "发动机线束", "高速连接器", "轮速传感器",
                   "域控制器DCU", "中控屏总成", "扬声器", "前视摄像头", "角雷达", "智驾域控",
                   "T-BOX", "充电枪", "高压线束总成"] + [f"零件{i:04d}" for i in range(28, 1201)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["零件", "零件名", "部件"]),
            FK("category_id", "plm_part_categories", "零件分类ID，维表 plm_part_categories.id → name（分类名称）；按零件分类分组时 JOIN 取 name", alias=["零件分类", "类别"]),
            EN("is_safety", YESNO, "是否安全件", alias=["安全件"]),
            EN("lifecycle_status", ["在产", "试制", "停产", "淘汰"], "生命周期状态", alias=["生命周期", "状态"]),
            M("weight_kg", "单件重量（kg）", NUM(1, 180, 10, 2), alias=["重量"]),
            TS_C(), UPD(),
        ]),
        Tab("plm_materials", "dim", "原材料", 90, [
            PK(),
            D("name", "VARCHAR(80)", "材料名称",
              ARR(["铝合金6061", "高强度钢DP980", "热成型钢", "PP塑料", "ABS塑料", "真皮", "织物",
                   "锂电正极材料", "锂电负极材料", "电解液", "隔膜", "铜排", "硅钢片", "永磁体"] + [f"材料{i}" for i in range(15, 91)]),
              alias=["材料", "原材料"]),
            EN("category", ["金属", "聚合物", "化工", "纺织", "磁性材料"], "材料大类", alias=["大类"]),
            M("unit_price", "单价（元/kg）", NUM(5, 400, 10, 2), alias=["单价", "价格"]),
            TS_C(),
        ]),
        Tab("plm_suppliers", "dim", "供应商", 260, [
            PK(),
            D("name", "VARCHAR(120)", "供应商名称",
              ARR(["宁德时代", "比亚迪弗迪", "孚能科技", "国轩高科", "中创新航", "汇川技术",
                   "博世", "大陆集团", "采埃孚", "电装", "爱信", "延锋汽饰", "福耀玻璃",
                   "星宇股份", "伯特利", "拓普集团", "三花智控", "银轮股份"] + [f"{c}精工{i}号供应商" for i, c in
                   enumerate([CITIES[i % len(CITIES)] for i in range(242)])]).replace("{i}", "{i}"),
              alias=["供应商", "供货商", "厂家", "Tier1"]),
            D("city", "VARCHAR(30)", "所在城市", ARR(CITIES), alias=["城市"]),
            EN("level", ["A", "B", "C"], "供应商等级", alias=["等级", "评级"]),
            EN("status", ["合格", "限期整改", "暂停供货", "淘汰"], "合作状态", alias=["状态"]),
            TS_C(), UPD(),
        ]),
        Tab("plm_drawings", "dim", "图纸", 900, [
            PK(),
            D("drawing_no", "VARCHAR(40)", "图号", NO("DW", 8), alias=["图号"]),
            D("name", "VARCHAR(120)", "图纸名称", ARR([f"总成图-{i:04d}" for i in range(1, 901)]).replace("{i:04d}", "lpad({i}::text,4,'0')"), alias=["图纸名"]),
            FK("part_id", "plm_parts", "零件ID，维表 plm_parts.id → name（零件名称）", alias=["零件"]),
            EN("drawing_type", ["2D工程图", "3D模型", "布置图", "原理图"], "图纸类型", alias=["类型"]),
            D("version", "VARCHAR(20)", "版本", ARR(["V1.0", "V1.1", "V2.0", "V3.0"]), alias=["版本"]),
            TS_C(), UPD(),
        ]),
        Tab("fin_cost_items", "dim", "成本科目", 26, [
            PK(),
            D("name", "VARCHAR(60)", "科目名称",
              ARR(["直接材料", "直接人工", "制造费用", "模具摊销", "研发分摊", "三电成本",
                   "物流运输", "质量成本", "售后预备", "销售返利", "关税", "能源费用"] + [f"科目{i}" for i in range(13, 27)]),
              alias=["科目", "成本科目"]),
            EN("category", ["变动成本", "固定成本", "期间费用"], "成本性质", alias=["性质"]),
            TS_C(),
        ]),
        Tab("sal_price_lists", "dim", "价目表", 240, [
            PK(),
            D("list_no", "VARCHAR(30)", "价目编号", NO("PL", 7), alias=["价目号"]),
            FK("trim_id", "veh_trims", "款型ID，维表 veh_trims.id → name（款型名称）", alias=["款型"]),
            M("list_price", "价目价格（万元）", NUM(9, 56, 10, 2), alias=["价格", "指导价"]),
            EN("region_scope", REGIONS, "适用大区", alias=["大区", "适用区域"]),
            D("effective_date", "DATE", "生效日期", DAY, alias=["生效日期"]),
            EN("status", ["生效", "停用"], "状态", alias=["状态"]),
        ]),
        Tab("sal_promotions", "dim", "促销活动", 42, [
            PK(),
            D("name", "VARCHAR(120)", "活动名称",
              ARR(["春季购车节", "五一焕新礼", "618大促", "金九银十", "双11权益翻倍",
                   "年终冲量季", "老友置换季", "金融贴息月"] + [f"区域促销{i}" for i in range(9, 43)]),
              alias=["促销", "活动", "营销活动"]),
            EN("type", ["现金优惠", "置换补贴", "金融贴息", "权益赠送", "限时折扣"], "促销类型", alias=["类型"]),
            D("start_date", "DATE", "开始日期", DAY, alias=["开始", "开始日期"]),
            D("end_date", "DATE", "结束日期", "DATE '2025-10-01' + (({i} * 7) % 381) * INTERVAL '1 day' + INTERVAL '30 day'", alias=["结束", "结束日期"]),
            M("budget", "活动预算（万元）", NUM(50, 2000, 10, 0), alias=["预算"]),
        ]),
        Tab("svc_service_centers", "dim", "服务中心", 96, [
            PK(),
            D("name", "VARCHAR(120)", "服务中心名称",
              ARR([f"{c}{'悦达' if i % 3 == 0 else '安途' if i % 3 == 1 else '星辉'}服务中心{1 + i // len(CITIES)}号" for i, c in
                   enumerate([CITIES[i % len(CITIES)] for i in range(96)])]).replace("{i}", "{i}"),
              alias=["服务中心", "服务网点", "网点", "售后门店"]),
            D("city", "VARCHAR(30)", "所在城市", ARR(CITIES), alias=["城市"]),
            FK("region_id", "sal_regions", "大区ID，维表 sal_regions.id → name（大区名称）；按大区分组时 JOIN sal_regions 取 name ★行级权限列（sales 角色按 region_id 隔离）", alias=["大区", "区域"]),
            EN("level", ["中心店", "社区店", "钣喷中心"], "网点类型", alias=["类型"]),
            TS_C(), UPD(),
        ]),
        Tab("svc_mechanics", "dim", "技师", 420, [
            PK(),
            D("name", "VARCHAR(50)", "技师姓名", ARR(["张师傅", "李师傅", "王师傅", "刘师傅", "陈师傅", "杨师傅", "赵师傅", "周师傅"] + [f"技师{i:03d}" for i in range(9, 421)]).replace("{i:03d}", "lpad({i}::text,3,'0')"), alias=["技师", "维修师傅"]),
            FK("service_center_id", "svc_service_centers", "服务中心ID，维表 svc_service_centers.id → name（服务中心名称）；按网点分组时 JOIN 取 name", alias=["服务中心", "网点"]),
            EN("skill_level", ["初级", "中级", "高级", "专家"], "技能等级", alias=["等级", "技能", "职级"]),
            EN("specialty", ["机电", "钣金", "喷漆", "三电", "智驾标定"], "专业方向", alias=["专业", "专长"]),
            D("hired_at", "DATE", "入职日期", "DATE '2015-01-01' + ({i} % 3000) * INTERVAL '1 day'", alias=["入职日期"]),
        ]),
        Tab("svc_service_advisors", "dim", "服务顾问", 160, [
            PK(),
            D("name", "VARCHAR(50)", "顾问姓名", ARR(["小王", "小李", "小张", "小刘", "小陈", "小杨"] + [f"顾问{i:03d}" for i in range(7, 161)]).replace("{i:03d}", "lpad({i}::text,3,'0')"), alias=["服务顾问", "顾问"]),
            FK("service_center_id", "svc_service_centers", "服务中心ID，维表 svc_service_centers.id → name（服务中心名称）", alias=["服务中心", "网点"]),
            EN("level", ["初级", "中级", "高级"], "级别", alias=["级别"]),
        ]),
        Tab("svc_labor_items", "dim", "工时项目", 90, [
            PK(),
            D("item_no", "VARCHAR(30)", "项目编码", NO("LB", 6), alias=["工时代码"]),
            D("name", "VARCHAR(120)", "工时项目名称",
              ARR(["小保养", "大保养", "更换刹车油", "更换空调滤芯", "四轮定位", "更换轮胎",
                   "更换刹车片", "更换雨刮", "电池检测", "电机保养", "软件升级", "灯光调试",
                   "底盘检测", "空调加氟", "贴膜"] + [f"工时项目{i}" for i in range(16, 91)]),
              alias=["工时项目", "保养项目", "服务项目"]),
            M("standard_hours", "标准工时（小时）", NUM(5, 120, 10, 1), alias=["标准工时", "工时"]),
            M("price", "工时费（元）", NUM(80, 2200, 10, 0), alias=["工时费", "价格"]),
            EN("category", ["保养", "维修", "检测", "美容"], "项目类别", alias=["类别"]),
        ]),
        Tab("svc_maintenance_packages", "dim", "保养套餐", 14, [
            PK(),
            D("name", "VARCHAR(80)", "套餐名称",
              ARR(["首保免费套餐", "基础保养卡", "臻选保养卡", "尊享保养卡", "电机终身保养",
                   "电池健康守护", "轮胎无忧包", "喷漆年卡", "洗车年卡", "延保套餐基础版",
                   "延保套餐尊享版", "终身质保激活", "玻璃险套餐", "三电检测年包"]),
              alias=["套餐", "保养套餐"]),
            M("price", "套餐价格（元）", ARR(["0", "999", "1999", "3999", "5999", "9999", "15999", "19999", "899", "2999", "8999", "19999", "499", "12999"], "::numeric"), alias=["价格"]),
            M("times", "包含次数", ARR(["1", "4", "6", "8", "12", "2", "10"], "::int"), alias=["次数"]),
        ]),
        Tab("svc_warranty_policies", "dim", "质保政策", 24, [
            PK(),
            D("name", "VARCHAR(120)", "政策名称",
              ARR(["整车3年10万公里", "整车5年15万公里", "三电终身质保", "电池8年16万公里",
                   "首任车主权益", "营运车质保", "二手车质保", "延保1年", "延保2年"] + [f"政策{i}" for i in range(10, 25)]),
              alias=["质保政策", "政策", "三包政策"]),
            EN("scope", ["整车", "三电", "电池", "电机", "电控", "智驾硬件", "基础配件"], "质保范围", alias=["范围"]),
            M("months", "质保月数", ARR(["12", "24", "36", "60", "96", "1200"], "::int"), alias=["月数", "质保期"]),
            M("km_limit", "里程上限（公里）", ARR(["100000", "150000", "160000", "200000", "300000", "1000000"], "::int"), alias=["里程上限", "里程限制"]),
        ]),
        Tab("qms_fault_codes", "dim", "故障码字典（DTC）", 320, [
            PK(),
            D("dtc_code", "VARCHAR(10)", "故障码",
              ARR([f"{'P' if i % 5 == 0 else 'B' if i % 5 == 1 else 'C' if i % 5 == 2 else 'U' if i % 5 == 3 else 'U'}{1000 + i}" for i in range(1, 321)]).replace("{i}", "{i}"),
              alias=["故障码", "DTC", "诊断码"]),
            D("name", "VARCHAR(120)", "故障名称",
              ARR(["动力电池电压异常", "电机过温", "车载充电故障", "低压蓄电池亏电", "胎压异常",
                   "制动系统故障", "转向助力异常", "ADAS摄像头遮挡", "雷达信号丢失", "域控制器重启",
                   "空调压缩机过流", "热管理阀门卡滞", "整车CAN通讯丢失", "OTA升级失败",
                   "充电口电子锁故障"] + [f"故障{i:03d}" for i in range(16, 321)]).replace("{i:03d}", "lpad({i}::text,3,'0')"),
              alias=["故障名称", "故障名"]),
            EN("system", ["动力", "底盘", "车身", "智驾", "座舱", "热管理", "网络通讯"], "所属系统", alias=["系统", "所属域"]),
            EN("severity", SEVERITY, "严重程度", alias=["严重级别", "级别"]),
            EN("is_safety_related", YESNO, "是否安全相关", alias=["安全相关"]),
        ]),
        Tab("ota_ecus", "dim", "ECU 电子控制单元清单", 42, [
            PK(),
            D("name", "VARCHAR(50)", "ECU 名称",
              ARR(["VCU整车控制器", "BMS电池管理", "MCU电机控制", "ADCU智驾域控", "IHU智能座舱",
                   "TBOX远程通信", "GW网关", "BCM车身控制", "ESP车身稳定", "EPB电子驻车",
                   "ACU安全气囊", "PEPS无钥匙", "ICM仪表", "HUD抬头显示", "AVAS低速提示"] + [f"ECU-{i:02d}" for i in range(16, 43)]).replace("{i:02d}", "lpad({i}::text,2,'0')"),
              alias=["ECU", "控制器", "电控单元"]),
            EN("domain", ["动力域", "底盘域", "车身域", "智驾域", "座舱域"], "所属域", alias=["域"]),
            M("supplier_count", "供应商数量", NUM(1, 5, 1, 0), alias=["供应商数"]),
        ]),
        Tab("ota_packages", "dim", "OTA 软件包", 70, [
            PK(),
            D("version", "VARCHAR(30)", "软件版本",
              ARR([f"V{1 + i // 20}.{i % 20}.{'alpha' if i % 3 == 0 else 'beta' if i % 3 == 1 else 'release'}" for i in range(1, 71)]).replace("{i}", "{i}"),
              alias=["版本", "软件版本"]),
            FK("ecu_id", "ota_ecus", "ECU ID，维表 ota_ecus.id → name（ECU 名称）；按 ECU 分组时 JOIN 取 name", alias=["ECU", "控制器"]),
            EN("release_type", ["正式版", "灰度版", "内测版"], "发布类型", alias=["类型"]),
            M("size_mb", "包大小（MB）", NUM(50, 4200, 10, 0), alias=["大小"]),
            D("released_at", "TIMESTAMP", "发布时间", TS, alias=["发布日期", "发布时间"]),
        ]),
        Tab("ad_scenarios", "dim", "智驾测试场景库", 520, [
            PK(),
            D("code", "VARCHAR(30)", "场景编号", NO("SC", 6), alias=["场景编号"]),
            D("name", "VARCHAR(160)", "场景名称",
              ARR(["雨天高速自动变道", "夜间无灯城市路口", "隧道内切入cut-in", "环岛多出口选择",
                   "施工路段借道", "鬼探头行人横穿", "大车侧风补偿", "拥堵跟车启停",
                   "自动泊车窄位", "记忆泊车跨层", "匝道汇入主路", "收费站通过",
                   "红灯右转礼让", "环岛让行", "夜间会车眩光"] + [f"场景{i:04d}" for i in range(16, 521)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["场景", "测试场景"]),
            EN("road_type", ["高速", "城市", "隧道", "泊车", "乡村", "环岛"], "道路类型", alias=["道路", "路况"]),
            EN("weather", ["晴天", "雨天", "雪天", "雾天", "夜间"], "天气条件", alias=["天气"]),
            EN("risk_level", ["低", "中", "高"], "风险等级", alias=["风险", "等级"]),
        ]),
        Tab("ad_test_vehicles", "dim", "智驾测试车队", 60, [
            PK(),
            D("plate_no", "VARCHAR(15)", "测试车牌号", ARR([f"沪A{10000 + i}测" for i in range(1, 61)]).replace("{i}", "{i}"), alias=["车牌", "测试车牌"]),
            FK("model_id", "veh_models", "车型ID，维表 veh_models.id → name（车型名称）", alias=["车型"]),
            EN("level", ["L2", "L2+", "L3"], "智驾等级", alias=["智驾等级", "等级"]),
            EN("status", ["在役", "改装中", "退役"], "车辆状态", alias=["状态"]),
            TS_C(),
        ]),
        Tab("alm_engineers", "dim", "研发工程师", 320, [
            PK(),
            D("name", "VARCHAR(50)", "工程师姓名", ARR(["工程师小张", "工程师小李", "工程师小王", "工程师小赵"] + [f"工程师{i:03d}" for i in range(5, 321)]).replace("{i:03d}", "lpad({i}::text,3,'0')"), alias=["工程师", "负责人", "责任人"]),
            EN("domain", ["动力域", "底盘域", "车身域", "智驾域", "座舱域", "热管理域", "软件平台域"], "所属域", alias=["域", "领域"]),
            EN("level", ["初级", "中级", "高级", "资深"], "职级", alias=["职级", "级别"]),
            D("joined_at", "DATE", "入职日期", "DATE '2018-01-01' + ({i} % 2600) * INTERVAL '1 day'", alias=["入职日期"]),
        ]),
        Tab("alm_projects", "dim", "研发项目", 42, [
            PK(),
            D("project_no", "VARCHAR(30)", "项目编号", NO("PRJ", 6), alias=["项目号"]),
            D("name", "VARCHAR(120)", "项目名称",
              ARR([f"{b}{'换代' if i % 4 == 0 else '年款' if i % 4 == 1 else '改款' if i % 4 == 2 else '预研'}项目" for i, b in
                   enumerate([BRANDS[i % len(BRANDS)] for i in range(42)])]).replace("{i}", "{i}"),
              alias=["项目"]),
            EN("stage", ["概念", "设计", "开发", "验证", "量产"], "项目阶段", alias=["阶段"]),
            FK("plant_id", "mfg_plants", "落地工厂ID，维表 mfg_plants.id → name（工厂名称）★行级权限列（engineer 角色按 plant_id 隔离）", alias=["工厂"]),
            TS_C("started_at", "立项时间", alias=["立项日期"]), UPD(),
        ]),
    ]

    # ══════════════════════ ALM 研发域（12）══════════════════════
    t += [
        Tab("alm_requirements", "fact", "产品需求", 6500, [
            PK(),
            D("req_no", "VARCHAR(30)", "需求编号", NO("REQ", 7), alias=["需求号", "需求单号"]),
            D("title", "VARCHAR(300)", "需求标题",
              ARR(["支持 CarPlay 互联", "新增露营模式", "哨兵模式优化", "语音免唤醒", "座椅记忆功能",
                   "HUD 显示自定义", "能量回收调节", "手机 App 远程空调", "对外放电 V2L",
                   "无线 CarPlay", "自动雨刮灵敏度", "后视镜自动防眩目"] + [f"需求条目{i:04d}" for i in range(13, 6501)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["标题", "需求名"]),
            FK("project_id", "alm_projects", "项目ID，维表 alm_projects.id → name（项目名称）；按项目分组时 JOIN 取 name", alias=["项目"]),
            EN("priority", PRIORITIES, "优先级", alias=["优先级", "重要程度"]),
            EN("status", ["草稿", "评审中", "已批准", "已实现", "已取消"], "需求状态", alias=["状态"]),
            D("source", "VARCHAR(60)", "需求来源", ARR(["用户反馈", "竞品分析", "法规要求", "内部规划", "老板需求", "经销商建议"]), alias=["来源"]),
            TS_C(), UPD(),
        ]),
        Tab("alm_requirement_links", "fact", "需求追溯关系", 14000, [
            PK(),
            FK("requirement_id", "alm_requirements", "需求ID，维表 alm_requirements.id → req_no（需求编号）", alias=["需求"]),
            FK("test_case_id", "alm_test_cases", "测试用例ID，维表 alm_test_cases.id → case_no（用例编号）", alias=["测试用例", "用例"]),
            EN("link_type", ["追溯", "验证", "派生"], "追溯类型", alias=["类型"]),
        ]),
        Tab("alm_change_requests", "fact", "变更请求（CR）", 4200, [
            PK(),
            D("cr_no", "VARCHAR(30)", "变更编号", NO("CR", 7), alias=["CR号", "变更号"]),
            D("title", "VARCHAR(300)", "变更标题",
              ARR(["电池供应商切换", "内饰配色调整", "智驾算法版本升级", "底盘件降本替代", "车机系统改版",
                   "充电接口国标切换", "新增选装配置", "召回相关设计变更"] + [f"变更事项{i:04d}" for i in range(9, 4201)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["标题", "变更名"]),
            FK("project_id", "alm_projects", "项目ID，维表 alm_projects.id → name（项目名称）；按项目分组时 JOIN 取 name", alias=["项目"]),
            EN("status", ["待评审", "已批准", "已实施", "已驳回"], "变更状态", alias=["状态"]),
            EN("change_type", ["设计变更", "工艺变更", "软件变更", "配置变更"], "变更类型", alias=["类型"]),
            EN("impact_level", ["低", "中", "高"], "影响等级", alias=["影响", "等级"]),
            TS_C(), UPD(),
        ]),
        Tab("alm_baselines", "dim", "基线", 320, [
            PK(),
            D("baseline_no", "VARCHAR(30)", "基线编号", NO("BL", 6), alias=["基线号"]),
            D("name", "VARCHAR(150)", "基线名称", ARR([f"{'软件' if i % 3 == 0 else '整车' if i % 3 == 1 else '硬件'}基线-V{1 + i // 10}.{i % 10}" for i in range(1, 321)]).replace("{i}", "{i}"), alias=["基线名"]),
            FK("project_id", "alm_projects", "项目ID，维表 alm_projects.id → name（项目名称）", alias=["项目"]),
            EN("is_frozen", YESNO, "是否冻结", alias=["冻结", "冻结状态"]),
            D("release_date", "DATE", "发布日期", DAY, alias=["发布日期"]),
            TS_C(),
        ]),
        Tab("alm_issues", "fact", "缺陷问题单（研发）", 32000, [
            PK(),
            D("issue_no", "VARCHAR(30)", "问题单编号", NO("ISS", 8), alias=["问题单号", "缺陷号", "单号"]),
            D("title", "VARCHAR(400)", "问题标题",
              ARR(["中控屏偶发黑屏", "充电枪无法拔出", "低速制动异响", "空调制冷不足", "车机启动慢",
                   "蓝牙断连", "座椅调节卡滞", "尾灯进水", "APP 无法解锁", "智驾误报警",
                   "雨刮不回位", "方向盘偏右"] + [f"缺陷问题{i:05d}" for i in range(13, 32001)]).replace("{i:05d}", "lpad({i}::text,5,'0')"),
              alias=["标题", "问题名"]),
            FK("project_id", "alm_projects", "项目ID，维表 alm_projects.id → name（项目名称）；按项目分组时 JOIN 取 name", alias=["项目"]),
            EN("source", ["路试", "台架", "生产线", "用户反馈", "市场故障", "审核发现"], "问题来源", alias=["来源"]),
            EN("severity", SEVERITY, "严重程度", alias=["严重级别", "级别"]),
            Col("status", "VARCHAR(20)", "dimension",
                "问题状态，枚举值固定为英文：open(未处理)/analyzing(分析中)/fixing(修复中)/closed(已关闭)/verified(已验证)，过滤时必须用英文值如 status='closed'",
                alias=["状态", "关闭", "未关闭", "已关闭"], sync=True,
                gen=ARR(["open", "analyzing", "fixing", "closed", "verified"], "::varchar")),
            FK("owner_engineer_id", "alm_engineers", "责任工程师ID，维表 alm_engineers.id → name（工程师姓名）；按工程师分组时 JOIN 取 name", alias=["责任人", "工程师", "责任工程师"]),
            FK("root_cause_id", "alm_defect_root_causes", "根因ID，维表 alm_defect_root_causes.id → name（根因分类），可为空", alias=["根因"], nullable=True),
            FK("model_id", "veh_models", "车型ID，维表 veh_models.id → name（车型名称）；按车型分组时 JOIN 取 name", alias=["车型"]),
            TS_C(), UPD(),
        ]),
        Tab("alm_defect_root_causes", "dim", "缺陷根因分类", 8000, [
            PK(),
            D("name", "VARCHAR(120)", "根因描述",
              ARR(["软件逻辑缺陷", "软件配置错误", "设计余量不足", "公差配合不当", "来料批次不良",
                   "装配工艺不当", "标定参数错误", "散热设计缺陷", "线束干涉", "密封不良"] + [f"根因{i:04d}" for i in range(11, 8001)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["根因", "根因分类"]),
            EN("category", ["设计", "工艺", "软件", "来料", "装配", "标定"], "根因大类", alias=["大类"]),
            D("corrective_action", "VARCHAR(300)", "纠正措施描述",
              ARR(["优化软件版本", "修改图纸公差", "加强来料检验", "调整工装夹具", "重新标定",
                   "更换供应商批次", "增加防错工位", "更新作业指导书"]), alias=["纠正措施", "措施"]),
            EN("status", ["进行中", "已完成", "已验证"], "措施状态", alias=["状态"]),
            TS_C(),
        ]),
        Tab("alm_test_cases", "fact", "测试用例", 8500, [
            PK(),
            D("case_no", "VARCHAR(30)", "用例编号", NO("TC", 7), alias=["用例号"]),
            D("title", "VARCHAR(300)", "用例标题",
              ARR(["高速 120kph 巡航", "城市路口左转", "自动泊车垂直车位", "雨天 AEB 触发",
                   "语音打开空调", "手机钥匙解锁", "充电桩兼容性", "OTA 断点续传"] + [f"测试用例{i:04d}" for i in range(9, 8501)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["标题", "用例名"]),
            FK("project_id", "alm_projects", "项目ID，维表 alm_projects.id → name（项目名称）；按项目分组时 JOIN 取 name", alias=["项目"]),
            EN("case_type", ["单元", "集成", "系统", "回归", "实车"], "用例类型", alias=["类型"]),
            EN("priority", PRIORITIES, "优先级", alias=["优先级"]),
            EN("status", ["有效", "废弃"], "用例状态", alias=["状态"]),
            TS_C(), UPD(),
        ]),
        Tab("alm_test_plans", "dim", "测试计划", 220, [
            PK(),
            D("plan_no", "VARCHAR(30)", "计划编号", NO("TP", 6), alias=["计划号"]),
            D("name", "VARCHAR(150)", "计划名称", ARR([f"{q}季{'集成' if i % 4 == 0 else '系统' if i % 4 == 1 else '实车' if i % 4 == 2 else '回归'}测试计划" for i, q in
                                                        enumerate(["春", "夏", "秋", "冬"] * 60)]).replace("{i}", "{i}"), alias=["计划名"]),
            FK("project_id", "alm_projects", "项目ID，维表 alm_projects.id → name（项目名称）", alias=["项目"]),
            EN("phase", ["SVP", "DV", "PV", "MP"], "测试阶段", alias=["阶段"]),
            D("start_date", "DATE", "开始日期", DAY, alias=["开始日期"]),
            D("end_date", "DATE", "结束日期", "DATE '2025-10-01' + (({i} * 7) % 381) * INTERVAL '1 day' + INTERVAL '60 day'", alias=["结束日期"]),
        ]),
        Tab("alm_test_executions", "fact", "测试执行记录", 62000, [
            PK(),
            FK("test_case_id", "alm_test_cases", "测试用例ID，维表 alm_test_cases.id → case_no（用例编号）；按用例分组时 JOIN 取 case_no", alias=["测试用例", "用例"]),
            FK("test_plan_id", "alm_test_plans", "测试计划ID，维表 alm_test_plans.id → name（计划名称）；按计划分组时 JOIN 取 name", alias=["测试计划", "计划"]),
            EN("result", ["通过", "失败", "阻塞", "跳过"], "执行结果", alias=["结果", "通过/失败"]),
            M("duration_s", "执行耗时（秒）", NUM(10, 7200, 10, 0), alias=["耗时", "执行时长"], type="INTEGER"),
            FK("engineer_id", "alm_engineers", "执行工程师ID，维表 alm_engineers.id → name（工程师姓名）", alias=["执行人", "工程师"]),
            D("executed_at", "TIMESTAMP", "执行时间", TS, alias=["执行日期", "时间"]),
            D("environment", "VARCHAR(60)", "测试环境", ARR(["台架-01", "台架-02", "HIL", "试验场", "公开道路", "园区道路"]), alias=["环境"]),
        ]),
        Tab("alm_release_versions", "dim", "版本发布记录", 160, [
            PK(),
            D("version_no", "VARCHAR(30)", "版本号", ARR([f"R{1 + i // 20}.{i % 20}" for i in range(1, 161)]).replace("{i}", "{i}"), alias=["版本号"]),
            FK("project_id", "alm_projects", "项目ID，维表 alm_projects.id → name（项目名称）", alias=["项目"]),
            EN("release_type", ["大版本", "小版本", "热修复"], "发布类型", alias=["类型"]),
            D("release_date", "DATE", "发布日期", DAY, alias=["发布日期"]),
            EN("is_golden", YESNO, "是否金版本（基线版本）", alias=["金版本"]),
        ]),
        Tab("alm_sprints", "dim", "迭代", 460, [
            PK(),
            D("sprint_no", "VARCHAR(30)", "迭代编号", ARR([f"Sprint-{i:03d}" for i in range(1, 461)]).replace("{i:03d}", "lpad({i}::text,3,'0')"), alias=["迭代号", "冲刺"]),
            FK("project_id", "alm_projects", "项目ID，维表 alm_projects.id → name（项目名称）", alias=["项目"]),
            D("start_date", "DATE", "开始日期", "DATE '2025-01-05' + ({i} % 78) * INTERVAL '14 day'", alias=["开始日期"]),
            D("end_date", "DATE", "结束日期", "DATE '2025-01-19' + ({i} % 78) * INTERVAL '14 day'", alias=["结束日期"]),
            EN("status", ["计划中", "进行中", "已完成"], "迭代状态", alias=["状态"]),
        ]),
        Tab("alm_ci_records", "fact", "持续集成构建记录", 26000, [
            PK(),
            D("build_no", "VARCHAR(40)", "构建编号", NO("BUILD", 10), alias=["构建号"]),
            FK("project_id", "alm_projects", "项目ID，维表 alm_projects.id → name（项目名称）；按项目分组时 JOIN 取 name", alias=["项目"]),
            EN("result", ["成功", "失败", "取消"], "构建结果", alias=["结果"]),
            M("duration_min", "构建时长（分钟）", NUM(2, 180, 10, 1), alias=["时长", "构建耗时"]),
            EN("branch", ["main", "develop", "release", "hotfix"], "代码分支", alias=["分支"]),
            TS_C("triggered_at", "触发时间", alias=["触发时间", "时间"]),
        ]),
    ]

    # ══════════════════════ PLM/BOM/采购域（9）══════════════════════
    t += [
        Tab("plm_bom_headers", "dim", "BOM 清单头", 320, [
            PK(),
            D("bom_no", "VARCHAR(30)", "BOM 编号", NO("BOM", 6), alias=["BOM号"]),
            FK("model_id", "veh_models", "车型ID，维表 veh_models.id → name（车型名称）；按车型分组时 JOIN 取 name", alias=["车型"]),
            EN("bom_type", ["设计BOM", "工艺BOM", "制造BOM", "售后BOM"], "BOM 类型", alias=["类型"]),
            D("version", "VARCHAR(20)", "版本", ARR(["V1.0", "V2.0", "V3.0", "V4.0"]), alias=["版本"]),
            TS_C(), UPD(),
        ]),
        Tab("plm_bom_items", "fact", "BOM 明细行", 9000, [
            PK(),
            FK("bom_id", "plm_bom_headers", "BOM 头ID，维表 plm_bom_headers.id → bom_no（BOM 编号）", alias=["BOM"]),
            FK("part_id", "plm_parts", "零件ID，维表 plm_parts.id → name（零件名称）；按零件分组时 JOIN 取 name", alias=["零件"]),
            M("quantity", "单车用量", NUM(1, 8, 1, 0), alias=["用量", "数量"], type="INTEGER"),
            D("unit", "VARCHAR(10)", "计量单位", ARR(["个", "件", "套", "米", "kg"]), alias=["单位"]),
        ]),
        Tab("plm_engineering_changes", "fact", "工程变更（ECN）", 3200, [
            PK(),
            D("ecn_no", "VARCHAR(30)", "ECN 编号", NO("ECN", 7), alias=["ECN号", "工程变更号"]),
            D("title", "VARCHAR(300)", "变更标题",
              ARR(["电池包结构优化", "线束走向调整", "支架材料更换", "焊接工艺变更", "软件策略更新",
                   "降本替代方案"] + [f"工程变更{i:04d}" for i in range(7, 3201)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["标题"]),
            FK("part_id", "plm_parts", "涉及零件ID，维表 plm_parts.id → name（零件名称）；按零件分组时 JOIN 取 name", alias=["零件"]),
            EN("reason", ["质量改进", "降本", "法规符合", "停产替代", "性能优化"], "变更原因", alias=["原因"]),
            EN("status", ["评审中", "已生效", "已取消"], "状态", alias=["状态"]),
            TS_C(), UPD(),
        ]),
        Tab("plm_part_costs", "fact", "零件成本记录", 1200, [
            PK(),
            FK("part_id", "plm_parts", "零件ID，维表 plm_parts.id → name（零件名称）；按零件分组时 JOIN 取 name", alias=["零件"]),
            M("cost", "单件成本（元）", NUM(5, 48000, 10, 2), alias=["成本", "单价", "零件成本"]),
            D("currency", "VARCHAR(10)", "币种", ARR(["CNY"])),
            D("effective_date", "DATE", "生效日期", DAY, alias=["生效日期"]),
        ]),
        Tab("plm_supplier_parts", "fact", "供应商供货关系", 5200, [
            PK(),
            FK("supplier_id", "plm_suppliers", "供应商ID，维表 plm_suppliers.id → name（供应商名称）；按供应商分组时 JOIN 取 name", alias=["供应商", "厂家"]),
            FK("part_id", "plm_parts", "零件ID，维表 plm_parts.id → name（零件名称）；按零件分组时 JOIN 取 name", alias=["零件"]),
            M("price", "供货价（元）", NUM(5, 52000, 10, 2), alias=["价格", "供货价"]),
            EN("is_primary", YESNO, "是否主供", alias=["主供"]),
            M("lead_time_days", "供货周期（天）", NUM(7, 90, 1, 0), alias=["周期", "供货周期", "交期"], type="INTEGER"),
        ]),
        Tab("plm_supplier_ratings", "fact", "供应商季度评级", 2400, [
            PK(),
            FK("supplier_id", "plm_suppliers", "供应商ID，维表 plm_suppliers.id → name（供应商名称）；按供应商分组时 JOIN 取 name", alias=["供应商", "厂家"]),
            D("period", "VARCHAR(10)", "评级期间（季度）", ARR(["2025Q3", "2025Q4", "2026Q1", "2026Q2", "2026Q3"]), alias=["期间", "季度"]),
            M("quality_score", "质量得分", NUM(60, 100, 10, 1), alias=["质量分"]),
            M("delivery_score", "交付得分", NUM(60, 100, 10, 1), alias=["交付分"]),
            M("total_score", "综合得分", NUM(60, 100, 10, 1), alias=["综合得分", "总分"]),
            EN("grade", ["A", "B", "C"], "评级", alias=["等级", "评级"]),
        ]),
        Tab("pur_purchase_orders", "fact", "采购订单头", 15000, [
            PK(),
            D("po_no", "VARCHAR(30)", "采购订单号", NO("PO", 8), alias=["PO号", "采购单号"]),
            FK("supplier_id", "plm_suppliers", "供应商ID，维表 plm_suppliers.id → name（供应商名称）；按供应商分组时 JOIN 取 name", alias=["供应商", "厂家"]),
            EN("status", ["草稿", "已审批", "部分到货", "已到齐", "已关闭"], "订单状态", alias=["状态"]),
            D("buyer", "VARCHAR(50)", "采购员", ARR(["采购员小王", "采购员小李", "采购员小张", "采购员小陈"] + [f"采购员{i:03d}" for i in range(5, 41)]).replace("{i:03d}", "lpad({i}::text,3,'0')"), alias=["采购员", "买手"]),
            TS_C(), UPD(),
        ]),
        Tab("pur_po_lines", "fact", "采购订单行", 38000, [
            PK(),
            FK("po_id", "pur_purchase_orders", "采购订单头ID，维表 pur_purchase_orders.id → po_no（采购订单号）", alias=["采购订单", "PO"]),
            FK("part_id", "plm_parts", "零件ID，维表 plm_parts.id → name（零件名称）；按零件分组时 JOIN 取 name", alias=["零件"]),
            M("quantity", "采购数量", NUM(100, 50000, 1, 0), alias=["数量"], type="INTEGER"),
            M("unit_price", "含税单价（元）", NUM(5, 52000, 10, 2), alias=["单价", "价格"]),
            M("amount", "行金额（元）= 数量 × 含税单价",
              "round((({i} * 37 % 49900) + 100) * ((({i} * 37 % 51995) + 5) / 10.0), 2)", alias=["金额", "总价"]),
        ]),
        Tab("pur_inbound_receipts", "fact", "采购入库单", 30000, [
            PK(),
            D("receipt_no", "VARCHAR(30)", "入库单号", NO("RC", 8), alias=["入库单号", "收货单号"]),
            FK("po_id", "pur_purchase_orders", "采购订单头ID，维表 pur_purchase_orders.id → po_no（采购订单号）", alias=["采购订单", "PO"]),
            FK("part_id", "plm_parts", "零件ID，维表 plm_parts.id → name（零件名称）", alias=["零件"]),
            M("quantity", "到货数量", NUM(50, 30000, 1, 0), alias=["数量", "到货数量"], type="INTEGER"),
            M("qualified_qty", "合格数量", "round((({i} * 37 % 29500) + 50) / 1.0, 0)", alias=["合格数量"], type="INTEGER"),
            EN("inspect_result", ["合格", "让步接收", "退货"], "检验结论", alias=["检验结果", "结论"]),
            D("arrival_date", "DATE", "到货日期", DAY, alias=["到货日期", "时间"]),
        ]),
    ]

    # ══════════════════════ 整车档案（1，全局事实锚点）══════════════════════
    t += [
        Tab("veh_vehicles", "fact", "整车档案（一辆车一行，VIN 唯一）", 30000, [
            PK(),
            Col("vin", "VARCHAR(20)", "dimension", "车架号（敏感，元数据故意不定义）", gen=VIN(), sensitive=True),
            FK("trim_id", "veh_trims", "款型ID，维表 veh_trims.id → name（款型名称）；按款型分组时 JOIN 取 name", alias=["款型", "配置"]),
            FK("color_id", "veh_colors", "颜色ID，维表 veh_colors.id → name（颜色名称）；按颜色分组时 JOIN 取 name", alias=["颜色", "车色"]),
            FK("plant_id", "mfg_plants", "下线工厂ID，维表 mfg_plants.id → name（工厂名称）；按工厂分组时 JOIN 取 name ★行级权限列（engineer 角色按 plant_id 隔离）", alias=["工厂", "生产基地", "下线工厂"]),
            D("build_date", "DATE", "下线日期", DAY, alias=["下线日期", "生产日期", "出厂日期"]),
            EN("status", ["在产", "在库", "已售", "已退役"], "车辆状态", alias=["状态", "车辆状态"]),
            M("msrp", "该配置售价（万元）", NUM(9, 56, 10, 2), alias=["售价", "指导价", "车价"]),
        ]),
    ]

    # ══════════════════════ 生产制造域（10）══════════════════════
    t += [
        Tab("mfg_vehicle_builds", "fact", "整车下线记录", 30000, [
            PK(),
            D("build_no", "VARCHAR(30)", "下线编号", NO("BLD", 8), alias=["下线号", "车身号"]),
            FK("vehicle_id", "veh_vehicles", "整车ID，维表 veh_vehicles.id → vin（车架号）", alias=["整车", "车辆"]),
            FK("line_id", "mfg_lines", "产线ID，维表 mfg_lines.id → name（产线名称）；按产线分组时 JOIN 取 name", alias=["产线", "线体"]),
            FK("shift_id", "mfg_shifts", "班次ID，维表 mfg_shifts.id → name（班次名称）；按班次分组时 JOIN 取 name", alias=["班次"]),
            EN("result", ["一次合格", "返修后合格", "待检"], "下线结果", alias=["结果"]),
            D("build_date", "DATE", "下线日期", DAY, alias=["下线日期", "日期"]),
        ]),
        Tab("mfg_work_orders", "fact", "生产工单", 6000, [
            PK(),
            D("wo_no", "VARCHAR(30)", "生产工单号", NO("MWO", 7), alias=["工单号", "生产单号"]),
            FK("plant_id", "mfg_plants", "工厂ID，维表 mfg_plants.id → name（工厂名称）；按工厂分组时 JOIN 取 name ★行级权限列", alias=["工厂"]),
            FK("line_id", "mfg_lines", "产线ID，维表 mfg_lines.id → name（产线名称）", alias=["产线"]),
            FK("model_id", "veh_models", "车型ID，维表 veh_models.id → name（车型名称）；按车型分组时 JOIN 取 name", alias=["车型"]),
            M("plan_qty", "计划数量", NUM(50, 600, 1, 0), alias=["计划量"], type="INTEGER"),
            M("actual_qty", "实际数量", "round((({i} * 37 % 500) + 40) / 1.0, 0)", alias=["实际量", "产量"], type="INTEGER"),
            EN("status", ["待排产", "生产中", "已完工", "已关闭"], "工单状态", alias=["状态"]),
            D("start_date", "DATE", "开始日期", DAY, alias=["开始日期"]),
            D("end_date", "DATE", "完工日期", "CASE WHEN {i} % 5 = 0 THEN NULL ELSE DATE '2025-09-01' + (({i} * 7) % 381) * INTERVAL '1 day' + INTERVAL '5 day' END", alias=["完工日期", "结束日期"]),
        ]),
        Tab("mfg_production_records", "fact", "生产产量记录（产线×班次×日）", 32000, [
            PK(),
            D("record_date", "DATE", "生产日期", DAY, alias=["生产日期", "日期"]),
            FK("plant_id", "mfg_plants", "工厂ID，维表 mfg_plants.id → name（工厂名称）；按工厂分组时 JOIN 取 name ★行级权限列", alias=["工厂"]),
            FK("line_id", "mfg_lines", "产线ID，维表 mfg_lines.id → name（产线名称）", alias=["产线"]),
            FK("shift_id", "mfg_shifts", "班次ID，维表 mfg_shifts.id → name（班次名称）", alias=["班次"]),
            FK("model_id", "veh_models", "车型ID，维表 veh_models.id → name（车型名称）", alias=["车型"]),
            M("plan_qty", "计划产量", NUM(20, 320, 1, 0), alias=["计划产量"], type="INTEGER"),
            M("actual_qty", "实际产量", "round((({i} * 37 % 300) + 18) / 1.0, 0)", alias=["实际产量", "产量"], type="INTEGER"),
            M("oee", "设备综合效率 OEE（0-1）", NUM(55, 96, 100, 2), alias=["OEE", "综合效率"], type="NUMERIC(6,4)"),
        ]),
        Tab("mfg_station_defects", "fact", "工位缺陷记录", 42000, [
            PK(),
            FK("build_id", "mfg_vehicle_builds", "下线记录ID，维表 mfg_vehicle_builds.id → build_no（下线编号）", alias=["下线记录"]),
            FK("station_id", "mfg_stations", "工位ID，维表 mfg_stations.id → name（工位名称）；按工位分组时 JOIN 取 name", alias=["工位"]),
            EN("defect_type", ["螺栓扭矩", "间隙面差", "漆面缺陷", "焊接缺陷", "装配错漏", "划伤", "脏污"], "缺陷类型", alias=["类型", "缺陷类别"]),
            EN("severity", SEVERITY, "严重程度", alias=["严重级别", "级别"]),
            EN("status", ["未处理", "返修中", "已闭环"], "处理状态", alias=["状态"]),
            D("found_at", "TIMESTAMP", "发现时间", TS, alias=["发现时间", "时间"]),
        ]),
        Tab("mfg_quality_inspections", "fact", "整车质检记录", 52000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "整车ID，维表 veh_vehicles.id → vin（车架号）", alias=["整车", "车辆"]),
            FK("station_id", "mfg_stations", "工位ID，维表 mfg_stations.id → name（工位名称）", alias=["工位"]),
            EN("inspection_type", ["下线检测", "抽检", "终检", "审核"], "检验类型", alias=["类型", "检验类别"]),
            EN("result", ["合格", "返修", "报废"], "检验结论", alias=["结论", "结果"]),
            D("inspector", "VARCHAR(50)", "检验员", ARR(["质检员小张", "质检员小李", "质检员小王"] + [f"质检员{i:03d}" for i in range(4, 61)]).replace("{i:03d}", "lpad({i}::text,3,'0')"), alias=["检验员"]),
            D("inspected_at", "TIMESTAMP", "检验时间", TS, alias=["检验时间", "时间"]),
        ]),
        Tab("mfg_rework_records", "fact", "返修记录", 8500, [
            PK(),
            D("rework_no", "VARCHAR(30)", "返修单号", NO("RW", 7), alias=["返修号"]),
            FK("inspection_id", "mfg_quality_inspections", "质检记录ID，维表 mfg_quality_inspections.id", alias=["质检记录"]),
            FK("vehicle_id", "veh_vehicles", "整车ID，维表 veh_vehicles.id → vin（车架号）", alias=["整车", "车辆"]),
            EN("defect_type", ["螺栓扭矩", "间隙面差", "漆面缺陷", "焊接缺陷", "装配错漏", "划伤", "脏污"], "返修缺陷类型", alias=["类型"]),
            M("rework_hours", "返修工时（小时）", NUM(1, 24, 10, 1), alias=["工时", "返修工时"]),
            EN("status", ["返修中", "已完成", "已验证"], "状态", alias=["状态"]),
            D("finished_at", "TIMESTAMP", "完工时间", TS2, alias=["完工时间"]),
        ]),
        Tab("mfg_equipment_faults", "fact", "设备故障记录", 5200, [
            PK(),
            FK("equipment_id", "mfg_equipment", "设备ID，维表 mfg_equipment.id → name（设备名称）；按设备分组时 JOIN 取 name", alias=["设备"]),
            EN("fault_type", ["机械故障", "电气故障", "程序故障", "通讯故障", "气路故障"], "故障类型", alias=["类型"]),
            M("downtime_min", "停机时长（分钟）", NUM(10, 2400, 1, 0), alias=["停机时长", "停机"], type="INTEGER"),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
            D("repaired_at", "TIMESTAMP", "修复时间", TS2, alias=["修复时间"]),
            EN("level", ["一般", "较大", "重大"], "故障等级", alias=["等级"]),
        ]),
        Tab("mfg_material_consumptions", "fact", "物料消耗记录", 60000, [
            PK(),
            FK("work_order_id", "mfg_work_orders", "生产工单ID，维表 mfg_work_orders.id → wo_no（生产工单号）", alias=["生产工单", "工单"]),
            FK("part_id", "plm_parts", "零件ID，维表 plm_parts.id → name（零件名称）；按零件分组时 JOIN 取 name", alias=["零件", "物料"]),
            M("quantity", "消耗数量", NUM(10, 800, 1, 0), alias=["数量", "消耗量"], type="INTEGER"),
            M("scrap_qty", "报废数量", "round((({i} * 37 % 8) + 0) / 1.0, 0)", alias=["报废量"], type="INTEGER"),
            D("consumed_at", "TIMESTAMP", "消耗时间", TS, alias=["时间"]),
        ]),
        Tab("mfg_andon_events", "fact", "安灯事件（产线异常呼叫）", 9000, [
            PK(),
            FK("station_id", "mfg_stations", "工位ID，维表 mfg_stations.id → name（工位名称）；按工位分组时 JOIN 取 name", alias=["工位"]),
            FK("line_id", "mfg_lines", "产线ID，维表 mfg_lines.id → name（产线名称）", alias=["产线"]),
            EN("type", ["设备", "质量", "物料", "人员"], "安灯类型", alias=["类型"]),
            M("duration_min", "持续时长（分钟）", NUM(1, 240, 1, 0), alias=["时长"], type="INTEGER"),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
        ]),
        Tab("mfg_energy_usages", "fact", "工厂能耗记录", 9000, [
            PK(),
            FK("plant_id", "mfg_plants", "工厂ID，维表 mfg_plants.id → name（工厂名称）；按工厂分组时 JOIN 取 name ★行级权限列", alias=["工厂"]),
            D("usage_date", "DATE", "能耗日期", DAY, alias=["日期"]),
            EN("energy_type", ["电", "天然气", "水", "压缩空气", "蒸汽"], "能源类型", alias=["能源", "类型"]),
            M("usage", "用量", NUM(100, 90000, 10, 1), alias=["用量", "消耗"]),
            M("cost", "费用（元）", NUM(500, 260000, 10, 2), alias=["费用", "成本"]),
        ]),
    ]

    # ══════════════════════ QMS 质量域（9）══════════════════════
    t += [
        Tab("qms_audits", "fact", "体系/过程审核", 400, [
            PK(),
            D("audit_no", "VARCHAR(30)", "审核编号", NO("AU", 6), alias=["审核号"]),
            EN("audit_type", ["体系审核", "过程审核", "产品审核"], "审核类型", alias=["类型"]),
            FK("plant_id", "mfg_plants", "工厂ID，维表 mfg_plants.id → name（工厂名称）；按工厂分组时 JOIN 取 name ★行级权限列", alias=["工厂"]),
            D("audit_date", "DATE", "审核日期", DAY, alias=["审核日期", "日期"]),
            M("score", "审核得分", NUM(60, 100, 10, 1), alias=["得分", "分数"]),
            EN("conclusion", ["通过", "有条件通过", "不通过"], "审核结论", alias=["结论"]),
        ]),
        Tab("qms_audit_findings", "fact", "审核发现项", 2600, [
            PK(),
            FK("audit_id", "qms_audits", "审核ID，维表 qms_audits.id → audit_no（审核编号）", alias=["审核"]),
            EN("severity", SEVERITY, "不符合严重度", alias=["严重度", "级别"]),
            D("description", "VARCHAR(400)", "发现描述",
              ARR(["作业指导书未更新", "量具超期未校准", "现场5S不合格", "记录填写不完整",
                   "防错装置失效", "参数超出控制限", "标识缺失", "追溯记录断档"] + [f"发现项{i:04d}" for i in range(9, 2601)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["描述", "问题描述"]),
            EN("status", ["待整改", "整改中", "已关闭"], "整改状态", alias=["状态"]),
            TS_C(), UPD(),
        ]),
        Tab("qms_ppm_records", "fact", "供应商 PPM 记录（百万分之不良率）", 4200, [
            PK(),
            FK("supplier_id", "plm_suppliers", "供应商ID，维表 plm_suppliers.id → name（供应商名称）；按供应商分组时 JOIN 取 name", alias=["供应商", "厂家"]),
            FK("part_id", "plm_parts", "零件ID，维表 plm_parts.id → name（零件名称）", alias=["零件"]),
            D("stat_month", "DATE", "统计月份", MON, alias=["月份", "统计月"]),
            M("ppm", "PPM 值（不良率，百万分之一）", NUM(1, 2600, 1, 0), alias=["PPM", "不良率"], type="INTEGER"),
            M("lot_count", "检验批次量", NUM(50, 6000, 1, 0), alias=["批次量"], type="INTEGER"),
            M("defect_count", "不良品数量", "round((({i} * 37 % 18) + 0) / 1.0, 0)", alias=["不良数量"], type="INTEGER"),
        ]),
        Tab("qms_customer_complaints", "fact", "客户投诉", 15000, [
            PK(),
            D("complaint_no", "VARCHAR(30)", "投诉编号", NO("CP", 7), alias=["投诉号"]),
            FK("vehicle_id", "veh_vehicles", "涉诉整车ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            FK("dealer_id", "sal_dealers", "涉诉经销商ID，维表 sal_dealers.id → name（经销商名称）；按经销商分组时 JOIN 取 name", alias=["经销商", "门店"]),
            EN("category", ["质量", "服务", "销售"], "投诉类别", alias=["类别", "类型"]),
            EN("severity", SEVERITY, "严重程度", alias=["严重级别", "级别"]),
            EN("status", ["受理中", "处理中", "已解决", "已关闭"], "投诉状态", alias=["状态"]),
            D("description", "VARCHAR(500)", "投诉描述",
              ARR(["异响", "空调不制冷", "车机死机", "续航不达预期", "漆面瑕疵", "交付延迟",
                   "销售承诺未兑现", "维修效率低", "配件等待久", "充电故障"] + [f"投诉内容{i:04d}" for i in range(11, 15001)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["描述", "内容"]),
            TS_C(), UPD(),
        ]),
        Tab("qms_8d_reports", "fact", "8D 报告", 3600, [
            PK(),
            D("report_no", "VARCHAR(30)", "8D 报告编号", NO("8D", 6), alias=["8D号"]),
            FK("complaint_id", "qms_customer_complaints", "关联投诉ID，维表 qms_customer_complaints.id → complaint_no（投诉编号），可为空", alias=["投诉"], nullable=True),
            D("problem", "VARCHAR(400)", "问题描述",
              ARR(["批量尾灯进水", "制动异响批量问题", "空调压缩机异响", "软件黑屏批量"] + [f"8D问题{i:04d}" for i in range(5, 3601)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["问题"]),
            EN("status", ["D1-D2 组建团队", "D3-D4 根因分析", "D5-D6 纠正措施", "D7-D8 预防关闭"], "8D 进度", alias=["状态", "进度"]),
            TS_C(), UPD(),
        ]),
        Tab("qms_market_faults", "fact", "市场故障记录", 26000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "故障整车ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            FK("fault_code_id", "qms_fault_codes", "故障码ID，维表 qms_fault_codes.id → dtc_code（故障码）；按故障码/系统分组时 JOIN 取 dtc_code 或 name", alias=["故障码", "DTC"]),
            FK("service_center_id", "svc_service_centers", "维修服务中心ID，维表 svc_service_centers.id → name（服务中心名称）；按网点分组时 JOIN 取 name", alias=["服务中心", "网点"]),
            M("mileage", "故障时里程（公里）", NUM(100, 190000, 10, 0), alias=["里程", "行驶里程"], type="INTEGER"),
            EN("is_repeated", YESNO, "是否重复故障", alias=["重复故障"]),
            D("occurred_at", "TIMESTAMP", "故障发生时间", TS, alias=["发生时间", "时间"]),
        ]),
        Tab("qms_cpk_records", "fact", "过程能力 CPK 记录", 8000, [
            PK(),
            FK("station_id", "mfg_stations", "工位ID，维表 mfg_stations.id → name（工位名称）；按工位分组时 JOIN 取 name", alias=["工位"]),
            FK("part_id", "plm_parts", "零件ID，维表 plm_parts.id → name（零件名称）", alias=["零件"]),
            M("cpk", "过程能力指数 CPK", NUM(50, 250, 100, 2), alias=["CPK", "过程能力"], type="NUMERIC(6,2)"),
            D("measured_at", "TIMESTAMP", "测量时间", TS, alias=["测量时间", "时间"]),
        ]),
        Tab("qms_calibration_records", "fact", "量具校准记录", 5000, [
            PK(),
            D("gauge_no", "VARCHAR(40)", "量具编号", NO("GG", 7), alias=["量具号"]),
            FK("station_id", "mfg_stations", "使用工位ID，维表 mfg_stations.id → name（工位名称）", alias=["工位"]),
            EN("result", ["合格", "不合格"], "校准结论", alias=["结论", "结果"]),
            D("calibrated_at", "DATE", "校准日期", DAY, alias=["校准日期"]),
            D("next_due", "DATE", "下次校准到期日", "DATE '2026-03-01' + (({i} * 7) % 300) * INTERVAL '1 day'", alias=["到期日", "下次校准"]),
        ]),
        Tab("qms_capa_measures", "fact", "CAPA 纠正预防措施", 3000, [
            PK(),
            D("capa_no", "VARCHAR(30)", "CAPA 编号", NO("CAPA", 6), alias=["CAPA号"]),
            EN("source", ["投诉", "审核", "市场故障", "生产缺陷", "供应商"], "措施来源", alias=["来源"]),
            D("description", "VARCHAR(400)", "措施描述",
              ARR(["修订检验规范", "增加防错工位", "供应商换批", "软件版本修复", "工装改造",
                   "培训上岗", "图纸公差调整"] + [f"CAPA措施{i:04d}" for i in range(8, 3001)]).replace("{i:04d}", "lpad({i}::text,4,'0')"),
              alias=["描述", "措施"]),
            EN("status", ["进行中", "已关闭", "已验证"], "状态", alias=["状态"]),
            TS_C(), UPD(),
        ]),
    ]

    # ══════════════════════ 销售域（8）══════════════════════
    t += [
        Tab("sal_leads", "fact", "销售线索", 30000, [
            PK(),
            D("lead_no", "VARCHAR(30)", "线索编号", NO("LD", 8), alias=["线索号"]),
            EN("channel", ["线上", "门店", "车展", "转介绍", "直播"], "线索渠道", alias=["渠道", "来源"]),
            FK("region_id", "sal_regions", "大区ID，维表 sal_regions.id → name（大区名称）；按大区分组时 JOIN 取 name ★行级权限列（sales 角色按 region_id 隔离）", alias=["大区", "区域"]),
            FK("dealer_id", "sal_dealers", "经销商ID，维表 sal_dealers.id → name（经销商名称）；按经销商分组时 JOIN 取 name", alias=["经销商", "门店"]),
            FK("model_id", "veh_models", "意向车型ID，维表 veh_models.id → name（车型名称）；按意向车型分组时 JOIN 取 name", alias=["意向车型", "车型"]),
            EN("status", ["未跟进", "跟进中", "已到店", "已成交", "已流失"], "线索状态", alias=["状态"]),
            TS_C(), UPD(),
        ]),
        Tab("sal_test_drives", "fact", "试乘试驾记录", 16000, [
            PK(),
            D("drive_no", "VARCHAR(30)", "试驾编号", NO("TD", 7), alias=["试驾号"]),
            FK("customer_id", "sal_customers", "客户ID，维表 sal_customers.id → cust_no（客户编号）", alias=["客户"]),
            FK("model_id", "veh_models", "试驾车型ID，维表 veh_models.id → name（车型名称）；按车型分组时 JOIN 取 name", alias=["试驾车型", "车型"]),
            FK("dealer_id", "sal_dealers", "经销商ID，维表 sal_dealers.id → name（经销商名称）；按经销商分组时 JOIN 取 name", alias=["经销商", "门店"]),
            M("score", "客户试驾评分（1-10）", NUM(4, 10, 1, 1), alias=["评分", "试驾评分"]),
            M("mileage", "试驾里程（公里）", NUM(2, 30, 1, 1), alias=["试驾里程", "里程"]),
            D("drive_date", "DATE", "试驾日期", DAY, alias=["试驾日期", "日期"]),
        ]),
        Tab("sal_sales_orders", "fact", "销售订单", 25000, [
            PK(),
            D("order_no", "VARCHAR(30)", "订单编号", NO("SO", 8), alias=["订单号", "销售单号"]),
            FK("customer_id", "sal_customers", "下单客户ID，维表 sal_customers.id → cust_no（客户编号）；按客户分组时 JOIN 取 cust_no", alias=["客户"]),
            FK("dealer_id", "sal_dealers", "经销商ID，维表 sal_dealers.id → name（经销商名称）；按经销商分组/统计销售额时 JOIN sal_dealers 取 name", alias=["经销商", "门店"]),
            FK("trim_id", "veh_trims", "订购款型ID，维表 veh_trims.id → name（款型名称）；按款型/车型统计时 JOIN veh_trims（再 JOIN veh_models 取车型）", alias=["款型", "配置", "车型"]),
            FK("region_id", "sal_regions", "成交大区ID，维表 sal_regions.id → name（大区名称）；按大区分组时 JOIN 取 name ★行级权限列（sales 角色按 region_id 隔离）", alias=["大区", "区域"]),
            EN("order_type", ["全款", "贷款", "租赁"], "订单类型", alias=["类型", "购车方式"]),
            EN("status", ["待付款", "已锁单", "生产中", "运输中", "已交付", "已取消"], "订单状态", alias=["状态", "订单状态"]),
            M("amount", "订单金额（万元）", NUM(9, 60, 10, 2), alias=["金额", "成交金额", "销售额", "车价"]),
            TS_C(), UPD(),
        ]),
        Tab("sal_deliveries", "fact", "交付记录", 22000, [
            PK(),
            D("delivery_no", "VARCHAR(30)", "交付编号", NO("DL", 8), alias=["交付号"]),
            FK("order_id", "sal_sales_orders", "销售订单ID，维表 sal_sales_orders.id → order_no（订单编号）；按订单/经销商统计交付时 JOIN 取 order_no", alias=["订单", "销售订单"]),
            FK("vehicle_id", "veh_vehicles", "交付整车ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            EN("transport_mode", ["板车", "铁路", "自提"], "运输方式", alias=["运输", "方式"]),
            M("lead_days", "交付周期（天，锁单到交付）", NUM(3, 90, 1, 0), alias=["交付周期", "交付天数", "周期"], type="INTEGER"),
            D("delivered_at", "DATE", "交付日期", DAY, alias=["交付日期", "交付时间", "日期"]),
        ]),
        Tab("sal_invoices", "fact", "发票记录", 22000, [
            PK(),
            D("invoice_no", "VARCHAR(40)", "发票号码", NO("INV", 10), alias=["发票号"]),
            FK("order_id", "sal_sales_orders", "销售订单ID，维表 sal_sales_orders.id → order_no（订单编号）", alias=["订单", "销售订单"]),
            M("amount", "开票金额（万元）", NUM(9, 60, 10, 2), alias=["金额", "开票金额"]),
            M("tax", "税额（万元）", NUM(1, 8, 10, 2), alias=["税额", "税"]),
            D("issued_at", "DATE", "开票日期", DAY, alias=["开票日期", "日期"]),
        ]),
        Tab("sal_financing_applications", "fact", "金融贷款申请", 9000, [
            PK(),
            D("app_no", "VARCHAR(30)", "申请编号", NO("FIN", 7), alias=["申请号", "贷款申请号"]),
            FK("order_id", "sal_sales_orders", "销售订单ID，维表 sal_sales_orders.id → order_no（订单编号）", alias=["订单", "销售订单"]),
            D("bank", "VARCHAR(80)", "金融机构", ARR(["工商银行", "建设银行", "平安银行", "招商银行", "上汽财务", "比亚迪金融"]), alias=["银行", "金融机构"]),
            M("down_payment_ratio", "首付比例（0-1）", NUM(15, 60, 100, 2), alias=["首付比例", "首付"], type="NUMERIC(6,4)"),
            M("loan_amount", "贷款金额（万元）", NUM(5, 50, 10, 2), alias=["贷款额", "贷款金额"]),
            EN("status", ["审核中", "已通过", "已拒绝", "已放款"], "申请状态", alias=["状态"]),
            TS_C(), UPD(),
        ]),
        Tab("sal_inventories", "fact", "经销商库存", 13000, [
            PK(),
            FK("dealer_id", "sal_dealers", "经销商ID，维表 sal_dealers.id → name（经销商名称）；按经销商分组时 JOIN 取 name", alias=["经销商", "门店"]),
            FK("trim_id", "veh_trims", "款型ID，维表 veh_trims.id → name（款型名称）；按款型分组时 JOIN 取 name", alias=["款型", "配置"]),
            FK("color_id", "veh_colors", "颜色ID，维表 veh_colors.id → name（颜色名称）", alias=["颜色", "车色"]),
            M("stock_days", "库龄（天）", NUM(1, 260, 1, 0), alias=["库龄", "库存天数"], type="INTEGER"),
            EN("status", ["在途", "在库", "已预订"], "库存状态", alias=["状态"]),
            D("arrived_at", "DATE", "到店日期", DAY, alias=["到店日期"]),
        ]),
        Tab("sal_customer_visits", "fact", "客户到店登记", 12000, [
            PK(),
            FK("customer_id", "sal_customers", "客户ID，维表 sal_customers.id → cust_no（客户编号）", alias=["客户"]),
            FK("dealer_id", "sal_dealers", "经销商ID，维表 sal_dealers.id → name（经销商名称）；按经销商分组时 JOIN 取 name", alias=["经销商", "门店"]),
            EN("purpose", ["看车", "试驾", "保养", "维修", "提车"], "到店目的", alias=["目的", "事由"]),
            D("visited_at", "TIMESTAMP", "到店时间", TS, alias=["到店时间", "时间"]),
        ]),
    ]

    # ══════════════════════ 售后服务域（8）══════════════════════
    t += [
        Tab("svc_appointments", "fact", "服务预约", 20000, [
            PK(),
            D("apt_no", "VARCHAR(30)", "预约编号", NO("APT", 7), alias=["预约号"]),
            FK("customer_id", "sal_customers", "客户ID，维表 sal_customers.id → cust_no（客户编号）", alias=["客户"]),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            FK("service_center_id", "svc_service_centers", "服务中心ID，维表 svc_service_centers.id → name（服务中心名称）；按网点分组时 JOIN 取 name", alias=["服务中心", "网点"]),
            EN("service_type", ["保养", "维修", "召回", "检测"], "服务类型", alias=["类型"]),
            EN("status", ["已预约", "已到店", "已取消", "已完成"], "预约状态", alias=["状态"]),
            D("booked_at", "TIMESTAMP", "预约到店时间", TS, alias=["预约时间"]),
            D("arrived_at", "TIMESTAMP", "实际到店时间", TS2, alias=["实际到店时间", "到店时间"]),
        ]),
        Tab("svc_work_orders", "fact", "维修工单", 40000, [
            PK(),
            D("wo_no", "VARCHAR(30)", "维修工单号", NO("SWO", 8), alias=["工单号", "维修单号"]),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）；按车辆统计维修时 JOIN 取 vin", alias=["车辆", "整车"]),
            FK("service_center_id", "svc_service_centers", "服务中心ID，维表 svc_service_centers.id → name（服务中心名称）；按网点分组/统计维修量时 JOIN 取 name", alias=["服务中心", "网点"]),
            FK("advisor_id", "svc_service_advisors", "服务顾问ID，维表 svc_service_advisors.id → name（顾问姓名）", alias=["服务顾问", "顾问"]),
            FK("mechanic_id", "svc_mechanics", "主修技师ID，维表 svc_mechanics.id → name（技师姓名）；按技师分组时 JOIN 取 name，可为空", alias=["技师", "维修师傅"], nullable=True),
            EN("wo_type", ["保养", "机电维修", "钣金喷漆", "三电维修", "智驾标定", "软件升级"], "工单类型", alias=["类型", "维修类型"]),
            EN("status", ["已创建", "维修中", "已完工", "已结算"], "工单状态", alias=["状态"]),
            M("mileage", "进厂里程（公里）", NUM(500, 190000, 10, 0), alias=["里程", "进厂里程"], type="INTEGER"),
            M("labor_hours", "维修工时（小时）", NUM(5, 80, 10, 1), alias=["工时", "维修工时"]),
            D("intake_at", "TIMESTAMP", "进厂时间", TS, alias=["进厂时间", "时间"]),
            # ★ 完工时间与状态对齐地可空：status 按 i%4 落「已创建/维修中/已完工/已结算」，
            #   i%4<2 即前两个未完工状态 → finished_at 为 NULL（各 50%）。生产语义下
            #   「还有多少工单没修完」必须能答（数据体检实测：空值率 0% 是假象之一）。
            D("finished_at", "TIMESTAMP", "完工时间（未完成为 NULL）",
              "CASE WHEN {i} % 4 < 2 THEN NULL ELSE " + TS2 + " END", alias=["完工时间"]),
        ]),
        Tab("svc_wo_labor_items", "fact", "工单工时项目", 55000, [
            PK(),
            FK("work_order_id", "svc_work_orders", "维修工单ID，维表 svc_work_orders.id → wo_no（维修工单号）；按工单统计工时费用时 JOIN 取 wo_no", alias=["工单", "维修工单"]),
            FK("labor_item_id", "svc_labor_items", "工时项目ID，维表 svc_labor_items.id → name（工时项目名称）；按项目分组时 JOIN 取 name", alias=["工时项目", "项目", "服务项目"]),
            M("hours", "工时数（小时）", NUM(5, 80, 10, 1), alias=["工时"]),
            M("amount", "工时费（元）", NUM(80, 3600, 10, 2), alias=["金额", "工时费"]),
        ]),
        Tab("svc_wo_parts", "fact", "工单配件消耗", 70000, [
            PK(),
            FK("work_order_id", "svc_work_orders", "维修工单ID，维表 svc_work_orders.id → wo_no（维修工单号）；按工单统计配件费用时 JOIN 取 wo_no", alias=["工单", "维修工单"]),
            FK("part_id", "plm_parts", "配件ID，维表 plm_parts.id → name（零件名称）；按配件分组/统计配件消耗时 JOIN 取 name", alias=["配件", "零件"]),
            M("quantity", "消耗数量", NUM(1, 12, 1, 0), alias=["数量", "用量"], type="INTEGER"),
            M("unit_price", "配件单价（元）", NUM(5, 26000, 10, 2), alias=["单价", "价格"]),
            M("amount", "配件金额（元）", NUM(5, 120000, 10, 2), alias=["金额", "配件金额"]),
        ]),
        Tab("svc_warranty_claims", "fact", "质保索赔单", 18000, [
            PK(),
            D("claim_no", "VARCHAR(30)", "索赔编号", NO("WC", 8), alias=["索赔号"]),
            FK("work_order_id", "svc_work_orders", "维修工单ID，维表 svc_work_orders.id → wo_no（维修工单号）", alias=["工单", "维修工单"]),
            FK("policy_id", "svc_warranty_policies", "适用政策ID，维表 svc_warranty_policies.id → name（政策名称）", alias=["政策", "质保政策"]),
            M("amount", "索赔金额（元）", NUM(50, 48000, 10, 2), alias=["金额", "索赔金额"]),
            EN("status", ["待审核", "已通过", "已拒赔", "已打款"], "索赔状态", alias=["状态"]),
            D("submitted_at", "DATE", "提交日期", DAY, alias=["提交日期", "日期"]),
        ]),
        Tab("svc_customer_feedback", "fact", "服务回访满意度", 26000, [
            PK(),
            FK("work_order_id", "svc_work_orders", "维修工单ID，维表 svc_work_orders.id → wo_no（维修工单号）；按工单关联回访时 JOIN 取 wo_no", alias=["工单", "维修工单"]),
            M("score", "满意度评分（1-5）", NUM(1, 5, 1, 0), alias=["评分", "满意度", "满意度评分"], type="INTEGER"),
            EN("nps_bucket", ["贬损者", "中立者", "推荐者"], "NPS 分类", alias=["NPS"]),
            D("comment", "VARCHAR(400)", "回访评语",
              ARR(["服务很好，很专业", "等待时间有点长", "一次修好，点赞", "配件等了一周",
                   "态度不错", "价格偏贵", "环境整洁", "技师解释很清楚", "还要再来一次", "总体满意"]), alias=["评语", "评价"]),
            D("surveyed_at", "TIMESTAMP", "回访时间", TS2, alias=["回访时间", "时间"]),
        ]),
        Tab("svc_roadside_assists", "fact", "道路救援记录", 4200, [
            PK(),
            D("case_no", "VARCHAR(30)", "救援案例号", NO("RSA", 7), alias=["救援号", "案例号"]),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            FK("service_center_id", "svc_service_centers", "出援服务中心ID，维表 svc_service_centers.id → name（服务中心名称）；按网点分组时 JOIN 取 name", alias=["服务中心", "网点"]),
            EN("reason", ["亏电", "爆胎", "事故", "拖车", "充电故障", "锁车困境"], "救援原因", alias=["原因", "类型"]),
            M("response_minutes", "响应时长（分钟）", NUM(10, 240, 1, 0), alias=["响应时长", "响应"], type="INTEGER"),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
        ]),
        Tab("svc_maintenance_signups", "fact", "保养套餐订购", 6000, [
            PK(),
            FK("customer_id", "sal_customers", "客户ID，维表 sal_customers.id → cust_no（客户编号）", alias=["客户"]),
            FK("package_id", "svc_maintenance_packages", "套餐ID，维表 svc_maintenance_packages.id → name（套餐名称）；按套餐分组时 JOIN 取 name", alias=["套餐", "保养套餐"]),
            M("paid_amount", "实付金额（元）", NUM(0, 21000, 10, 2), alias=["实付", "金额"]),
            D("signed_at", "DATE", "订购日期", DAY, alias=["订购日期", "日期"]),
            EN("channel", ["门店", "APP", "电话"], "订购渠道", alias=["渠道"]),
        ]),
    ]

    # ══════════════════════ 召回域（4）══════════════════════
    t += [
        Tab("rcl_recalls", "dim", "召回活动", 14, [
            PK(),
            D("recall_no", "VARCHAR(30)", "召回编号", NO("RC", 5), alias=["召回号"]),
            D("name", "VARCHAR(200)", "召回活动名称",
              ARR(["电池包线束固定优化", "制动助力软件升级", "安全带预紧器更换", "车门锁止机构更换",
                   "动力电池BMS软件刷新", "座椅传感器更换", "冷却水泵更换", "气囊模块更换",
                   "高压继电器更换", "雨刮电机更换", "尾灯密封改进", "转向机线束更换",
                   "OTA 策略升级活动", "底盘螺栓复紧活动"]),
              alias=["召回活动", "活动名称", "服务活动"]),
            EN("level", ["召回", "服务活动"], "活动级别", alias=["级别", "类型"]),
            D("authority", "VARCHAR(60)", "监管机构", ARR(["国家市场监督管理总局", "企业自主", "地方监管局"]), alias=["机构"]),
            D("announce_date", "DATE", "公告日期", "DATE '2025-03-01' + ({i} % 420) * INTERVAL '1 day'", alias=["公告日期"]),
            EN("status", ["进行中", "已完成"], "召回状态", alias=["状态"]),
        ]),
        Tab("rcl_recall_vehicles", "fact", "召回车辆清单", 8500, [
            PK(),
            FK("recall_id", "rcl_recalls", "召回活动ID，维表 rcl_recalls.id → name（召回活动名称）；按召回活动分组时 JOIN 取 name", alias=["召回活动", "召回"]),
            FK("vehicle_id", "veh_vehicles", "召回车辆ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            FK("region_id", "sal_regions", "车辆所属大区ID，维表 sal_regions.id → name（大区名称）；按大区分组时 JOIN 取 name ★行级权限列（sales 角色按 region_id 隔离）", alias=["大区", "区域"]),
            EN("status", ["未通知", "已通知", "已进厂", "已完成"], "处置状态", alias=["状态", "召回进度"]),
            D("notified_at", "DATE", "通知日期", "CASE WHEN {i} % 4 = 0 THEN NULL ELSE DATE '2025-05-01' + (({i} * 7) % 350) * INTERVAL '1 day' END", alias=["通知日期"]),
        ]),
        Tab("rcl_recall_remedies", "fact", "召回处置记录", 7600, [
            PK(),
            FK("recall_vehicle_id", "rcl_recall_vehicles", "召回车辆清单ID，维表 rcl_recall_vehicles.id", alias=["召回车辆"]),
            FK("service_center_id", "svc_service_centers", "处置服务中心ID，维表 svc_service_centers.id → name（服务中心名称）；按网点分组时 JOIN 取 name", alias=["服务中心", "网点"]),
            D("remedy_date", "DATE", "处置日期", DAY, alias=["处置日期", "日期"]),
            M("cost", "单车处置成本（元）", NUM(80, 12000, 10, 2), alias=["成本", "处置成本"]),
            EN("result", ["修复完成", "待件", "车主未到"], "处置结果", alias=["结果"]),
        ]),
        Tab("rcl_recall_costs", "fact", "召回成本明细", 3000, [
            PK(),
            FK("recall_id", "rcl_recalls", "召回活动ID，维表 rcl_recalls.id → name（召回活动名称）", alias=["召回活动", "召回"]),
            EN("cost_type", ["零件", "工时", "物流", "其他"], "成本类型", alias=["类型"]),
            M("amount", "金额（万元）", NUM(1, 2600, 10, 2), alias=["金额", "成本"]),
            D("occurred_month", "DATE", "发生月份", MON, alias=["月份"]),
        ]),
    ]

    # ══════════════════════ 车联网/OTA 域（11）══════════════════════
    t += [
        Tab("iot_trip_records", "fact", "行程记录", 180000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）；按车辆统计里程时 JOIN 取 vin", alias=["车辆", "整车"]),
            D("started_at", "TIMESTAMP", "行程开始时间", TS, alias=["开始时间", "时间"]),
            D("ended_at", "TIMESTAMP", "行程结束时间", TS2, alias=["结束时间"]),
            M("distance_km", "行驶里程（公里）", NUM(2, 720, 10, 1), alias=["里程", "行驶里程", "公里数"]),
            M("energy_kwh", "耗电量（kWh）", NUM(1, 120, 10, 2), alias=["耗电量", "电量", "能耗"]),
            M("avg_speed", "平均车速（km/h）", NUM(8, 130, 10, 1), alias=["平均车速", "速度"]),
            M("max_speed", "最高车速（km/h）", NUM(60, 220, 10, 0), alias=["最高车速"], type="INTEGER"),
            EN("drive_mode", ["经济", "舒适", "运动", "雪地", "越野"], "驾驶模式", alias=["模式", "驾驶模式"]),
        ]),
        Tab("iot_vehicle_alerts", "fact", "车辆告警事件", 60000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            EN("alert_type", ["低电量", "胎压异常", "疲劳驾驶", "超速", "碰撞预警", "故障提示"], "告警类型", alias=["类型", "告警类别"]),
            EN("level", ["提示", "警告", "紧急"], "告警级别", alias=["级别"]),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
            D("cleared_at", "TIMESTAMP", "解除时间", TS2, alias=["解除时间"]),
        ]),
        Tab("iot_dtc_events", "fact", "DTC 诊断事件", 85000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）；按车辆统计故障码时 JOIN 取 vin", alias=["车辆", "整车"]),
            FK("fault_code_id", "qms_fault_codes", "故障码ID，维表 qms_fault_codes.id → dtc_code（故障码）；按故障码/系统统计时 JOIN qms_fault_codes 取 dtc_code/name", alias=["故障码", "DTC"]),
            M("mileage", "发生时里程（公里）", NUM(100, 190000, 10, 0), alias=["里程"], type="INTEGER"),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
            EN("status", ["活跃", "已消除", "历史"], "事件状态", alias=["状态"]),
        ]),
        Tab("iot_charging_sessions", "fact", "充电会话", 95000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）；按车辆统计充电时 JOIN 取 vin", alias=["车辆", "整车"]),
            EN("charge_type", ["快充", "慢充", "家充"], "充电类型", alias=["类型", "充电方式"]),
            D("city", "VARCHAR(30)", "充电城市", ARR(CITIES), alias=["城市"]),
            M("energy_kwh", "充电电量（kWh）", NUM(5, 120, 10, 2), alias=["电量", "充电量", "充电电量"]),
            M("cost", "充电费用（元）", NUM(5, 420, 10, 2), alias=["费用", "金额", "充电费用"]),
            M("duration_min", "充电时长（分钟）", NUM(8, 720, 1, 0), alias=["时长", "充电时长"], type="INTEGER"),
            D("started_at", "TIMESTAMP", "开始时间", TS, alias=["开始时间", "时间"]),
        ]),
        Tab("iot_energy_daily", "fact", "能耗日统计（车×日）", 100000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            D("stat_date", "DATE", "统计日期", DAY, alias=["日期", "统计日期"]),
            M("distance_km", "当日里程（公里）", NUM(5, 900, 10, 1), alias=["里程", "当日里程"]),
            M("energy_kwh", "当日耗电（kWh）", NUM(1, 160, 10, 2), alias=["耗电", "能耗", "当日能耗"]),
            M("regen_kwh", "动能回收电量（kWh）", NUM(0, 40, 10, 2), alias=["回收电量", "动能回收"]),
        ]),
        Tab("iot_geofence_events", "fact", "电子围栏事件", 9000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            EN("event_type", ["驶入", "驶出", "超速", "停留超时"], "围栏事件类型", alias=["类型"]),
            D("fence_name", "VARCHAR(80)", "围栏名称", ARR(["二环内限行区", "试驾专属路线", "工厂园区", "经销商展区", "城市限行区"]), alias=["围栏"]),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
        ]),
        Tab("ota_campaigns", "dim", "OTA 推送活动", 34, [
            PK(),
            D("name", "VARCHAR(150)", "OTA 活动名称",
              ARR(["V2.5 全量推送", "V3.0 灰度推送", "泊车优化专项", "语音引擎升级", "泊车记忆功能",
                   "高速NOA优化", "冬季续航专项", "座椅记忆修复", "仪表主题更新", "充电策略优化"] + [f"OTA活动{i}" for i in range(11, 35)]),
              alias=["OTA活动", "推送活动", "活动"]),
            FK("package_id", "ota_packages", "软件包ID，维表 ota_packages.id → version（软件版本）", alias=["软件包", "版本"]),
            EN("strategy", ["全量", "灰度", "定向"], "推送策略", alias=["策略", "推送策略"]),
            D("started_at", "TIMESTAMP", "开始时间", TS, alias=["开始时间"]),
            EN("status", ["进行中", "已完成", "已暂停"], "活动状态", alias=["状态"]),
        ]),
        Tab("ota_installations", "fact", "OTA 安装记录", 62000, [
            PK(),
            FK("campaign_id", "ota_campaigns", "OTA 活动ID，维表 ota_campaigns.id → name（活动名称）；按活动分组/统计成功率时 JOIN 取 name", alias=["OTA活动", "活动"]),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）；按车辆统计时 JOIN 取 vin", alias=["车辆", "整车"]),
            EN("result", ["成功", "失败", "取消"], "安装结果", alias=["结果", "安装状态"]),
            M("duration_min", "安装耗时（分钟）", NUM(5, 90, 1, 0), alias=["耗时", "安装时长"], type="INTEGER"),
            D("started_at", "TIMESTAMP", "开始时间", TS, alias=["开始时间", "时间"]),
        ]),
        Tab("ota_install_failures", "fact", "OTA 安装失败明细", 3200, [
            PK(),
            FK("installation_id", "ota_installations", "安装记录ID，维表 ota_installations.id", alias=["安装记录"]),
            D("error_code", "VARCHAR(30)", "错误码", ARR(["E_DOWNLOAD", "E_VERIFY", "E_FLASH", "E_ROLLBACK", "E_BATTERY", "E_TIMEOUT"]), alias=["错误码"]),
            EN("phase", ["下载", "校验", "刷写", "回滚"], "失败阶段", alias=["阶段"]),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
        ]),
        Tab("ota_ecu_software_versions", "fact", "车辆 ECU 软件版本快照", 9000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            FK("ecu_id", "ota_ecus", "ECU ID，维表 ota_ecus.id → name（ECU 名称）；按 ECU 分组时 JOIN 取 name", alias=["ECU", "控制器"]),
            D("version", "VARCHAR(30)", "当前软件版本",
              ARR(["V1.0.0", "V1.1.2", "V2.0.1", "V2.3.0", "V2.5.0", "V3.0.0"]), alias=["版本", "软件版本"]),
            D("flashed_at", "TIMESTAMP", "刷写时间", TS2, alias=["刷写时间", "时间"]),
        ]),
        Tab("iot_driving_behavior_scores", "fact", "驾驶行为评分（月度）", 40000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            D("stat_month", "DATE", "统计月份", MON, alias=["月份", "统计月"]),
            M("score", "驾驶行为评分（0-100）", NUM(45, 100, 10, 1), alias=["评分", "驾驶评分"]),
            M("harsh_brake_count", "急刹次数", NUM(0, 60, 1, 0), alias=["急刹", "急刹车次数"], type="INTEGER"),
            M("harsh_accel_count", "急加速次数", NUM(0, 60, 1, 0), alias=["急加速", "急加速次数"], type="INTEGER"),
        ]),
    ]

    # ══════════════════════ 三电/电池域（6）══════════════════════
    t += [
        Tab("bat_battery_packs", "fact", "电池包档案", 30000, [
            PK(),
            D("pack_no", "VARCHAR(30)", "电池包编号", NO("BP", 8), alias=["电池包号", "电池编号"]),
            FK("vehicle_id", "veh_vehicles", "装配整车ID，维表 veh_vehicles.id → vin（车架号）；按车辆查询电池时 JOIN 取 vin", alias=["车辆", "整车"]),
            FK("supplier_id", "plm_suppliers", "电池供应商ID，维表 plm_suppliers.id → name（供应商名称）；按供应商分组时 JOIN 取 name", alias=["供应商", "厂家"]),
            M("capacity_kwh", "标称容量（kWh）", NUM(40, 150, 10, 1), alias=["容量", "电池容量"]),
            EN("chemistry", ["磷酸铁锂", "三元锂"], "电池化学体系", alias=["化学体系", "电芯类型"]),
            D("manufactured_at", "DATE", "生产日期", DAY, alias=["生产日期", "出厂日期"]),
        ]),
        Tab("bat_soh_records", "fact", "电池健康度 SOH 记录", 90000, [
            PK(),
            FK("pack_id", "bat_battery_packs", "电池包ID，维表 bat_battery_packs.id → pack_no（电池包编号）；按电池包统计 SOH 时 JOIN 取 pack_no", alias=["电池包", "电池"]),
            M("soh", "电池健康度 SOH（%）", NUM(72, 101, 10, 1), alias=["SOH", "健康度", "电池健康度"]),
            M("temperature", "检测时温度（℃）", NUM(-10, 55, 10, 1), alias=["温度"]),
            M("mileage", "检测时里程（公里）", NUM(500, 200000, 10, 0), alias=["里程"], type="INTEGER"),
            D("measured_at", "DATE", "检测日期", DAY, alias=["检测日期", "日期"]),
        ]),
        Tab("bat_pack_inspections", "fact", "电池包检测记录", 20000, [
            PK(),
            FK("pack_id", "bat_battery_packs", "电池包ID，维表 bat_battery_packs.id → pack_no（电池包编号）", alias=["电池包", "电池"]),
            EN("inspection_type", ["例行检测", "事故检测", "召回检测", "升级检测"], "检测类型", alias=["类型"]),
            EN("result", ["正常", "异常"], "检测结果", alias=["结论", "结果"]),
            D("inspected_at", "DATE", "检测日期", DAY, alias=["检测日期", "日期"]),
        ]),
        Tab("bat_battery_faults", "fact", "电池故障记录", 3200, [
            PK(),
            FK("pack_id", "bat_battery_packs", "电池包ID，维表 bat_battery_packs.id → pack_no（电池包编号）；按电池包统计故障时 JOIN 取 pack_no", alias=["电池包", "电池"]),
            EN("fault_type", ["过热", "析锂", "绝缘异常", "均衡异常", "压差过大", "BMS 通信"], "故障类型", alias=["类型"]),
            EN("severity", SEVERITY, "严重程度", alias=["严重级别", "级别"]),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
        ]),
        Tab("bat_battery_recycles", "fact", "电池回收记录", 2100, [
            PK(),
            FK("pack_id", "bat_battery_packs", "电池包ID，维表 bat_battery_packs.id → pack_no（电池包编号）", alias=["电池包", "电池"]),
            D("recycled_at", "DATE", "回收日期", DAY, alias=["回收日期", "日期"]),
            M("residual_value", "残值（元）", NUM(200, 36000, 10, 2), alias=["残值", "回收价值"]),
            EN("channel", ["官方回收", "第三方", "梯次利用"], "回收渠道", alias=["渠道"]),
        ]),
        Tab("bat_charge_cycles", "fact", "充放电循环统计（月度）", 24000, [
            PK(),
            FK("pack_id", "bat_battery_packs", "电池包ID，维表 bat_battery_packs.id → pack_no（电池包编号）", alias=["电池包", "电池"]),
            D("stat_month", "DATE", "统计月份", MON, alias=["月份", "统计月"]),
            M("cycle_count", "循环次数", NUM(5, 120, 1, 0), alias=["循环次数", "充放电次数"], type="INTEGER"),
            M("dc_fast_count", "快充次数", "round((({i} * 37 % 40) + 1) / 1.0, 0)", alias=["快充次数"], type="INTEGER"),
        ]),
    ]

    # ══════════════════════ 智驾测试域（6）══════════════════════
    t += [
        Tab("ad_test_drives", "fact", "智驾路测记录", 30000, [
            PK(),
            D("drive_no", "VARCHAR(30)", "路测编号", NO("AD", 8), alias=["路测号"]),
            FK("test_vehicle_id", "ad_test_vehicles", "测试车辆ID，维表 ad_test_vehicles.id → plate_no（测试车牌号）；按测试车分组时 JOIN 取 plate_no", alias=["测试车", "测试车辆"]),
            FK("scenario_id", "ad_scenarios", "主场景ID，维表 ad_scenarios.id → name（场景名称）；按场景分组时 JOIN 取 name", alias=["场景", "测试场景"]),
            D("drive_date", "DATE", "测试日期", DAY, alias=["测试日期", "日期"]),
            M("distance_km", "测试里程（公里）", NUM(5, 620, 10, 1), alias=["里程", "测试里程"]),
            EN("weather", ["晴天", "雨天", "雪天", "雾天", "夜间"], "天气", alias=["天气", "天气条件"]),
            EN("result", ["通过", "未通过", "中断"], "测试结论", alias=["结论", "结果"]),
        ]),
        Tab("ad_scenario_runs", "fact", "场景执行记录", 80000, [
            PK(),
            FK("test_drive_id", "ad_test_drives", "路测记录ID，维表 ad_test_drives.id → drive_no（路测编号）", alias=["路测记录", "路测"]),
            FK("scenario_id", "ad_scenarios", "场景ID，维表 ad_scenarios.id → name（场景名称）；按场景统计通过率时 JOIN ad_scenarios 取 name", alias=["场景", "测试场景"]),
            EN("passed", ["是", "否"], "是否通过", alias=["通过", "是否通过", "结果"]),
            M("duration_s", "耗时（秒）", NUM(10, 1800, 1, 0), alias=["耗时"], type="INTEGER"),
        ]),
        Tab("ad_disengagements", "fact", "智驾接管事件", 12000, [
            PK(),
            FK("test_drive_id", "ad_test_drives", "路测记录ID，维表 ad_test_drives.id → drive_no（路测编号）；按路测统计接管次数时 JOIN 取 drive_no", alias=["路测记录", "路测"]),
            EN("type", ["人工接管", "系统退出"], "接管类型", alias=["类型"]),
            EN("reason", ["感知丢失", "规划异常", "控制异常", "舒适度差", "安全冗余", "法规区域"], "接管原因", alias=["原因"]),
            M("speed", "接管时车速（km/h）", NUM(0, 130, 10, 1), alias=["车速", "速度"]),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
        ]),
        Tab("ad_mileage_records", "fact", "智驾里程统计（测试车×日）", 42000, [
            PK(),
            FK("test_vehicle_id", "ad_test_vehicles", "测试车辆ID，维表 ad_test_vehicles.id → plate_no（测试车牌号）；按测试车统计里程时 JOIN 取 plate_no", alias=["测试车", "测试车辆"]),
            D("stat_date", "DATE", "统计日期", DAY, alias=["日期", "统计日期"]),
            M("auto_km", "智驾里程（公里）", NUM(5, 800, 10, 1), alias=["智驾里程", "自动驾驶里程"]),
            M("manual_km", "人工驾驶里程（公里）", NUM(5, 600, 10, 1), alias=["人工里程"]),
            D("city", "VARCHAR(30)", "测试城市", ARR(CITIES), alias=["城市"]),
        ]),
        Tab("ad_safety_incidents", "fact", "测试安全事故", 220, [
            PK(),
            FK("test_drive_id", "ad_test_drives", "路测记录ID，维表 ad_test_drives.id → drive_no（路测编号）", alias=["路测记录", "路测"]),
            EN("level", ["轻微", "一般", "较大", "重大"], "事故等级", alias=["等级"]),
            D("description", "VARCHAR(400)", "事故描述",
              ARR(["测试车被追尾", "轻微剐蹭护栏", "紧急制动致追尾风险", "传感器误触发", "接管不及蹭路沿"] + [f"事故{i:03d}" for i in range(6, 221)]).replace("{i:03d}", "lpad({i}::text,3,'0')"),
              alias=["描述"]),
            D("occurred_at", "TIMESTAMP", "发生时间", TS, alias=["发生时间", "时间"]),
        ]),
        Tab("ad_perception_kpi", "fact", "感知指标记录", 15000, [
            PK(),
            FK("scenario_id", "ad_scenarios", "场景ID，维表 ad_scenarios.id → name（场景名称）；按场景分组时 JOIN 取 name", alias=["场景"]),
            M("detection_rate", "目标检出率（0-1）", NUM(80, 100, 100, 3), alias=["检出率"], type="NUMERIC(7,4)"),
            M("false_positive_rate", "误检率（0-1）", NUM(0, 8, 100, 3), alias=["误检率"], type="NUMERIC(7,4)"),
            D("measured_at", "DATE", "测量日期", DAY, alias=["测量日期", "日期"]),
        ]),
    ]

    # ══════════════════════ 财务域（5）══════════════════════
    t += [
        Tab("fin_vehicle_costs", "fact", "单车成本", 30000, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "整车ID，维表 veh_vehicles.id → vin（车架号）；按车辆统计成本时 JOIN 取 vin", alias=["车辆", "整车"]),
            M("material_cost", "材料成本（元）", NUM(40000, 180000, 10, 2), alias=["材料成本", "物料成本"]),
            M("labor_cost", "人工成本（元）", NUM(3000, 16000, 10, 2), alias=["人工成本"]),
            M("overhead_cost", "制造费用（元）", NUM(5000, 30000, 10, 2), alias=["制造费用", "摊销"]),
            M("total_cost", "总成本（元）", NUM(50000, 220000, 10, 2), alias=["总成本", "单车成本", "成本"]),
        ]),
        Tab("fin_revenues", "fact", "营收记录（月度）", 5000, [
            PK(),
            D("stat_month", "DATE", "统计月份", MON, alias=["月份", "统计月"]),
            FK("region_id", "sal_regions", "大区ID，维表 sal_regions.id → name（大区名称）；按大区分组时 JOIN 取 name ★行级权限列（sales 角色按 region_id 隔离）", alias=["大区", "区域"]),
            EN("business_line", ["整车", "配件", "金融", "服务"], "业务线", alias=["业务", "业务线"]),
            M("amount", "营收金额（万元）", NUM(500, 900000, 10, 2), alias=["金额", "营收", "收入"]),
        ]),
        Tab("fin_incentives", "fact", "补贴记录", 3200, [
            PK(),
            FK("vehicle_id", "veh_vehicles", "车辆ID，维表 veh_vehicles.id → vin（车架号）", alias=["车辆", "整车"]),
            EN("incentive_type", ["国补", "地补", "置换补贴", "下乡补贴"], "补贴类型", alias=["类型", "补贴类型"]),
            M("amount", "补贴金额（元）", NUM(3000, 30000, 10, 2), alias=["金额", "补贴金额"]),
            D("granted_at", "DATE", "拨付日期", DAY, alias=["拨付日期", "日期"]),
        ]),
        Tab("fin_dealer_settlements", "fact", "经销商结算", 8000, [
            PK(),
            D("settle_no", "VARCHAR(30)", "结算单号", NO("ST", 7), alias=["结算号"]),
            FK("dealer_id", "sal_dealers", "经销商ID，维表 sal_dealers.id → name（经销商名称）；按经销商分组时 JOIN 取 name", alias=["经销商", "门店"]),
            D("period", "VARCHAR(10)", "结算期间", ARR(["2025Q3", "2025Q4", "2026Q1", "2026Q2", "2026Q3"]), alias=["期间"]),
            EN("settle_type", ["车款", "返利", "佣金", "建设补贴"], "结算类型", alias=["类型"]),
            M("amount", "结算金额（万元）", NUM(10, 68000, 10, 2), alias=["金额", "结算金额"]),
        ]),
        Tab("fin_manhour_rates", "dim", "工时费率表", 40, [
            PK(),
            D("name", "VARCHAR(80)", "费率名称", ARR(["机电工时费率-A店", "机电工时费率-B店", "钣喷工时费率", "三电工时费率", "智驾标定费率", "夜间加班费率"] + [f"费率{i}" for i in range(7, 41)]), alias=["费率"]),
            M("rate", "费率（元/小时）", NUM(80, 680, 10, 0), alias=["费率值", "单价"]),
            EN("level", ["标准", "高峰", "夜间"], "费率档", alias=["档位"]),
        ]),
    ]
    # ── 后处理：值召回配置 ──────────────────────────────────
    # 维表的 label 列（默认 name）进 ES 值索引；指向「带 name 列维表」的外键
    # 也 sync（enum_source 拉 ID→可读名映射，行级权限与分组取值都靠它）。
    # 超 200 值的列由 build_nl2sql_meta 的护栏自动整列跳过，这里不用管。
    by_name = {x.name: x for x in t}
    for tab in t:
        if tab.role == "dim":
            for c in tab.cols:
                if c.name == tab.label and not c.sync:
                    c.sync = True
        for c in tab.cols:
            if c.role == "foreign_key" and not c.sync:
                ref = c.enum_source.split(".")[0]
                rt = by_name.get(ref)
                if rt is not None and any(rc.name == "name" for rc in rt.cols):
                    c.sync = True
    return t


def metrics() -> list[dict]:
    return [
        dict(name="vehicle_count", description="整车数量/下线量统计，使用 COUNT(*) 聚合",
             relevant_columns=["veh_vehicles.id"], alias=["整车数量", "下线量", "产量", "车辆数", "总数"]),
        dict(name="sales_order_count", description="销售订单量统计，使用 COUNT(*) 聚合",
             relevant_columns=["sal_sales_orders.id"], alias=["订单量", "订单数", "销量", "成交量", "总数"]),
        dict(name="sales_amount", description="销售总金额，SUM(sal_sales_orders.amount)，单位万元",
             relevant_columns=["sal_sales_orders.amount"], alias=["销售额", "销售总额", "成交金额", "总销售额", "金额"]),
        dict(name="delivery_count", description="交付量统计，使用 COUNT(*) 聚合",
             relevant_columns=["sal_deliveries.id"], alias=["交付量", "交付数", "交付总数"]),
        dict(name="avg_delivery_days", description="平均交付周期（天），AVG(sal_deliveries.lead_days)",
             relevant_columns=["sal_deliveries.lead_days"], alias=["平均交付周期", "交付周期", "平均交付天数"]),
        dict(name="complaint_count", description="客户投诉量统计，使用 COUNT(*) 聚合",
             relevant_columns=["qms_customer_complaints.id"], alias=["投诉量", "投诉数", "投诉总数"]),
        dict(name="market_fault_count", description="市场故障量统计，使用 COUNT(*) 聚合",
             relevant_columns=["qms_market_faults.id"], alias=["故障量", "市场故障数", "故障总数"]),
        dict(name="avg_ppm", description="供应商平均 PPM（百万分之不良率），AVG(qms_ppm_records.ppm)",
             relevant_columns=["qms_ppm_records.ppm"], alias=["平均PPM", "PPM", "不良率"]),
        dict(name="warranty_claim_amount", description="质保索赔总金额，SUM(svc_warranty_claims.amount)，单位元",
             relevant_columns=["svc_warranty_claims.amount"], alias=["索赔金额", "索赔总额", "三包费用"]),
        dict(name="svc_wo_count", description="维修工单量统计，使用 COUNT(*) 聚合",
             relevant_columns=["svc_work_orders.id"], alias=["维修量", "工单量", "维修工单数", "进厂台次"]),
        dict(name="avg_repair_hours", description="平均维修工时（小时），AVG(svc_work_orders.labor_hours)",
             relevant_columns=["svc_work_orders.labor_hours"], alias=["平均维修工时", "维修工时", "平均工时"]),
        dict(name="defect_count", description="生产缺陷数量统计，使用 COUNT(*) 聚合",
             relevant_columns=["mfg_station_defects.id"], alias=["缺陷数", "缺陷数量", "不良数"]),
        dict(name="issue_count", description="研发缺陷问题单数量统计，使用 COUNT(*) 聚合",
             relevant_columns=["alm_issues.id"], alias=["问题数", "问题单数", "缺陷单数", "缺陷数"]),
        dict(name="test_pass_rate", description="测试通过率，COUNT(result='通过')/COUNT(*)",
             relevant_columns=["alm_test_executions.result"], alias=["通过率", "测试通过率"]),
        dict(name="recall_vehicle_count", description="召回车辆数量统计，使用 COUNT(*) 聚合",
             relevant_columns=["rcl_recall_vehicles.id"], alias=["召回车辆数", "召回量", "涉及车辆数"]),
        dict(name="ota_success_rate", description="OTA 安装成功率，COUNT(result='成功')/COUNT(*)",
             relevant_columns=["ota_installations.result"], alias=["升级成功率", "OTA成功率", "安装成功率"]),
        dict(name="avg_soh", description="电池平均健康度，AVG(bat_soh_records.soh)，单位%",
             relevant_columns=["bat_soh_records.soh"], alias=["平均SOH", "电池健康度", "SOH"]),
        dict(name="total_charging_kwh", description="总充电电量，SUM(iot_charging_sessions.energy_kwh)，单位kWh",
             relevant_columns=["iot_charging_sessions.energy_kwh"], alias=["充电电量", "总充电量", "充电度数"]),
        dict(name="energy_per_100km", description="百公里能耗，SUM(energy_kwh)/SUM(distance_km)*100（iot_energy_daily）",
             relevant_columns=["iot_energy_daily.energy_kwh"], alias=["百公里能耗", "能耗", "电耗"]),
        dict(name="trip_distance_total", description="总行驶里程，SUM(iot_trip_records.distance_km)，单位公里",
             relevant_columns=["iot_trip_records.distance_km"], alias=["总里程", "行驶里程", "总行驶里程"]),
        dict(name="disengagement_count", description="智驾接管事件数量统计，使用 COUNT(*) 聚合",
             relevant_columns=["ad_disengagements.id"], alias=["接管次数", "接管数", "接管总数"]),
        dict(name="inventory_count", description="经销商库存数量统计，使用 COUNT(*) 聚合",
             relevant_columns=["sal_inventories.id"], alias=["库存量", "库存数", "在库数量"]),
        dict(name="avg_unit_cost", description="平均单车成本，AVG(fin_vehicle_costs.total_cost)，单位元",
             relevant_columns=["fin_vehicle_costs.total_cost"], alias=["单车成本", "平均成本", "单位成本"]),
        dict(name="nps_score", description="服务满意度平均分，AVG(svc_customer_feedback.score)",
             relevant_columns=["svc_customer_feedback.score"], alias=["满意度", "服务满意度", "平均满意度", "NPS"]),
    ]
