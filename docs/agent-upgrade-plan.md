# rd-chatBI 深度升级方案：从静态流水线到「语义层 + Agent 双引擎」

> 调研范围：Vanna、WrenAI、SuperSonic、DB-GPT（工程派），MAC-SQL、XiYan-SQL、CHESS、CHASE-SQL（研究派），LangChain/LangGraph 官方 SQL Agent 范式。
> 结论：不换框架、不整仓替换，保留本项目的差异化资产（sqlglot 四层安全防线、badcase 评测回流闭环、三层元数据），借鉴四条主线——**Agent 工具化、few-shot 示例库在线化、语义层 DSL 双引擎、澄清反问/结果自评**——分三阶段大改。

---

## 一、主流开源方案调研

### 1.1 工程派（可直接借鉴架构）

| 项目 | 定位 | 核心机制 | 对本项目的借鉴点 |
|---|---|---|---|
| **Vanna** (MIT, 23.8k★，**仓库已于 2026-02 归档**) | RAG 式 Text-to-SQL | ① 训练三元组：DDL / 业务文档 / (question, SQL) 对；② 生成前先向量检索相似问答对做 few-shot；③ 2.0 版重写为 Agent 架构（Agent + ToolRegistry 用户感知工具 + ContextEnricher + 生命周期钩子），但**闭源未发布**（vanna-io org 无公开仓库） | **把评测集 golden SQL 变成在线 few-shot 库**——本项目 150 条评测集 + badcase 审核 golden SQL 现在只离线用，从不在线服务；Vanna 的"越用越准"正是靠这个闭环。0.x 代码 MIT 可读可借，但不可再作为运行时依赖 |
| **SuperSonic**（腾讯，Java） | ChatBI + Headless BI 双范式统一 | ① 语义层：Model/Metric/Dimension 统一建模；② **LLM 不直接写物理 SQL**，而是生成受限的语义查询语句（DSL），由 Semantic Translator 确定性翻译成物理 SQL；③ 七组件流水线：Knowledge Base → Schema Mapper（实体标注）→ Semantic Parser（规则+LLM 双解析器）→ Semantic Corrector → Semantic Translator → Chat Memory（历史查询轨迹召回做 few-shot）；④ 规则解析器优先、LLM 兜底的成本分层 | **指标 DSL 双引擎**：本项目已有 nl2sql_metrics 指标元数据，但只是塞进 prompt 让 LLM 写 SQL（口径由 LLM 保证）。改成「指标命中 → LLM 产 JSON DSL → 确定性编译 SQL」，口径由引擎保证 |
| **WrenAI** (AGPL-3.0) | GenBI 引擎 | ① MDL 语义建模文件（Git 版本化）；② LLM 生成 modeled SQL，SQL Planner 展开/编译成目标库可执行 SQL，借此支持 22+ 数据源；③ 流水线用 Haystack + Hamilton 重写支撑 1500+ 并发 | **语义层翻译器模式 + 多方言**：modeled SQL → executable SQL 的分层让多方言变成翻译器问题而不是 prompt 问题 |
| **DB-GPT** (MIT) | Agentic 数据助手全家桶 | AWEL 编排 text2sql 流、RAG（ChunkManager 切分 schema）、沙箱技能、报表生成 | 编排思想已被 LangGraph 覆盖；全家桶定位过重，不引入 |
| Dataherald | 早期开源 Text2SQL | 模板 + voice macros，社区已不活跃 | 仅作 landscape 参考 |

### 1.2 研究派（BIRD 榜多 Agent / SOTA 技术）

这些方法验证了「agent 化深度」带来的是可量化的准确率收益（MAC-SQL + GPT-4 在 BIRD 拿到 59.59% 当时 SOTA；XiYan-SQL 多生成器集成持平/超过 GPT-4o）：

