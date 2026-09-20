# rd-chatBI — NL2SQL 智能分析平台

> 📖 **HTML 版文档**：[README.html](README.html) ｜ [面试讲解稿 INTERVIEW.html](INTERVIEW.html) ｜ [**面试速记（口述版）SPEAK.html**](SPEAK.html) ｜ [架构图](docs/diagrams/architecture.html)
>
> 🚗 **默认数据源是 `auto_full`**：汽车全域 **127 张表 / 13 子域 / 208 万行种子数据**
> （`scripts/gen_auto_full.py` 生成），考验召回环节在表多且语义相近时的 discrimination。
> `hospital_demo` 保留为第二数据源，演示「同一份代码换一套角色模型」（config 与案例在本仓库）。
> 由 `npm run docs` 从同名 `.md` 生成（改了 md 记得重跑）。

自然语言查业务库：多数据源路由 + 9 阶段 NL2SQL 流水线（LangGraph）+ 四层 SQL 安全防线 + 图表推荐渲染（服务端产出 ECharts option，前端渲染）。

## 架构

```
src/
  main.py               FastAPI 入口（CORS / 指标 / 静态页 / /health /metrics
                        / jieba 预热 + 业务词典 + 连接池生命周期）
  core/                 配置、认证、限流、熔断器、日志（trace_id + 审计）、Prometheus 指标
  infra/                PG（元数据库/业务库只读，按 event loop 分区池化）、Redis、Milvus、ES、多数据源注册表
  nl2sql/
    router.py           /api/v1/bi/*（query、query-stream SSE、history、datasources）
    graph.py            LangGraph 图定义（单一来源）：召回(4路并行)→过滤(2路并行)
                        →生成→校验→(纠错回环)→执行
    security.py         SQL 安全部（见下）
    ctx_store.py        对话历史存储（redis / memory 双后端）
    llm_text.py         LLM 输出清洗 + safe_ainvoke（熔断 + token 指标收口）
    dict_loader.py      jieba 业务词典（字段别名/指标名 → 分词器）
    nodes/              各流水线节点
  conf/projects/*.yaml  每数据源一份：表/列/指标元数据 + 角色规则 + 敏感列
scripts/                demo 数据初始化、元数据三层构建（PG + Milvus + ES）、案例校验
eval/                   离线评测门禁 + 实况执行准确率（exec-match，按数据源分案例文件）
```

## 快速开始

```bash
cp .env.example .env          # 填 DASHSCOPE_API_KEY 等
python -m venv .venv && .venv/bin/pip install -r requirements.txt
# 无需外部网络：本项目 compose 自带 PG / Redis / Milvus / ES
docker compose -f docker/docker-compose.yml up -d --build   # 自带基础设施 + 一次性元数据构建
uvicorn src.main:app --port 8003
```

默认数据源 `auto_full`（汽车全域）；角色 admin / engineer（按工厂）/ sales（按大区）/ customer。

脚本与评测：

```bash
.venv/bin/python -m pytest                          # 单测（211 条）
.venv/bin/python eval/run_nl2sql_eval.py            # 离线门禁：全部案例（当前 97 条）过安全层（CI 可跑）
.venv/bin/python eval/run_nl2sql_eval.py --live     # 实况准确率（需 LLM + 业务库）
.venv/bin/python eval/run_nl2sql_eval.py --live --project auto_full   # 只跑单个数据源
.venv/bin/python scripts/verify_cases.py            # 校验案例 golden SQL 在真实库上可执行
```

评测案例按数据源分文件：`eval/cases/nl2sql_cases_auto_full.json`（百表库，63 条）、
`eval/cases/nl2sql_cases_auto_full.json`（auto_full，97 条）。
`--project` 默认 `all`，跑全部有案例的数据源。

压测库的生成与接入（代码零改动，就是标准多数据源流程）：

