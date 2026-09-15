# rd-chatBI 架构图
> 本文件只放 Mermaid 源码，便于 GitHub / GitLab / Obsidian / mermaid.live 直接渲染。
> 讲稿正文见 [`../INTERVIEW.md`](../INTERVIEW.md)。
>
> **本地直接看图**：打开 [`architecture.html`](architecture.html)（浏览器渲染，可导出 PNG）。
> **可打印 HTML 版**：[architecture_print.html](architecture_print.html)（由本文件生成）；
> **已导出的图片**：[`png/fig0.png`](png/fig0.png)（总览）~ [`png/fig4.png`](png/fig4.png)，
> 由 `mmdc`（mermaid-cli）从本文件的代码块生成，`-s 2` 即 2 倍分辨率：
> ```bash
> npx @mermaid-js/mermaid-cli -i figN.mmd -o figN.png -b white -s 2 \
>   -p puppeteer.json -c mermaid-theme.json   # 配色必须传，否则用内置低对比主题
> ```

## 图 0 — 一句话总览（先看这张，再看细节）

```mermaid
flowchart LR
    Q["自然语言提问<br/>「目前有多少个未关闭的严重问题单」"]
    R["① 召回<br/>表 / 字段 / 指标 / 枚举值<br/>（向量 + 全文检索）"]
    F["② 过滤<br/>LLM 选出最小集合<br/>（省 token、防选错表）"]
    G["③ 生成 SQL<br/>LLM 拿元数据 + 日期生成"]
    V{"④ 安全校验<br/>能不能过？"}
    E["⑤ 执行<br/>只读事务 + 行级权限注入<br/>+ 结果列过滤"]
    C["纠错<br/>拿 DB 报错回炉（1 轮）"]
    O["返回<br/>数据表 + 图表 + 文字摘要"]

    Q --> R --> F --> G --> V
    V -->|通过| E --> O
    V -->|不通过| C
    C -.->|"修完必须重新校验"| V

    style V fill:#fee2e2,stroke:#b91c1c,stroke-width:2px
    style E fill:#fef3c7,stroke:#b45309,stroke-width:2px
    style O fill:#dcfce7,stroke:#15803d
```

> 记住这一条线，细节都是它的展开：**召回 → 过滤 → 生成 → 校验 →（纠错）→ 执行**。
> 下面三张图分别是：架构全景、流水线细节、安全防线细节。

## 图 1 — 整体架构（请求链路 + 存储分工）
```mermaid
flowchart TB
    FE["前端<br/>src/static/chatbi.html<br/>Vue + ECharts CDN"]

    subgraph API["FastAPI　src/main.py"]
        direction TB
        MW["中间件链<br/>TraceLogging → Prometheus → CORS"]
        subgraph DEPS["依赖注入链　router.py"]
            direction LR
            D1["get_current_user<br/>UserContext<br/>role / owner_domain_id / project_id"]
            D2["enforce_rate_limit<br/>Redis ZSET 滑动窗口<br/>20 req / 60s / user"]
            D3["get_project_dw<br/>按 project_id 打开<br/>只读业务库 session"]
        end
    end

    subgraph ORCH["编排层　graph.py（定义单一来源）"]
        direction LR
        A1["astream → pipeline.py<br/>SSE 逐节点推送（在线）"]
        A2["ainvoke → run_nl2sql_graph<br/>一次拿完整终态（离线评测）"]
    end

    subgraph PIPE["9 阶段流水线"]
        P["见下方图 2"]
    end

    subgraph STORE["存储分工"]
        direction LR
        S1[("PostgreSQL<br/>rd_chatbi 元数据<br/>chatbi_demo 业务库·只读")]
        S2[("Milvus<br/>chatbi_columns<br/>chatbi_metrics<br/>语义检索")]
        S3[("Elasticsearch<br/>chatbi_{prefix}_values<br/>真实枚举值 + 同义词")]
    end

    FE -->|"POST /api/v1/bi/query-stream<br/>X-User-* / X-Project-Id"| MW
    MW --> DEPS
    DEPS --> ORCH
    ORCH --> PIPE
    PIPE <--> S1
    PIPE <--> S2
    PIPE <--> S3

    style PIPE fill:#e8f4ff,stroke:#0369a1,stroke-width:2px
    style STORE fill:#fff7e6,stroke:#b45309
    style API fill:#f0fdf4,stroke:#15803d
```

