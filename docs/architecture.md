# kb-agent 架构设计

> 状态：与 [PLAN v4](../PLAN.md) 对齐。L1 已实现；P0 治理/运行时开关、L2/L3 为规划中。
> 定位：个人可部署的**知识库助手 / 知识库组件**，不是通用自主 agent；不执行 shell、HTTP、文件系统、浏览器或任意代码。
> 配套：[愿景](./vision.md) · [L1→L3 拆解](./mvp-l1-to-l3.md) · [知识治理](./knowledge-governance.md) · [ADR 0001 存储选型](./adr/0001-storage.md)

---

## 1. 设计原则

1. **源文件是唯一事实来源**：Markdown / PDF / 网页；所有索引与学习数据都是派生物，可重建
2. **核心轻量**：摄入、整理、检索、问答始终可用，不依赖学习扩展
3. **扩展可选**：概念、题库、队列、周报默认关闭，支持热切换，失败不阻塞核心
4. **单向依赖**：核心不依赖 `study`；`study` 依赖核心
5. **单机优先 + 组件化**：默认本地部署；通过 REST / Python API 作为其他 AI 的知识库组件
6. **受控能力面**：管理操作限于白名单命令；不提供通用工具调用

---

## 2. 系统上下文

```mermaid
flowchart LR
    U["用户"] -->|"CLI / Web / Chat"| KB["kb-agent"]
    AI["其他 AI 系统 / 组件"] -->|"REST API"| KB
    KB -->|"embedding / rerank"| M["本地模型<br/>bge-small / bge-reranker"]
    KB -->|"抽取 / 出题 / 判分 / 问答"| L["LLM 服务<br/>OpenAI 兼容"]
    KB <-->|"读写"| D[("本地数据<br/>源文件 / SQLite / Chroma / media")]
```

**边界**：

- 只有 LLM / 本地模型两类外部调用；没有 shell、HTTP 抓取、浏览器、代码执行工具
- 默认只监听 `127.0.0.1`；对外集成通过 REST，只读 Key 与 admin Key 分权

---

## 3. 分层架构

```mermaid
flowchart TB
    subgraph IF[接口层]
        CLI["CLI (kb)"]
        API["REST API"]
        LIB["Python API"]
        WEB["Web / Chat"]
    end

    subgraph CORE[核心知识库 常开]
        ING["ingestion"]
        VEC["vectorstore"]
        DOC["document_store"]
        RET["retrieval"]
        GEN["generation"]
        VIS["vision"]
        EVA["eval"]
    end

    subgraph GOV[治理 P0]
        META["docs_meta / doc_events"]
        SCOPE["scope / 时效 / 审计"]
    end

    subgraph INTEL[智能层 可选]
        CON["概念抽取 / 链接 / 消歧"]
        FOC["焦点感知"]
        COV["覆盖度 / 参照系"]
    end

    subgraph LEARN[学习扩展 可选 默认关]
        ITEM["题库"]
        QUEUE["每日队列"]
        MAST["掌握度 / 调度"]
        REP["周报 / 概览"]
    end

    subgraph CROSS[横切]
        FLAG["配置 / 运行时开关"]
        SEC["安全 / 审计"]
        OBS["日志 / 指标"]
    end

    IF --> CORE
    CORE --> GOV
    CORE --> INTEL
    INTEL --> LEARN
    ING --> VEC
    ING --> DOC
    RET --> VEC
    RET --> DOC
    GEN --> RET
    GOV -.->|"doc_events"| INTEL
    INTEL -.->|"派生数据"| LEARN
    CROSS -.-> CORE
    CROSS -.-> LEARN
```

**核心 vs 扩展**：

| 层 | 内容 | 默认 | 失败影响 |
| --- | --- | --- | --- |
| 核心知识库 | 摄入 / 检索 / 问答 / 管理 / 评估 | 常开 | 主流程 |
| 治理 | scope / 时效 / 事件 / 审计 | 常开（P0） | 数据一致性 |
| 智能层 | 概念、焦点、覆盖度 | 可关 | 不影响检索 |
| 学习扩展 | 题库、队列、掌握度、周报 | 默认关 | 不影响核心 |