```bash
.venv/bin/python scripts/gen_auto_full.py                       # 建库 + DDL + 208 万行种子 + YAML
.venv/bin/python scripts/build_nl2sql_meta.py --datasource auto_full --rebuild
.venv/bin/python scripts/link_fk_labels.py --datasource auto_full
.venv/bin/python scripts/sync_examples.py --datasource auto_full      # few-shot 示例库（评测集+badcase golden）
.venv/bin/python eval/run_nl2sql_eval.py --live --project auto_full   # 百表压测实况
```

### P1 已实施：时间锚点 + few-shot 示例库（详见 docs/agent-upgrade-plan.md）

- **时间锚点**：相对周期（上月/上上个月/上周/近30天…）边界由 Python 预计算注入 prompt，
  LLM 只抄不算；TIMESTAMP 列强制半开区间写法。防"NOW()-interval 当自然月"静默错误。
- **few-shot 示例库**：评测集 golden SQL + 审核通过的 badcase 向量化进 Milvus
  （`chatbi_{prefix}_examples`），生成 SQL 前检索相似问题注入 prompt ——
  badcase 修复后重跑 `sync_examples.py` 即在线生效。检索自动剔除与当前问题完全相同的示例（防评测自泄漏）。
- **dialect 从业务库连接推断**（P0）：接入 MySQL/DuckDB 等不再需要改 add_context/prompt。

### badcase 回流（让题库自己长大）

线上真实查询持续回流成待审案例，人工审过后进评测集 —— 题库不是人肉堆出来的。

```
线上失败/被拒/空结果 ─┐
前端「答得不对」按钮 ─┼→ chatbi_badcases（待审队列）─→ 人工审核 ─→ eval/cases/*_reflow.json ─→ 离线门禁
会话历史批量挖掘 ─────┘                                                      （git 提交，可 review）
```

| 采集来源 | 触发点 | 说明 |
|---|---|---|
| `api_error` | `pipeline.py` 收尾 + `/query` 返回前 | 失败/被拒/超时/0 行，自动落库；采集失败不影响查询（fail-open） |
| `manual` | 前端 👎 按钮 → `POST /api/v1/bi/badcases` | ★ 服务端从会话历史反查 predicted_sql/角色/行数，**不信前端传参** |
| `history` | `scripts/mine_badcases.py` | 扫 Redis 会话历史补录（历史 7 天 TTL 会过期） |

- **去重**：`sha256(datasource_id|归一化问题)` 唯一约束 + `ON CONFLICT` upsert。同一条问题反复踩只累加 `seen_count`，**人工写的 golden_sql / 分类 / 状态永不被机器覆盖**。
- **审核**：`GET/PATCH /api/v1/bi/badcases`，页面 `GET /review`。鉴权用独立的 `ADMIN_TOKEN`（不与 `METRICS_TOKEN` 共用 —— 能抓指标不该等于能改评测集）；未配置 + header 模式下 fail-closed 拒绝。
- **导出**：`python scripts/export_badcase_cases.py --write` 生成 `*_reflow.json`，`git commit` 后加 `--mark-exported` 更新状态。导出前逐条校验（golden 过安全层、分层合法、敏感列数据源禁止 `SELECT *`），一条脏数据就拒绝整批。
- **首次接入**：`python scripts/export_badcase_cases.py --seed-from-export` 把现有案例灌进表，让 DB 从第一天就是评测集的真相来源。

## 安全模型

四层防线，外加数据源级敏感列配置（`conf/projects/*.yaml` → `datasource.sensitive_columns`）：

| 层 | 机制 |
|---|---|
| Prompt | 敏感列不在元数据中定义，LLM 看不到 |
| 文本/AST | `security.validate_sql`：SELECT-only、单语句、LIMIT AST 钳制、敏感列名拦截 |
| 执行 | 只读事务 + statement_timeout + `filter_result_columns` 结果列过滤（堵 `SELECT *`） |
| 数据库 | 只读副本 / 只读账号 |