## 图 2 — 9 阶段流水线主干（并行 / 条件 / 回环）
```mermaid
flowchart TB
    START(("用户提问<br/>各责任域的问题单数量排名<br/>role=engineer, owner_domain_id=1"))

    N1["① extract_keywords　【纯 CPU】<br/>jieba TF-IDF + 词性白名单 topK=10<br/>原句插到首位兜底"]

    subgraph RECALL["② 三路并行召回（各写 state 不同 key，互不冲突）"]
        direction LR
        N2A["②a recall_columns<br/>【LLM】扩展关键词<br/>→【Embedding】批量向量化<br/>→【Milvus】chatbi_columns<br/>top_k=5, threshold=0.6"]
        N2B["②b recall_values<br/>【LLM】扩展关键词<br/>→【ES】两步检索<br/>①命中词找到列 ②带回该列真实值<br/>size=10"]
        N2C["②c recall_metrics<br/>【LLM】扩展关键词<br/>→【Embedding】批量向量化<br/>→【Milvus】chatbi_metrics<br/>top_k=5, threshold=0.6"]
    end

    N3["③ merge_info　【PG】<br/>★三条入边 = 自动等待全部完成<br/>从指标定义反查 relevant_columns<br/>补全量列 + 主键/外键（JOIN 必需）<br/>把召回的值挂到列的 examples<br/>冷启动兜底：一无所获 → 回退全量表"]

    subgraph FILTER["④ 两路并行过滤"]
        direction LR
        N4A["④a filter_tables　【LLM】<br/>表结构→YAML，选最小列集合<br/>主外键强制保留<br/>解析失败 → 保留全量（降级）"]
        N4B["④b filter_metrics　【LLM】<br/>指标列表→YAML，选最小集合<br/>解析失败 → 保留全量（降级）"]
    end

    N5["⑤ add_context　【纯 CPU】<br/>注入 date / quarter / weekday / PG 版本<br/>不加这个，LLM 算不出「上个月」"]
    N6["⑥ generate_sql　【LLM】<br/>System：表结构 YAML + 指标 YAML<br/>+ 日期 + 安全规则 + 6 条规则<br/>Human：用户原问题"]

    N7["⑦ validate_sql　★安全闸门★<br/>security.validate_sql()<br/>+ EXPLAIN 预执行（只解析不执行）<br/>★返回值是重写后的 SQL，必须用返回值"]

    N8["⑧ correct_sql　【LLM】<br/>把 DB 报错喂回 LLM 修<br/>预算 MAX_SQL_FIX_ROUNDS = 1"]
    REJECT(("拒绝执行<br/>不返回任何数据"))

    N9["⑨ execute_sql<br/>行级过滤注入 → 只读事务执行<br/>→ 结果列过滤 → LLM 摘要"]

    CHART["图表　router.py 消费端<br/>recommend_chart【LLM】→ to_echarts_option【pandas】"]
    DONE(("SSE 返回<br/>SQL + 数据表 + 图表 + 摘要"))

    START --> N1 --> N2A & N2B & N2C
    N2A & N2B & N2C --> N3
    N3 --> N4A & N4B
    N4A & N4B --> N5 --> N6 --> N7

    N7 -->|"error 为空<br/>校验通过"| N9
    N7 -->|"error 非空<br/>且有纠错预算"| N8
    N7 -->|"预算用尽仍报错"| REJECT
    N8 -.->|"★纠错产物同样是 LLM 输出<br/>必须回 ⑦ 复检"| N7

    N9 --> CHART --> DONE

    style N7 fill:#fee2e2,stroke:#b91c1c,stroke-width:3px
    style N9 fill:#fef3c7,stroke:#b45309,stroke-width:2px
    style RECALL fill:#e8f4ff,stroke:#0369a1
    style FILTER fill:#e8f4ff,stroke:#0369a1
    style REJECT fill:#fee2e2,stroke:#b91c1c
    style DONE fill:#f0fdf4,stroke:#15803d
```