- **MAC-SQL**：Selector（schema 筛选）/ Decomposer（复杂问题拆子问题）/ Refiner（执行报错喂回修正）三 agent 协作 —— 对应本项目的 filter_tables（已有）+ 缺失的问题分解 + 已有的 correct_sql 环。
- **XiYan-SQL**：Schema Linking → 多生成器候选 → 集成选择；M-Schema 紧凑 schema 表示（嵌套 JSON，比拼 DDL 字符串省 token 且结构清晰）。
- **CHASE-SQL**：多候选生成 + LLM judge + **基于执行结果并集的择优** —— 择优信号来自真实执行结果而非纯 LLM 判断。
- **CHESS**：关键词/列值/模式三路检索 + schema 剪枝 + **技能库**（语法模式库，与 Vanna 的 SQL 对库同源）。
- **DIN-SQL**：分解 + 分类难度路由（简单/复杂走不同 prompt 策略）——对应下面的 fast/deep 双路径。

### 1.3 官方范式：LangChain/LangGraph SQL Agent

LangChain 官方教程的标准形态：**工具调用 agent**（list_tables → get_schema → query_checker 校验 → db_query 执行 → 失败自主重试），LLM 自主决定下一步调什么工具，多轮循环。这是本项目"agent 能力为零"最直接的补课模板，且项目已用 LangGraph 1.x，迁移面最小。

---

## 二、现状差距分析

现状（详见 `docs/diagrams/architecture.md`）：LangGraph 9 阶段**静态**流水线（extract_keywords → 三路并行召回 → merge → filter → add_context → generate → validate → fix 环 → execute），无工具调用、无自主规划。

| 能力 | 主流方案做法 | 本项目现状 | 差距 |
|---|---|---|---|
| Agent 工具调用 | LLM 自主决定检索什么/何时够/失败怎么办 | 静态拓扑，唯一分支是纠错环 | **核心差距**：LLM 无法补检索、无法探查数据、无法换路 |
| Few-shot 示例库 | Vanna RAG / SuperSonic Chat Memory 在线召回问答对 | 150 条评测集 + badcase golden 只离线用 | 高性价比差距：原料全有，闭环没接上 |
| 语义层 | SuperSonic DSL→翻译器；WrenAI MDL→SQL Planner | 指标定义塞 prompt，LLM 自由发挥口径 | 口径一致性无保证 |
| 问题分解 | MAC-SQL Decomposer、DIN-SQL 难度路由 | 无，难题简单题同一流水线 | hard 题效果受限 |
| 澄清反问 | LangGraph interrupt / human-in-the-loop | 无，模糊问题硬猜 | 交互深度缺失 |
| 结果自评 | CHESS/CHASE-SQL 执行反馈择优；reflection | 执行成功即结束，空结果照推图表 | 最后一公里无质检 |
| 多方言 | WrenAI 22+ 数据源 | 硬编码 postgresql/16（`nodes/add_context.py:28`、`security.py` dialect 写死） | sqlglot 本身支持多方言，纯工程债 |
| 多轮对话 | 完整历史 + Chat Memory 召回 | 仅上一轮 question+sql 单次改写（`engine.py:278`） | 下钻/对比类问题弱 |

**保留资产（差异化，不放掉）**：
1. `security.py` sqlglot AST 四层防线（改写/只读/行级权限/敏感列）——上述所有开源方案都没有这层；
2. badcase 采集→审核→回流评测闭环——升级为在线学习闭环后价值翻倍；
3. 列值「真实值/同义词」双源标注（ES db/alias）——防 WHERE 值编造，是 CHESS 列值挖掘的强化版；
4. SSE 逐节点进度推送 + 断连止损。

---

## 三、目标架构

