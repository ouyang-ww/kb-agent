# 个人知识系统总体计划（v4）

> 版本：**v4**（2026-10-29）
> 状态：**L1 已交付（v0.1.0）；L2/L3 规划中，尚未排期**
> 配套文档：[愿景](docs/vision.md) · [L1→L3 最小闭环拆解](docs/mvp-l1-to-l3.md) · [知识治理](docs/knowledge-governance.md) · [知识图谱与本体](docs/knowledge-graph-and-ontology.md)
> 历史：[v3（L1 交付计划）](docs/archive/PLAN-v3-l1.md)

---

## 0. 版本与状态

| 版本 | 范围 | 状态 |
| --- | --- | --- |
| v3 | L1 检索助手（清洗 / 子母文档 / 重排 / 多模态 / 评估） | ✅ 已交付 v0.1.0 |
| v4 | L1 回归 + L2 知识结构 + L3 学习闭环 + 治理层 | 📝 当前计划 |

v4 不推翻 v3：**L1 的成功标准保留为回归护栏**，任何 L2/L3 改动不得使其退化。

---

## 1. 愿景与范围

### 1.1 最终目标

一个本地优先的个人学习系统：知道自己有什么知识、主人掌握了多少、今天该做什么，并通过每天 10 分钟的闭环让知识真正留下来。

三种用途：

1. **日常知识检索**：CLI + Web 聊天，答案带出处引用
2. **组件化集成**：Python API + REST，供其他 AI agent 项目使用
3. **个人学习系统**（v4 新增）：知识结构 + 学习者模型 + 每日队列

### 1.2 层级

| 层级 | 形态 | 关键能力 | 状态 |
| --- | --- | --- | --- |
| **L1** | 检索助手 | 向量检索、重排、引用、多模态 | ✅ 已交付 |
| **L2** | 知识管理员 | 概念/关系抽取、去重、缺口分析、概念页 | 规划中 |
| **L3** | 学习教练 | 学习者模型、题库、间隔复习、每日队列、周报 | 规划中 |
| **L4** | 知识伙伴 | 目标驱动、主动建议、跨笔记综述、教-回 | 非本计划 |

### 1.3 非目标

- 通用笔记编辑器（源文件保持 Markdown，人类可编辑）
- 完整 OWL 本体、图数据库（先用 SQLite 关系表）
- 自动改写 / 合并源笔记
- 移动端、多用户、微软 GraphRAG 式社区摘要
- L4 能力（目标管理、主动 agent、teach-back）

---

## 2. 分层成功标准

### 2.1 L1 回归护栏（必须保持）

- `pip install -e .` 成功；`kb ingest/search/ask/list/delete/stats/serve/chat/eval` 可用
- 写入链路：loader → cleaner → chunker（子 chunk 挂 `doc_id`）→ 嵌入 → Chroma（子）+ SQLite（父全文），哈希去重
- 读取链路：意图识别 → query 改写 → top-N → 重排 top-k → 父文档扩写 → 编号引用 → LLM
- `get_document(doc_id)` 回查原文档；检索结果带 `doc_id` 溯源；delete 级联
- 多模态：图片提取 → 描述 → 可选 OCR → 图文单元检索 → vision 生成；无 vision 模型自动降级
- `kb eval` 输出 recall@k / accuracy / faithfulness 等指标与对比报告；红线 Recall@5 ≥ 0.8 / Accuracy ≥ 0.75 / Faithfulness ≥ 0.9
- REST 全端点可用；Gradio 显示来源；`pytest` 全绿，结果追加到 [`test_history.log`](test_history.log)
- **`KB_STUDY_ENABLED=false` 时，行为与性能与 v0.1.0 一致**

### 2.2 L2 成功标准

- 入库自动产出概念与关系，带原文出处
- 概念消歧后重复率 < 10%；抽检"有意义"比例 ≥ 90%
- 概念页自动聚合定义、出处、相关笔记、掌握度
- 能对照参照系输出覆盖率与缺口
- **源文件零修改**（哈希校验）

### 2.3 L3 成功标准（14 天试点）

- 每日队列完成率 ≥ 80%
- 单次时长中位数 ≤ 12 分钟
- 7 天保持率 ≥ 85%
- 校准误差 ≤ 0.2
- 首批 50 题人工审核有效率 ≥ 90%，且 100% 带出处
- 周报每周至少被阅读一次

### 2.4 治理护栏（贯穿 L2/L3）