## 图 3 — 四层 SQL 安全防线（数据在每层被拦什么）
```mermaid
flowchart TB
    subgraph L1["第一层　Prompt 层"]
        A1["敏感列不在元数据里定义<br/>LLM 压根看不到这些列"]
        A1R["挡：从源头消除<br/>（LLM 不知道 = 不会主动查）"]
    end

    subgraph L2["第二层　文本 / AST 层　security.validate_sql　★核心★"]
        B0["剥尾部 -- 注释<br/>（跳过字符串字面量内的 --）"]
        B1["sqlglot 解析成 AST"]
        B2["① 单语句？<br/>len(statements) 大于 1 → 拒绝"]
        B3["② SELECT-only？<br/>非 exp.Select → 拒绝<br/>SELECT ... INTO → 拒绝"]
        B4["③ 去注释文本跑<br/>FORBIDDEN_PATTERNS"]
        B5["④ AST 检查敏感列引用<br/>_references_sensitive_column"]
        B6["⑤ AST 检查危险函数<br/>pg_read_file / set_config<br/>/ dblink* / lo_import"]
        B7["⑥ _clamp_row_limit<br/>所有 LIMIT / FETCH 压到 100<br/>非字面量也覆盖｜嵌套子查询一并处理"]
        B0 --> B1 --> B2 --> B3 --> B4 --> B5 --> B6 --> B7
    end

    subgraph L3["第三层　执行层"]
        C1["apply_role_filter<br/>数据驱动 role_rules<br/>★无条件 AND 注入，不去重<br/>参数经 exp.convert 编码"]
        C2["setup_readonly_session<br/>statement_timeout = 10s<br/>default_transaction_read_only = on"]
        C3["filter_result_columns<br/>★堵 SELECT *<br/>必须在 generate_summary 之前"]
        C1 --> C2 --> C3
    end

    subgraph L4["第四层　数据库层"]
        D1["只读副本 / 只读账号<br/>前三层全被绕过时的最后兜底"]
    end

    SQL["LLM 生成的 SQL<br/>（不可信输入）"]
    OUT["执行结果 + 摘要"]

    SQL --> L1 --> L2 --> L3 --> L4 --> OUT

    style L2 fill:#fee2e2,stroke:#b91c1c,stroke-width:2px
    style L3 fill:#fef3c7,stroke:#b45309,stroke-width:2px
    style L4 fill:#f0fdf4,stroke:#15803d
```

## 图 4 — SSE 事件流（queue 桥接）
```mermaid
sequenceDiagram
    autonumber
    participant C as 客户端<br/>EventSource
    participant R as router.py<br/>event_stream
    participant Q as asyncio.Queue
    participant P as 流水线 task<br/>run_pipeline
    participant N as graph 节点

    C->>R: POST /query-stream
    R->>R: resolve_question（多轮改写）

    par 生产者（流水线 task）
        loop 每个节点
            N-->>P: ctx["writer"]({type:progress,...})
            P->>Q: put(进度事件)
            N-->>P: 节点完成 return {...}
            P->>Q: put({node_name: result})
        end
        P->>Q: put(None)
    and 消费者（event_stream）
        loop 直到 sentinel
            Q->>R: get()
            R-->>C: data: {"node":...,"step":n,"data":{...}}
        end
    end

    note over R,C: execute_sql 事件到达时<br/>额外触发图表推荐与 ECharts option 构建
    note over R,P: 客户端断连 → finally task.cancel()<br/>否则剩余 LLM 调用会全跑完，白烧钱
```