```
用户问题
   │
   ▼
┌─────────────────────┐   澄清反问(SSE `clarify` 事件 + LangGraph interrupt)
│ ① 意图路由            │──────────────────► 前端选项 → 写回 state → 继续
│  查询/元数据/闲聊/越权  │
└─────────┬───────────┘
          ▼ 数据查询
┌─────────────────────┐
│ ② 问题理解            │  全历史多轮改写 + 实体/指标/维度标注(Schema Mapper 式)
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│ ③ 双引擎路由          │  指标+维度全部命中语义层？──是──► ④A 指标 DSL 路径
└─────────┬───────────┘                                │
          │否（明细/复杂/hard）                         ▼
          ▼                                   ┌──────────────────────┐
┌──────────────────────────┐                  │ ④A LLM 生成受限 Metric │
│ ④B 检索 Agent(ReAct loop) │                  │    DSL(JSON)          │
│  tools:                  │                  │    ↓ 确定性编译(纯Python)│
│   search_columns/values/ │                  │    ↓ sqlglot AST 校验  │
│   metrics                │                  └─────────┬────────────┘
│   get_table_detail       │                            │
│   sample_column_values   │◄── Agent 自主决定检索深度            │
│   find_similar_examples  │                                    │
└─────────┬────────────────┘                                    │
          ▼ schema context                                      │
┌──────────────────────────┐        ┌──────────────────────────┐ │
│ ⑤ SQL 生成(受控专家节点)    │        │ ⑥ 安全防线(不变)           │◄┘
│  few-shot: 示例库 top-3    │───────►│  AST改写+EXPLAIN+只读事务  │
└──────────────────────────┘        │  +行级权限+敏感列过滤       │
          ▲                         └─────────┬────────────────┘
          │          fix 环(≤N) ◄─────────────┤ 执行报错/校验失败
          │                                   ▼
          │                         ┌──────────────────────┐
          └───── 重写触发 ◄──────────│ ⑦ 执行 + 结果自评       │
                                    │  空结果/量纲异常→反思    │
                                    └─────────┬────────────┘
                                              ▼
                                    ⑧ 图表推荐(规则优先,LLM兜底) + 摘要
                                              ▼
                                    ⑨ 回流: badcase→审核→golden SQL
                                       → 示例库(在线生效) + 评测集
```

### 3.1 主线 A：检索 Agent 化（LangGraph SQL Agent 模式 + Vanna 2.0 工具注册）

**半 agent 化**：检索阶段 LLM 自主（这是"深度"），SQL 生成保持受控 prompt（这是准确率）。与 LangGraph 官方教程的 agent + query_gen 节点 + check/execute 循环同构，与 MAC-SQL 的 Selector/Decomposer/Refiner 分工对应。

- 现有节点能力包装成 async tools（保留 `nodes/` 实现复用）：
  - `search_columns(query)` / `search_values(query)` / `search_metrics(query)` —— 复用 `nodes/recall_*.py`
  - `get_table_detail(table)` —— 展开某张表完整列清单（现无此能力）
  - `sample_column_values(table.column, limit)` —— agent 主动到业务库采样真实值（新增；LangChain 教程 sample rows 模式，只读+限流）
  - `find_similar_examples(question)` —— 示例库检索（主线 B）
- 图结构（重写 `graph.py`）：
  `intent_router → understand → retriever_agent(ReAct loop, tools 上表) → sql_generate → validate ⇄ fix → execute → self_check → END`
- **fast/deep 双路径**（DIN-SQL 难度路由 + SuperSonic 成本分层思想）：简单问题（单实体命中、向量召回置信度高、或与历史 SQL 编辑距离小）跳过 agent loop 直接走精简静态流水线；agent loop 只对召回置信度低/多表/hard 触发。延迟与成本不回退。
- `build_graph()` 改为由 `PIPELINE_SPEC` 生成（消除 `graph.py:84` 与手写 add_edge 的双份声明）。

### 3.2 主线 B：few-shot 示例库在线化（Vanna 核心机制，原料已有）