- 隐藏内容泄漏次数 = 0
- `refusal_recall` ≥ 0.9，`over_refusal` ≤ 0.1
- `citation_accuracy` ≥ 0.95
- `attempts` / `learner_state` 有独立备份
- 派生数据可从源文件重建

---

## 3. 架构总览

### 3.1 分层

```
源层       Markdown / PDF / 网页（唯一事实来源，人类可编辑）
加工层     清洗 → 切块 → 概念抽取 → 链接/消歧 → 出题 → 排程
知识模型   概念图 + 依赖 + 出处 + 参照系覆盖率
学习者模型 掌握度 + 复习计划 + 作答记录 + 校准
策略层     今日队列 / 推荐 / 出题 / 讲解 / 周报
界面层     CLI / REST / Gradio / 聊天 Agent
评估层     检索 / 生成 / 学习 / 治理
```

四条原则：源层不可自动改写；加工层幂等可重建；模型层可解释；策略层可关闭。

### 3.2 四条数据流

**写入**

```
ingest → loader → cleaner → chunker → 嵌入 → Chroma（子 chunk）
                                      → SQLite（父文档全文）
       → docs_meta / doc_events（同一事务）
       → [consumer] 概念 / mentions / 题目 / 概念页 / 报告
```

**读取**

```
ask → 意图识别 → 改写 → top-N → 重排 → 可答性闸门
    → 父文档扩写 → 编号引用 → LLM → 引用契约 / 声明校验
    → Answer(text, sources, meta{intent, rewritten_query, verification}) + answer_log
```

**学习**

```
概念 → 题目 → 每日队列 → 作答（+ 信心值）→ 判分
     → mastery 更新 → 下次复习时间 → 周报
```

**治理**

```
doc_events → consumers（向量库 / mentions / items / reports / cache / queue）
           → 审计日志 / 可重放的修复
```

---

## 4. 存储与数据模型

| 存储 | 内容 | 可重建 |
| --- | --- | --- |
| `documents.sqlite3` | 文档、父文档全文、`docs_meta`、`doc_relations`、`doc_events`、`consumer_cursor`、`answer_log`、`audit_log` | 大部分可 |
| `study.sqlite3` | `concepts`、`concept_edges`、`mentions`、`items`、`attempts`、`learner_state`、`reviews`、`outline_nodes` | 除 `attempts` / `learner_state` 外可 |
| Chroma | 子 chunk 向量 + 元数据（`doc_id`、`scope`、`status`、`valid_until`、`manifest_id`） | ✅ |
| 派生文件 | 概念页、周报、参照系 | ✅ |

规则：

- `docs_meta` / `doc_events` / `answer_log` 放 documents 库；study 库通过事件消费，**跨库不做事务**
- `attempts` / `learner_state` 不可重建，必须独立备份
- 详细 schema 见 [知识治理](docs/knowledge-governance.md) 第 6 节与 [闭环拆解](docs/mvp-l1-to-l3.md) 第 4 节

---

## 5. 治理

| 问题 | 机制 | P0 必做 |
| --- | --- | --- |
| **防幻觉** | 可答性闸门 + 引用契约 + 声明校验 + 拒答指标 | 元数据 / 日志 |
| **隐藏与抽离** | `scope` + 级联清理 + 强制过滤 + 审计 | ✅ |
| **embedding 迁移** | `embedding_manifest` + 运行时指针 + 影子评估 | ✅ 建表 |
| **知识过期** | 时效元数据 + 冲突队列 + 题库联动 | 元数据 |
| **知识重复** | 分层判重 + 检索时折叠 + 人工合并 | 哈希 / 近似预留 |

详见 [知识治理](docs/knowledge-governance.md)。

---

## 6. 分阶段实施

### 6.0 第 5 天 Go/No-Go（纵向切片）

先用硬编码的 3 道题跑通 `today → answer → 下次到期 → 一行周报`，自己真实使用 3 天。  
**不达标就回到交互设计，不进入 P1。**

### 6.1 阶段表

| 阶段 | 内容 | 人日 | 验收 |
| --- | --- | --- | --- |
| **P0** | 基础设施 + 治理骨架 + 存储抽象 | 5–8 | 表就绪、开关零影响、隐藏测试 6 条、事件可重放、契约测试通过 |
| **P1** | 知识结构（概念 / 出处 / 消歧 / 概念页 / 覆盖度） | 8–12 | 概念准确率、重复率、源文件零修改 |
| **P2** | 题库与判分（生成 / 校验 / 质量闸门） | 10–15 | 50 题有效率 ≥ 90%、100% 带出处 |
| **P3** | 学习者模型 + 调度 + 每日队列 | 8–12 | 7 天自测、队列 < 15 分钟、可解释 |
| **P4** | 周报 + 最小地图 | 5–8 | 周报数字一致、能看出下周学什么 |
| **P5** | 评估与打磨 | 5–8 | 检索无退化、14 天试点达标、测试记录归档 |