---

## 4. 功能模块

| 模块 | 职责 | 状态 |
| --- | --- | --- |
| `ingestion/` | loaders → cleaner → chunker → indexer；哈希去重、更新/删除 | ✅ v0.1.0 |
| `embedding/` | 本地 CPU 嵌入（bge-small-zh） | ✅ |
| `vectorstore/` | `VectorStore` Protocol + Chroma 适配器 | ✅ |
| `document_store/` | `DocumentStore` Protocol + SQLite 父文档库 | ✅ |
| `storage/` | 后端中立 Filter、工厂、契约测试 | ✅ |
| `retrieval/` | 意图/改写 → 检索 → 重排 → 父文档扩写 | ✅ |
| `generation/` | LLM 客户端 + RAG 管线（编号引用、多模态） | ✅ |
| `vision/` | 图片提取 → caption → OCR → 图文单元 | ✅ |
| `eval/` | 评测集、指标、LLM-as-judge、对比报告 | ✅ |
| `api/` / `ui/` / `cli.py` | REST / Gradio / CLI | ✅ |
| `kb.py` | `KnowledgeBase` 门面（线程安全） | ✅ |
| 治理表 + consumer | `docs_meta`、`doc_events`、scope、`answer_log`、`audit_log` | 🟡 P0 |
| `FeatureFlags` | `runtime_flags` + 热切换 | 🟡 P0 |
| `study/` | 概念、题库、队列、掌握度、周报 | ⬜ L2/L3 |

---

## 5. 数据架构

| 存储 | 内容 | 可重建 |
| --- | --- | --- |
| 源文件 | Markdown / PDF / 网页 | ✅ 唯一事实来源 |
| `documents.sqlite3` | 文档、父文档全文、`docs_meta`、`doc_relations`、`doc_events`、`consumer_cursor`、`answer_log`、`audit_log`、`runtime_flags` | 大部分可 |
| `study.sqlite3` | `concepts`、`concept_edges`、`mentions`、`items`、`attempts`、`learner_state`、`reviews`、`outline_nodes` | 除 `attempts` / `learner_state` 外可 |
| Chroma | 子 chunk 向量 + 元数据（`doc_id`、`scope`、`status`、`valid_until`、`manifest_id`） | ✅ |
| `media/` | 图片等媒体文件 | ✅（从源文件重建） |
| 派生文件 | 概念页、周报、参照系 | ✅ |

```mermaid
erDiagram
    DOCUMENTS ||--|| DOCS_META : "1:1"
    DOCUMENTS ||--o{ DOC_EVENTS : "emits"
    DOCUMENTS ||--o{ MENTIONS : "has"
    CONCEPTS ||--o{ MENTIONS : "appears in"
    CONCEPTS ||--o{ CONCEPT_EDGES : "src"
    CONCEPTS ||--o{ ITEMS : "tests"
    ITEMS ||--o{ ATTEMPTS : "attempted"
    CONCEPTS ||--|| LEARNER_STATE : "mastery"
    DOCUMENTS ||--o{ ANSWER_LOG : "cited by"
```

**不可重建的数据**：`attempts`、`learner_state`（以及未来的复习历史）必须独立备份；其余派生数据可通过事件重放重建。

---

## 6. 写入流程（摄入）