- 新增 Milvus collection `chatbi_{prefix}_examples`：向量 = question embedding（复用 text-embedding-v3），payload = `{question, golden_sql, category, source: eval|badcase}`。
- 数据源三合一：`eval/cases/*.json`（150+40 条）+ `*_reflow.json` + `chatbi_badcases` 中 `status=approved` 记录。
- 在线：`sql_generate` 前检索 top-3（score 阈值过滤）注入 prompt 的 few-shot 区块（`prompts/generate_sql.prompt` 增加示例段）。
- 新脚本 `scripts/sync_examples.py` 全量构建；badcase 审核通过时增量 upsert（`badcase_store.py` approve 事务内）。
- **闭环升级**：badcase 修复从「回归测试不退化」升级为「在线立即生效」——评测集变成生产资产。这是 Vanna「越用越准」的本质，也是本项目评测闭环的延长线。

### 3.3 主线 C：语义层双引擎（SuperSonic DSL→Translator 模式）

- 定义受限 Metric DSL（JSON）：
  ```json
  {
    "metrics": ["avg_build_duration"],
    "dimensions": ["team"],
    "filters": [{"field": "platform", "op": "=", "values": ["Android"]}],
    "time_range": {"preset": "last_30d"},
    "order_by": [{"metric": "avg_build_duration", "dir": "desc"}],
    "limit": 10
  }
  ```
- **语义翻译器**（纯 Python，零 LLM）：指标表达式从 `nl2sql_column_metrics`/指标 YAML 编译为 SELECT 子句，维度→GROUP BY，时间谓词复用 `add_context` 日期逻辑；按 dialect 出 SQL。
- 路由：②的实体标注结果中 metrics+dimensions 全部命中语义层 → 走 DSL 路径；否则走 agent SQL 路径。DSL 路径 LLM 只产 JSON（结构化输出），pydantic 校验 + 编译产物过 `security.validate_sql`。
- 收益：口径一致性（业界研究：GPT-4 直写 SQL 16.7% → 语义层 54.2%）；DSL 可单测；天然安全（受限表达，不经过 LLM 自由 SQL）；多方言免费获得。

### 3.4 主线 D：交互深度（意图路由 + 澄清反问 + 结果自评）

- **意图路由**（新节点）：数据查询 / 元数据问答（「有哪些指标/表」直接查元数据库回答）/ 闲聊 / 越权拒答。现状默认一切输入都是查询。
- **澄清反问**：实体歧义（同名列多表命中）、指标缺失、时间窗缺失 → LangGraph `interrupt()` → SSE 新事件 `clarify`（前端 `chatbi.html` 渲染选项）→ 答案写回 state 从断点继续。
- **结果自评**（`self_check` 新节点）：空结果 / 全 NULL / 量纲异常 → 一次「问题-SQL-结果」三角反思：要么换检索词重写，要么如实回复「未找到数据，是否指…」。空结果不再推图表。
- **多轮升级**：完整对话历史注入改写（ctx_store 已存历史，只是只用了上一轮）；中期换 LangGraph checkpointer。

### 3.5 工程修缮（顺手还债）

1. **多方言**：DSN→sqlglot dialect 映射；`security.validate_sql(sql, dialect)` 参数化；`add_context` 去掉硬编码 postgresql/16；`generate_sql.prompt` 方言段模板化；EXPLAIN 按 dialect 分派。sqlglot 本身支持主流方言，改动集中且小。
2. **合并双路径**：`engine.py` 旧 `/query` 路径与 graph 流水线并存 → 保留端点作 fast-path 入口，内部共享节点实现，删除重复的 2 轮重试逻辑。
3. Milvus 批量 search（`recall_columns.py` 现逐关键词串行往返）。
4. DSN 密码加密存储（现状明文，README 已知限制 #1）。
5. 图表推荐规则兜底（行/列数与图型匹配的确定性规则，LLM 仅在规则未命中时调用）。

### 3.6 时间对比与归因分析（"相邻两个月差距 / 为什么"类问题）

现状实测：`add_context` 只注入当前日期/星期/季度，无上月边界锚点；评测集 190 条中环比题为 0、归因类为 0——这两类问题当前是未定义行为，出错不会被门禁发现。