总计约 **7–10 周**（业余）或 **3.5–4.5 周**（全职）。详细拆解见 [mvp-l1-to-l3.md](docs/mvp-l1-to-l3.md)。

### 6.2 P0 交付清单

学习侧：

- `study.sqlite3` + 版本化 migration + `KB_STUDY_ENABLED`
- `content_hash` + `prompt_version` 幂等
- LLM 调用封装（重试 / 限流 / JSON 模式 / schema 校验）

治理侧：

- `docs_meta`、`doc_events`、`consumer_cursor`
- 检索入口**强制传 scope**
- `embedding_manifest` + `runtime_pointer`
- `answer_log`
- 隐藏文档自动化测试（`search` / `ask` / `study` 三处均无结果）
- 存储抽象：`DocumentStore` Protocol、工厂、后端中立过滤模型、契约测试（✅ 2026-10-29，见 [ADR 0001](docs/adr/0001-storage.md)）

---

## 7. 评估体系

| 层 | 指标 | 频率 |
| --- | --- | --- |
| **检索** | Recall@k、Precision@k、Hit Rate、MRR、NDCG、p95 延迟 | 每次改动 |
| **生成** | Accuracy、Faithfulness、引用准确率、拒答率 / 误拒率 | 每次改动 |
| **学习** | 保持率、校准误差、队列完成率、题目有效率 | 试点 / 每两周 |
| **治理** | 隐藏泄漏、过期率、重复率、top-k 重复占比 | 每次改动 / 每周 |

规则：

- 现有 `kb eval` 的复合分与红线保持不变，**不掺入学习指标**
- 运行时判分（简答）必须以出处 + rubric 为准，不能拿 LLM 自由答案当标准
- 离线 judge 与运行时 grader 分开配置、分开版本化

---

## 8. 技术决策与配置

### 8.1 技术决策

| 项 | 决策 |
| --- | --- |
| LLM | OpenAI 兼容，默认 deepseek-chat；judge / 视觉 / 判分模型独立可配 |
| 嵌入 | 本地 `bge-small-zh-v1.5`；通过 manifest 版本化，可迁移 |
| 重排 | `bge-reranker-base`，失败降级相似度排序 |
| 意图 / 改写 | 单次 JSON 调用 + 规则关键词兜底 |
| 存储 | Chroma + `documents.sqlite3` + `study.sqlite3`；通过 Protocol 抽象，见 [ADR 0001](docs/adr/0001-storage.md) |
| 本体 | YAML 本体-lite，不上 OWL |
| 调度 | SM-2 简化版；可选 FSRS |
| 掌握度 | Elo 简化版；后续可升级 BKT / IRT |
| 出题 | LLM 生成 + 出处校验 + 人工抽检 |
| 界面 | CLI + Gradio + FastAPI |
| 治理 | 元数据 + 事件 outbox + 版本指针 |

### 8.2 配置

L1 配置保持不变（`KB_EMBEDDING_MODEL` / `KB_CHUNK_SIZE` / `KB_RERANK_*` / `KB_TOP_K` / `KB_INTENT_ENABLED` / `KB_QUERY_REWRITE_ENABLED` / `KB_MAX_PARENT_CHARS` / `KB_LLM_*` / `KB_MULTIMODAL_ENABLED` / `KB_VISION_LLM_MODEL` / `KB_OCR_ENABLED` / `KB_EVAL_JUDGE_MODEL` / `KB_STORAGE_DIR` / `KB_API_PORT` / `KB_API_KEY`）。

新增：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `KB_STUDY_ENABLED` | false | 学习层总开关 |
| `KB_STUDY_DAILY_MINUTES` | 10 | 每日时间预算 |
| `KB_STUDY_NEW_PER_DAY` | 1 | 每天新概念上限 |
| `KB_STUDY_ITEMS_PER_DOC` | 4 | 每篇出题上限 |
| `KB_STUDY_ONTOLOGY_PATH` | `~/.kbagent/ontology.yaml` | 本体-lite |
| `KB_STUDY_PROMPT_VERSION` | `r1` | 幂等与重生成 |
| `KB_STUDY_GRADER_MODEL` | 同 `KB_LLM_MODEL` | 判分模型 |
| `KB_ANSWER_STRICT` | false | 声明校验（严格模式） |