行级权限：`bi_datasources.role_rules` 数据驱动（default deny），认证用户参数经 sqlglot AST 编码后**无条件** AND 注入 —— SQL 里已出现同列条件也不会跳过（防用户诱导 LLM 写任意值越权）。

### 认证（AUTH_MODE）

- `header`（默认，开发/内网）：信任网关透传的 `X-User-*` 头。**网关必须剥离外部请求携带的身份头**，否则任何人可伪造角色。
- `jwt`（生产推荐）：验签 `Authorization: Bearer <token>`（HS256），claims 映射：`sub`→user_id、`role`、`project_id`、`dept_id`、`owner_domain_id`、`business_line`、`sid`→session_id。`JWT_SECRET` 未配置时 fail-closed 拒绝所有请求。

### 已知限制

- `bi_datasources.dsn` 与 `conf/projects/*.yaml` 的 DSN 含明文口令，生产应改用密钥管理服务注入。
- 元数据与业务库表结构是**双写**：业务库改了结构，元数据不会自动跟进，需重跑构建脚本。目前无变更检测。

### 已解决（原「已知限制」）

- **对话上下文**：已支持 `CONVERSATION_BACKEND=redis`（跨 worker 共享、带 TTL 自动过期）；Redis 故障时降级进程内历史（fail-open，多轮理解变弱但不影响查询本身）。
- **PG 连接池**：`src/infra/pool.py` 按 **(event loop, dsn) 分区池化** —— `pool_size=5, max_overflow=10`，同一 loop 内连接复用，跨 loop 隔离，跨数据源也隔离。实测确认：建 engine 与 loop 无关，**连接**绑 loop（复用会报 `Future ... attached to a different loop`），所以隔离粒度是 engine/池。★ 分区键必须同时含 dsn：只按 loop 分区时，同一 loop 里**第一个创建的 engine 会被所有数据源复用**（先建元数据库的池，查 hospital_demo 就会连着元数据库跑，报 `relation ... does not exist`）。`DB_POOL_SIZE<=0` 可退回 NullPool。
- **LLM 熔断器**：`src/core/circuit_breaker.py`，连续失败 5 次打开、30s 冷却、半开态只放 1 个探针；指标 `circuit_breaker_state_changes_total` 已接线（此前定义了没人 inc）。所有 LLM 调用收口到 `llm_text.safe_ainvoke`（有测试防止漏接）。
- **jieba 业务词典**：启动时把字段别名/指标名注册进 jieba 用户词典（`src/nl2sql/dict_loader.py`），业务词不再被切错。

## 生产清单

- `AUTH_MODE=jwt` + 强随机 `JWT_SECRET`
- `APP_ENV=prod`、`APP_DEBUG=false`、`DB_ECHO=false`、`LOG_LEVEL=INFO`
- `CORS_ORIGINS` 配具体来源白名单
- `RATE_LIMIT_BACKEND=redis`（跨 worker 限流）
- `CONVERSATION_BACKEND=redis`（跨 worker 共享对话历史）+ 合理的 `CONVERSATION_MAX_AGE_SECONDS`
- 连接池按需调 `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`（默认 5/10，按 PG 的 max_connections ÷ worker 数估算）
- 熔断器保持 `CIRCUIT_BREAKER_ENABLED=true`；对 `circuit_breaker_state_changes_total` 配告警
- CORS/限流/健康检查已在 docker-compose 配好：应用容器非 root 运行、带 healthcheck；ES 只绑 127.0.0.1
- 元数据结构变更后 `REBUILD_META=true docker compose up`（默认增量 upsert，不重复烧 embedding）
- 配 `ADMIN_TOKEN`（题库审核写权限），否则 header 模式下审核接口 fail-closed 不可用；`/review` 与 `GET/PATCH /api/v1/bi/badcases*` 需要它
- 关注 `chatbi_badcases` 的 pending 积压（`GET /api/v1/bi/badcases/stats`）—— 没人审的话这张表会变成垃圾场