#### 3.6.1 环比差距：业界共识是"语义层一等公民，不让 LLM 算日期"

- **Cube**：`compareDateRange` 查询原语——一次请求声明多个时间范围，引擎分别聚合返回；
- **dbt MetricFlow**：派生指标 `offset_window` / `offset_to_grain`（`metric_a - metric_a_offset` 声明式定义环比指标），注意嵌套派生指标 offset 有已知 bug（metricflow#882）；
- **Power BI**：DAX 时间智能函数（SAMEPERIODLASTYEAR/DATEADD）；**Metabase**：Offset 表达式；
- **Quick BI 小Q问数**：多周期指标对比分析作为问数原生能力。

结论：没有一家主流方案让 LLM 在 SQL 里现推月边界。LLM 的职责只是把"上个月比上上个月"映射成语义层参数。

**落地**：
- P1：`add_context` 升级为**时间锚点生成器**——Python 预计算并注入上月/上周/去年同期精确边界字符串（如 `上月 = [2026-08-01, 2026-08-31]`）；`generate_sql.prompt` 补"差距 = 绝对差值还是百分比"的歧义消解规则；评测集补 15~20 条环比题让门禁可见。
- P2：语义层 DSL 的 `time_range` 扩展对比结构（命名借鉴 MetricFlow：`offset: {"unit": "month", "n": 1}`），翻译器确定性生成两期 CTE + 相减，口径显式声明。

#### 3.6.2 归因分析：三种流派，共识是"算法算贡献、LLM 写解释"

**流派一：多维归因算法派（先于 LLM，工业验证充分）**
- **Adtributor**（微软 NSDI'14）：单层维度贡献度 + 解释力（explanatory power）+ 惊喜度（surprise）筛选根因；论文实测人工排查 73 分钟 → 自动分析 3 分钟。核心实现仅几十行，是 V1 首选；
- **Squeeze**（清华 SIGMOD'21）：异常在维度父子层级的传播树 + 潜在分数排序；**HotSpot**（KDD'18）：蒙特卡洛树搜索，解维度组合爆炸；
- 国内实践：携程（异常检测+归因）、vivo（Adtributor 分层维度）、阿里云（指标拆解加法/乘法模型+决策树）、火山引擎智能数据洞察"维度归因"产品化。

**流派二：产品派——归因算法 + AI 总结的分工模式**
- **Quick BI 指标洞察**：自动化归因算法做多维度、多步骤根因分析，AI 只负责生成总结与可视化归因报告；**小Q解读**：识别异常波动/趋势/下钻；
- **网易有数 ChatBI**：自动计算各归因维度成员项的增长/下降值与贡献率，找出贡献率最高的维度值；
- **Tableau Pulse**：Insights Platform 洞察类型系统，自动检测 drivers/trends/outliers，NL+可视化表达；
- **Smartbi**：因果图谱自定义 + 多指标归因。

**流派三：Agent 派——无预定义维度时的自主探索**
- **ThoughtSpot Spotter**：agentic 分析师，"why did revenue drop last quarter" 是招牌场景，多步分支根因调查（探索→比较→追问）；
- **InsightPilot**（微软 EMNLP'23）：LLM agent + **确定性洞察引擎（insight engine）做 grounding**——洞察引擎先从数据提取可靠洞察再喂给 LLM 组织表达，防幻觉；动作集：explore / hypothesis test / verify / summarize。

**业界演进判断**（爱分析，2025-08）：ChatBI 归因从"基础版"（SQL 列出维度让用户自己判断）到"真正可用"（自动量化贡献 + AI 总结）——本项目现状连基础版都没有，可直接跳到"真正可用"档位。

**落地（P3 归因 Agent，取流派二的分工模式）**：