```mermaid
flowchart LR
    SRC["源文件 / 文本 / URL"] --> LOAD["loaders"]
    LOAD --> CLEAN["cleaner"]
    CLEAN --> HASH{"content hash 去重"}
    HASH -->|"新内容"| CHUNK["chunker"]
    HASH -->|"已存在"| SKIP["跳过"]
    CHUNK --> EMB["embedding"]
    CHUNK --> VISION["vision 可选<br/>图片提取 → caption / OCR"]
    VISION --> EMB
    EMB --> CHROMA[("Chroma<br/>子 chunk + 元数据")]
    CLEAN --> PARENT[("SQLite<br/>父文档全文")]
    PARENT --> META[("docs_meta / doc_events")]
    META -.->|"异步"| CONS["扩展 consumer<br/>概念 / 题目 / 报告"]
```

**目标一致性模型**：父文档与 `doc_events` 在同一事务落库；向量索引、概念、题目、报告由 consumer 异步派生。当前 L1 仍为同步写入，P0 接入 outbox 后切换。

---

## 7. 读取流程（检索 / 问答）

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户 / 组件
    participant A as API / CLI
    participant Q as QueryProcessor
    participant R as Retriever
    participant RR as Reranker
    participant D as DocumentStore
    participant L as LLM

    U->>A: query（带 scope）
    A->>Q: process(query)
    Q-->>A: intent + rewritten_query
    A->>R: search(rewritten, top-N, scope)
    R-->>A: candidate hits
    A->>RR: rerank(top-k)
    RR-->>A: ranked hits
    A->>D: expand by doc_id
    D-->>A: parent text / window
    A->>L: prompt(sources + question)
    L-->>A: answer + citations
    A-->>U: Answer(text, sources, meta)
    Note over A: 记录 answer_log（脱敏）<br/>可选：可答性 / 引用校验 / 声明校验
```

**关键约束**：

- 检索必须带 scope 条件，隐藏文档在存储层过滤
- 答案只依据编号来源，每个事实句带引用
- 无相关内容时拒答，不臆造
- `answer_log` 记录检索结果、引用、校验结果，但不落密钥

---

## 8. 学习扩展流程（可选）

```mermaid
flowchart LR
    EVT["doc_events"] --> CON["概念抽取 / 消歧"]
    CON --> C[("concepts / mentions")]
    C --> ITEM["出题 + 出处校验"]
    ITEM --> I[("items")]
    I --> Q["每日队列"]
    Q --> ATT["作答 + 信心值"]
    ATT --> G["判分 / 错因"]
    G --> S[("attempts / learner_state")]
    S --> SCH["SM-2 / 调度"]
    SCH --> Q
    S --> REP["周报 / 概览"]
    C --> REP
    SIG["近期摄入 / 检索 / 标签"] --> FOC["焦点感知"]
    FOC --> Q
    FOC --> REP
```

**边界**：

- 默认关闭；`study.enabled=false` 时整条链路不运行
- 开启不自动回填历史；关闭不删数据
- 焦点感知只影响排序，不限制检索，也不是目标管理
- 题目必须带出处；质量不达标进入隔离区

---

## 9. 治理与事件流

```mermaid
flowchart TB
    D["文档变更<br/>ingest / update / hide / delete"] --> E[("doc_events")]
    E --> V["vector_index consumer"]
    E --> M["mentions consumer"]
    E --> IT["items consumer"]
    E --> RP["reports / cache / queue"]
    V --> CH[("Chroma")]
    M --> ST[("study.sqlite3")]
    IT --> ST
    RP --> OUT["派生文件 / 缓存"]
    E --> AU[("audit_log")]
    CU[("consumer_cursor")] -.-> V
    CU -.-> M
    CU -.-> IT
    CU -.-> RP
```

**级联规则**：

- 隐藏 / 删除 / 更新通过事件传播，不靠各处手写清理
- consumer 幂等，可从 cursor 重放
- 学习历史不可重建，单独备份
- 隐藏内容在检索、父文档、概念、题目、报告、缓存、日志七层生效

---

## 10. 运行时开关

```mermaid
flowchart LR
    ENV["KB_* 启动配置"] -->|"默认值"| FF["FeatureFlags 服务"]
    DB[("runtime_flags")] <-->|"读写 / 缓存"| FF
    FF --> CORE["核心：摄入 / 检索 / 问答"]
    FF --> INTEL["智能层 可选"]
    FF --> LEARN["学习扩展 可选"]
    CLI["CLI"] --> FF
    API["REST"] --> FF
    CHAT["Chat management"] -->|"白名单 + 确认"| FF