### 8.3 存储选型与迁移阈值

**决策**：L1–L3 保持 Chroma + SQLite，不引入 Milvus / MySQL。完整理由与接口审计见 [ADR 0001](docs/adr/0001-storage.md)。

迁移触发条件（命中 2–3 条且优化无效才评估）：

| 条件 | 阈值 |
| --- | --- |
| 向量规模 | > 100 万–200 万 |
| 文档 / 块规模 | 文档 > 5 万 或 chunk > 50 万 |
| 并发写入 | > 1–3 个并发写者 / 多用户 |
| 查询延迟 | 调优后 p95 > 300–500ms |
| 部署形态 | 多实例、远程访问、多设备同步 |
| 权限 | 需要 RBAC / 行级权限 |

迁移目标：Qdrant / LanceDB（向量）、PostgreSQL / pgvector（关系 + 向量合一）；Milvus 仅在 1000 万+ / GPU / 分片场景。

**接口要求**：`VectorStore` / `DocumentStore` / `StudyStore` 三个 Protocol + 工厂 + 契约测试；`where` 过滤改为后端中立模型；chunk 元数据携带 `embedding_manifest_id` / `embedding_model` / `chunker_version`。

---

## 9. 边界、降级与隐私

保留 v3 边界：

- 清洗不破坏代码块 / 表格
- 意图 / 改写 / 重排 / 视觉任一不可用降级不阻断
- 坏图 / 加密 PDF 警告跳过；空库明说不臆造
- API 默认绑 `127.0.0.1`、不接受服务器路径、可选 `X-API-Key`

新增：

- **隐藏内容**：`scope` 在检索、父文档、概念、题目、报告、缓存、日志七层生效；级联由 `doc_events` 驱动
- **过期内容**：默认过滤 expired / superseded；历史查询显式允许并标注
- **冲突内容**：并列呈现，不静默合并
- **严格模式**：声明校验失败降级为拒答
- **隐私**：抽取 / 判分默认走云端 LLM，需在 README 明示数据出境范围；敏感目录可排除或改用本地模型
- **成本**：每篇文档的 LLM 调用次数设上限，批量任务限流
- **备份**：`attempts` / `learner_state` 独立备份；其他派生数据可重建

---

## 10. 风险与开放问题

| 风险 | 应对 |
| --- | --- |
| 出题质量差 | 出处校验 + 50 题人工审 + 标记率熔断（> 10% 停自动出题） |
| 概念粒度失控 | "可出题的最小单元" + 每篇上限 + 合并建议 |
| 主人不用 | 5–10 分钟上限、零手工、周报正反馈、第 5 天 Go/No-Go |
| 掌握度不可信 | 展示作答依据、允许重置、先简单后复杂 |
| 隐私 | 明确出境范围、本地模型选项、排除机制 |
| 维护成本 | 全派生、幂等、增量、可重建 |

开放决策：

1. 每天时间预算（5 / 10 / 15 分钟）
2. 首批领域（仅 RAG，还是同时多领域）
3. 云端 LLM 可处理哪些目录
4. 是否允许高置信度自动 `supersede`
5. 严格模式是否默认开启

---

## 11. 测试历史与回归记录

**唯一记录文件**：[`test_history.log`](test_history.log)（仓库根目录，纳入 git）。

规则：

- 每次运行测试（`pytest` 或 `kb eval`）后，**追加**一条新记录，不覆盖、不删除历史
- 记录内容：时间戳、命令、环境变化（依赖 / 模型）、结果（passed / failed / skipped）、耗时、失败摘要、修复说明
- 失败与修复必须写清「现象 → 原因 → 方案」，便于回溯
- 不写入 API Key 等敏感信息；`.env` 永不入库
- 与评估报告分工：`test_history.log` 记录测试与回归；`eval/reports/` 记录检索 / 生成指标

当前文件已包含 v0.1.0 的测试环境、71 个测试结果、9 轮迭代、问题与解决方案、Git 变更记录。

---

## 12. 历史

v3（L1 交付计划）原文归档于 [docs/archive/PLAN-v3-l1.md](docs/archive/PLAN-v3-l1.md)。

L1 已按 v3 步骤 1–11 交付 v0.1.0，71 个测试通过。