意图路由识别"为什么/原因/怎么回事"→ 归因 Agent 多步工具循环，每步 SQL 照走安全层：
1. 跑基线：两期总量 + delta（复用环比路径）；
2. 从语义层取该指标全部维度；
3. 逐维度跑 group-by 两期对比 SQL（每维度一条，两期一次查出）；
4. **确定性计算贡献度**（V1 用 Adtributor：Δᵢ/Δ_total + surprise 排序；维度组合爆炸再考虑 HotSpot/Squeeze）；
5. LLM 只做归纳（Quick BI/InsightPilot 模式）：top 贡献因素组织成解释，**每条结论附验证 SQL 与数字**（InsightPilot grounding），可点开下钻；
6. 前端输出瀑布图/贡献度条形图 + 结论卡片。

约束：维度 top-N≤5、每维度取值 top-M≤10、步数硬上限；措辞必须为"X 贡献了 Δ 的 62%"（统计贡献分解），禁止"因为 X 导致"式因果断言；delta 不显著或维度解释不了时如实说明——与"真实值/同义词"标注同一设计哲学：宁可诚实，不可编造。

---

## 四、分阶段路线图

> **实施进度（2026-09-19）**：P0 dialect 推断 ✅、P1 时间锚点 ✅（含 TIMESTAMP 半开区间规则）、
> P1 few-shot 示例库 ✅（example_store + sync_examples + 图第 4 路召回，防自泄漏剔除同题）。
> 验收：auto_full 41 题中此前实测必错的 AUF40（上月环比）与 AUF26（5 跳 JOIN）均翻正；
> 图路径（SSE 在线）端到端实测环比问题答对。
> 复核修正：security.validate_sql 方言参数化（原 parse/render 写死 postgres）、旧路径
> system prompt 去"医院"硬编码、示例库陈旧清理、recall_examples 状态收敛到三态契约。
> **已知残留（2026-09-19 复核后更新）**：
> ① 消融已完成（`--disable-examples`）：auto_full 41 题，完整 71% vs 禁示例 63% ——
> 示例库净 +3 题（AUF16/19/27，全是 TopN/JOIN 形态题），时间类两配置均 8/9（锚点独立有效）。
> 局限：消融只在 auto_full 上跑过（另一个数据源当时没有可用业务库），近义题泄漏仍小幅乐观。
> ② 示例含硬编码日期会随天数腐烂 —— sync 已加 lint 告警，golden 规范应优先相对时间写法。
> ③ 明细/TopN 判分对列集合过严（6 题为口径/严格度问题，非答错数据，策略待定）。
> ④ 危险函数黑名单已按方言叠加（mysql/duckdb/tsql/oracle，只增不减）；
> FORBIDDEN_PATTERNS 的 hospital/ALM 表级正则已收敛为「未配置 sensitive_columns 数据源」的兜底。
> ⑤ auto_full.yaml 单文件 155KB/127 表（脚本生成、勿手改，暂可维护）——元数据再增长需拆分
> （conf/projects/{code}.d/ 按子域分片）或元数据直读 DB，**列为架构决策项，未实施**。
> 未实施：P1 检索 Agent 化、P2/P3 全部。