```

- `Settings` 只做启动默认值；运行时以 `runtime_flags` 为准
- 热切换无需重启；关闭不删除数据
- 聊天只能操作白名单 flag，写操作二次确认
- 安全 / 隐私 / 密钥类配置不允许从聊天修改

---

## 11. 部署形态

```mermaid
flowchart LR
    subgraph HOST[单机部署 默认]
        CLI["kb CLI"]
        WEB["本地 Web / Chat"]
        API["REST 127.0.0.1"]
        KB["kb-agent 进程"]
        DATA[("~/.kbagent<br/>SQLite / Chroma / media")]
        CLI --> KB
        WEB --> KB
        API --> KB
        KB --> DATA
    end
    RO["外部 AI / 组件"] -->|"只读 Key: search / ask"| API
    AD["管理员"] -->|"admin Key: ingest / delete"| API
    KB --> LLM["LLM 可选云端"]
```

- 默认单机、本地优先，零外部服务
- 作为组件时，只读与管理 Key 分离；`where` / `collection` 白名单
- 不提供通用工具调用，不需要沙箱

---

## 12. 安全边界

| 风险 | 对策 |
| --- | --- |
| 隐藏数据被检索/返回 | scope 在存储层强制过滤；输出裁剪 |
| 密钥泄漏 | 密钥只在环境变量/keyring；不入 prompt、日志、回答；常量时间比较 |
| 外部组件滥用 | 只读/admin Key 分权、限流、请求大小、超时、审计 |
| 上传滥用 | 大小/类型限制、临时文件清理、解析保护 |
| Prompt injection | 参考资料按“数据”分隔；文档内容不得触发管理命令 |
| 管理命令误执行 | 白名单、二次确认、dry-run、审计、软删除 |
| 非本地暴露 | 默认绑 `127.0.0.1`；非 loopback 无 Key 拒绝启动 |

---

## 13. 评估与可观测

| 层 | 指标 | 入口 |
| --- | --- | --- |
| 检索 | Recall@k、Precision@k、MRR、NDCG、p95 延迟 | `kb eval --mode retrieval` |
| 生成 | Accuracy、Faithfulness、引用准确率、拒答/误拒 | `kb eval --mode full` |
| 学习（扩展） | 保持率、校准、队列完成率、题目有效率 | 试点 / 周报 |
| 治理 | 隐藏泄漏、过期率、重复率、top-k 重复占比 | 回归测试 / 周报 |
| 运行 | `answer_log`、`audit_log`、工具/开关变更 | 本地日志 / API |

---

## 14. 当前实现状态

| 能力 | 状态 |
| --- | --- |
| L1 摄入 / 检索 / 问答 / 多模态 / 评估 / CLI / REST / UI | ✅ v0.1.0 |
| 存储抽象（Protocol / Filter / 工厂 / 契约测试） | ✅ 2026-10-29 |
| `docs_meta` / `doc_events` / scope / `answer_log` / 审计 | 🟡 P0 |
| `runtime_flags` / `FeatureFlags` 热切换 | 🟡 P0 |
| 概念 / 焦点 / 覆盖度 | ⬜ L2 |
| 题库 / 队列 / 掌握度 / 周报 | ⬜ L3 |

---

## 15. 关键架构决策

- 存储：Chroma + SQLite，通过 Protocol 抽象，迁移阈值见 [ADR 0001](./adr/0001-storage.md)
- 源文件优先：所有索引与学习数据可重建
- 扩展默认关闭、可热切换、单向依赖
- 不引入通用工具调用；管理操作白名单化
- 单机优先，REST/Python API 作为组件集成面