| 阶段 | 内容 | 主要改动文件 | 验收（eval/run_nl2sql_eval.py --live，150 题门禁 0.8 不回退） |
|---|---|---|---|
| **P0 工程债**（先行，低风险） | 多方言参数化；合并双路径；PIPELINE_SPEC 消费；Milvus 批量检索 | security.py / add_context.py / graph.py / engine.py / router.py | 现有全部测试过；live 指标持平 |
| **P1 核心深度**（性价比最高） | 主线 B 示例库 + 主线 A 检索 agent 化（含 fast/deep 双路径、sample_column_values/get_table_detail 工具）+ 时间锚点生成器（环比边界预计算，评测集补 15~20 条环比题） | scripts/sync_examples.py（新）/ nodes/* / graph.py / prompts/generate_sql.prompt / repositories/ | live exec-match 目标 +5~10pp（few-shot 上线 + agent 补检索）；延迟 P95 不劣化超过 20%（双路径兜住） |
| **P2 结构升级** | 主线 C 语义层 DSL 路径 + 主线 D 意图路由/澄清反问（interrupt + SSE clarify 事件 + 前端渲染） | semantic/（新模块：DSL schema + 翻译器）/ graph.py / router.py / chatbi.html | 指标类问题 exec-match 单独分层统计提升；新增 20+ 条指标类案例入评测集；澄清场景 5 例 e2e |
| **P3 增强** | 多候选生成+执行择优（CHASE-SQL 式，仅 hard 触发）；结果自评 self_check；多轮记忆/checkpointer；**归因 Agent**（环比基线 + 维度贡献度分解 + 证据链解释，§3.6） | graph.py / nodes/self_check.py（新）/ nodes/attribution.py（新）/ echarts_builder.py（瀑布图） | hard 难度子集 exec-match 提升；空结果推图率→0；归因场景 5+ 例端到端（结论可被验证 SQL 复算） |

每阶段以 150 题评测集为 A/B 基线，badcase 回流持续喂养示例库（P1 起）。

---

## 五、为什么不整仓替换成 Vanna / WrenAI / SuperSonic

- **Vanna**：无 AST 安全防线、无评测闭环；其核心机制（示例 RAG）以 ~200 行接入成本拿来即可（主线 B）。
- **WrenAI**：AGPL-3.0 许可（商用传染风险）+ 全栈重（引擎/服务分离）；借鉴 MDL→SQL Planner 分层思想（主线 C），不引代码。
- **SuperSonic**：Java 体系，整体迁移等于换技术栈；DSL→翻译器模式与语义层建模思路直接对应借鉴。
- **DB-GPT**：多模型管理/RAG/报表全家桶，定位过重。
- 本项目的差异化资产（安全层、评测回流、值级双源标注）恰是上述项目普遍缺失的，大改的方向是**在自有骨架上补 agent 深度**，而非推倒。

## 六、风险与取舍

1. **Agent loop 的延迟/成本**：fast/deep 双路径控制；agent 循环设硬上限（步数≤6）；全链路 LLM 调用已有熔断（safe_ainvoke）。
2. **Agent 路径不确定性**：SQL 生成保持受控节点（不交给 agent 自由写）；示例库检索有 score 阈值；评测门禁每阶段把关。
3. **DSL 路径覆盖不全**：路由 miss 时自然回退 agent SQL 路径，DSL 路径纯增量收益。
4. **多候选的成本**：仅 difficulty=hard 且首轮失败触发，N=3 上限。

## 七、参考

- Vanna: https://github.com/vanna-ai/vanna （2.0 Agent 架构：ToolRegistry / ContextEnricher / user-aware tools）
- SuperSonic: https://github.com/tencentmusic/supersonic （ChatBI + Headless BI，Schema Mapper/Semantic Parser/Translator/Chat Memory）
- WrenAI: https://github.com/Canner/WrenAI （MDL 语义引擎 + SQL Planner，AGPL）
- DB-GPT: https://github.com/eosphoros-ai/DB-GPT （AWEL 编排）
- MAC-SQL（ACL 2025）/ XiYan-SQL（arXiv 2024）/ CHASE-SQL / CHESS / DIN-SQL：BIRD 榜多 Agent 与候选集成方法
- LangChain 官方 SQL Agent 教程: https://docs.langchain.com （agent + query_checker + db_query 工具循环）
- 语义层收益研究（GPT-4 16.7%→54.2%）: 见 VentureBeat 2025-12 关于 semantic layer 的报道

---

## 八、最新可用 Agent 方案盘点（GitHub API 实测，2026-09-19）

星数/最近推送/license/归档状态均为当日 GitHub API 核查结果。

### 8.1 Text2SQL / ChatBI 类

| 仓库 | 实测状态 | License | 判定 |
|---|---|---|---|
| Canner/WrenAI | 17.7k★，2026-09-18 仍在提交，最活跃 | AGPL-3.0 | MDL/SQL Planner 思想可借，代码不可并入 |
| eosphoros-ai/DB-GPT | 20.0k★，2026-09-16 提交 | MIT | **唯一可自由借代码的活跃大库**（AWEL/agent/RAG 实现） |
| tencentmusic/supersonic | 5.1k★，2026-09-08 提交 | 非标准 license 文件（商用前确认） | DSL/语义层设计参考（Java 体系） |
| XGenerationLab/XiYan-SQL | 1.0k★，2026-05 后放缓 | Apache-2.0 | M-Schema 紧凑表示 + 多生成器集成思想可借 |
| crystaldba/postgres-mcp | 3.3k★，2026-08 提交 | MIT | Postgres MCP Pro：MCP 工具层现成实现，组织方式可参考/复用 |
| vanna-ai/vanna | 23.8k★，**2026-02 已归档**；2.0 闭源未发布（vanna-io org 无公开仓库） | MIT | 0.x RAG 机制可读可借；不可作为依赖引入 |
| defog-ai/sqlcoder | 4.0k★，2024-05 停更 | Apache-2.0 | 微调模型路线已过时，不采用 |

### 8.2 Agent 编排框架

| 框架 | 实测状态 | License | 对本项目 |
|---|---|---|---|
| **langchain-ai/langgraph** | 41.9k★，2026-09-18 提交 | MIT | **基座不换**：text2sql agent 仍是 LangGraph 主场，项目已用 1.1.6 |
| **langchain-ai/deepagents** | 29.5k★，2026-09-18 提交，当前最热的新模式 | MIT | Deep Agents 三件套（planning tool / 虚拟文件系统 / subagent 编排），可直接依赖；用于开放式探索场景（见 8.3-2） |
| openai/openai-agents-python | 29.6k★，活跃 | MIT | 观察；迁移无净收益 |
| pydantic/pydantic-ai | 20.0k★，活跃 | MIT | 观察 |
| huggingface/smolagents | 29.4k★，2026-08 提交 | Apache-2.0 | 代码型 agent，与 SQL 场景不匹配 |
| crewAIInc/crewAI | 58.7k★，活跃 | MIT | 角色编排范式，对本项目过重 |
| google/adk-python | 21.6k★，活跃 | Apache-2.0 | 绑 Google 生态 |
| anthropics/claude-agent-sdk-python | 8.1k★，活跃 | MIT | 绑 Anthropic 模型 |
| microsoft/autogen | 61.1k★，**2026-04 后未推送** | CC-BY-4.0（仓库） | 热度下降，不选 |
| coze-dev/coze-studio | 21.6k★，2026-07 提交 | Apache-2.0 | 可视化平台，非嵌入库 |
| modelcontextprotocol/servers | 90.5k★ | — | MCP 已成 agent↔数据源连接事实标准 |

### 8.3 采用结论

1. **基座不换**：LangGraph（活跃度、已有投入、MIT、text2sql agent 生态主场）。
2. **新模式分场景引入 deepagents**：结构化归因（§3.6.2 Quick BI 模式）保持 LangGraph 确定性图——可评测、可控；**开放式探索**（用户对归因结论追问"那再看下 X 维度呢"的 Spotter 式场景）用 deepagents（planning todo 管分析步骤、虚拟文件系统存中间 SQL 结果、task 工具派 subagent 逐维度并行跑数），MIT 可直接 `pip install deepagents`。
3. **MCP 化对外暴露**：把 rd-chatBI 工具层（search_columns/values/metrics、validate_sql、execute_sql——全部过安全层）发布为 MCP server，任何 MCP 客户端（Claude/Cursor/WrenAI）可"治理式"查数——参考 Postgres MCP Pro 的工具组织方式。安全层从内部防线变成对生态的卖点，这是接入主流生态的最低成本路径。
