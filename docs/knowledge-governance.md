# 知识治理：可信、可控、可演进

> 状态：**设计文档 + P0 骨架已实现（2026-10-30）**。与 [PLAN v4](../PLAN.md) 配套。
> 本文的 P0 / P1 / P2 与 [status §5](./status.md) 的治理阶段同名同义。学习阶段叫 L2、S1–S4，不占用这套编号。
> 配套：[愿景](./vision.md)、[L1→L3 最小可行闭环拆解](./mvp-l1-to-l3.md)、[知识图谱与本体学习笔记](./knowledge-graph-and-ontology.md)。
> 覆盖五个问题：**防幻觉、隐藏与抽离、embedding 迁移、知识过期、知识重复。**
> 实施状态：`docs_meta` / `doc_events` / `consumer_cursor` / scope 过滤 / hide-unhide / `answer_log` / `audit_log` / `embedding_manifest` / `runtime_pointer` / 只读-admin Key 分权已实现（2026-10-30）；过期/冲突检测、严格模式、去重待实现。

---

## 0. 总览

这五个问题分属三条轴，共用同一套基础设施：

| 能力 | 目标 | 主要机制 | 落地阶段 |
| --- | --- | --- | --- |
| **可信** | 每个事实性陈述可追溯，无据则明说 | 可答性闸门 / 引用契约 / 声明校验 / 拒答指标 | P1 闸门与引用；P2 声明校验 |
| **可控** | 不该出现的内容不出现 | scope 元数据 / 查询过滤 / **级联清理** / 硬抽离 / 审计 | P0 建表，P1 实现 |
| **可演进** | 模型、事实、内容变化可管理 | embedding manifest / 时效元数据 / 分层去重 | P0 建表，P2 实现 |

**共用的三件基础设施**（P0 就要有，后期补代价很大）：

1. **`docs_meta`**：每篇文档的治理元数据（scope、时效、权威度、哈希）
2. **`doc_events`**：变更事件 outbox，保证向量库、概念、题目、报告**级联不漏**
3. **`embedding_manifest` + 运行时指针**：索引版本化，支持并行构建、影子评估、原子切换、回滚

> 一句话：**治理不是五个功能，而是一层"文档元数据 + 变更传播 + 版本指针"的基础设施。**

---

## 1. 可信：怎么保证不乱回答

### 1.1 先明确边界

**无法保证"零幻觉"**。可承诺的是：

- 每个事实性陈述都能指到一个来源；
- 指不到来源时，明确说"资料不足"，而不是自由发挥；
- 来源冲突或过期时，明确标注；
- 错误可以被发现（引用可展开、日志可审计）。

### 1.2 失败模式与防线

| 失败模式 | 现有防线 | 需要补的 |
| --- | --- | --- |
| 检索到不相关块 | 重排 | 相关度阈值 + 可答性判定 |
| 模型用参数记忆作答 | prompt | 强制引用 + 无据不答 |
| 引用编号编造 | 编号引用 | 后置校验：编号存在、事实句有引用 |
| 部分覆盖答成全部 | — | 覆盖度检查（关键实体是否都在 context） |
| 多来源冲突 | — | 冲突检测 + 并列呈现 |
| 问题超出知识库 | 意图识别 | `out_of_scope` 直接拒答并说明 |

### 1.3 四道闸门

```
检索 → 重排 → 闸1 可答性 → 生成 → 闸2 引用契约 → 闸3 声明校验 → 闸4 展示
```

#### 闸 1：可答性（检索后、生成前）

输入 query + 重排后的 top-N。判定：

```text
top1_score < τ_low                  → 直接拒答
τ_low ≤ top1_score < τ_high         → 可答，但降低置信度并提示"依据较弱"
top1_score ≥ τ_high                 → 正常回答
```

可选叠加一个轻量 LLM 分类：`{"answerable": bool, "missing": ["缺少的要点"]}`。

**τ 必须按 embedding + rerank 模型组合校准**，换模型后重标定（见第 3 节）。

#### 闸 2：生成契约

Prompt 约束：

1. 只能使用给定编号来源，不得引入外部知识
2. 每个事实句必须带 `[n]`
3. 资料不足时使用固定拒答结构，而不是自由发挥
4. 末尾输出机器可读的自检

输出契约：

```json
{
  "answer": "... [1] ... [2] ...",
  "used_sources": [1, 2],
  "confidence": "high|medium|low",
  "gaps": ["未找到关于 X 的资料"],
  "unsupported": ["无法从来源推出的陈述"]
}
```

#### 闸 3：声明校验（可选，严格模式）

1. 把答案拆成原子陈述
2. 逐条判断"仅凭所引片段能否推出"
3. 推不出 → 删除，并加入 `gaps`
4. 校验失败率过高 → 整题降级为拒答

```json
{"claim": "RAG 一定需要重排", "source_ids": [2], "verdict": "not_entailed"}
```

成本高，建议：

- 只在 `KB_ANSWER_STRICT=true` 时启用
- 或按问题类型抽样（高影响、含数字/结论的问题）
- 或先小模型初筛，可疑项再复核

#### 闸 4：展示

- 来源片段可展开、可跳原文
- 置信度标签（由检索分 + 校验结果共同决定）
- 拒答时给出"最接近的资料 + 缺什么"

### 1.4 拒答也是答案

固定结构示例：

> 知识库中没有直接依据回答这个问题。最接近的是 [1]《xxx》，但它只覆盖了 A，缺少 B。你可以补充相关笔记，或换个更具体的问题。

产品上把拒答当作**正常输出**来设计，而不是错误。

### 1.5 评测集怎么建

| 集合 | 构造方式 | 规模 | 指标 |
| --- | --- | --- | --- |
| 可回答集 | 现有 `baseline.jsonl` | 已有 | `recall@k`、`accuracy`、`faithfulness` |
| 库外集 | 编 20–50 个知识库绝不覆盖的问题 | 20–50 | `refusal_recall`（应拒答率） |
| 近邻陷阱集 | 相关问题但答案只覆盖一部分 | 10–20 | 部分覆盖识别率 |
| 冲突集 | 人为制造两条互相矛盾的笔记 | 5–10 | 冲突呈现率 |
| 过期集 | 旧版本与新版本文档并存 | 5–10 | 过期标注率 |

**每次改动答案链路都跑这五套**，不能只看原有的检索指标。

### 1.6 答案日志

每次回答落一条，用于审计、回归和人工抽检：

```sql
CREATE TABLE answer_log (
  id               TEXT PRIMARY KEY,
  ts               TEXT NOT NULL,
  query            TEXT NOT NULL,
  rewritten_query  TEXT,
  retrieved_json   TEXT,   -- [{doc_id, chunk_id, score, rerank_score}]
  answer           TEXT,
  citations_json   TEXT,
  verification_json TEXT,
  confidence       TEXT,
  refusal_reason   TEXT,
  latency_ms       INTEGER,
  feedback         INTEGER -- 用户赞/踩，可空
);
```

### 1.7 学习系统的额外红线

- **判分不能拿 LLM 自己的答案当标准**：必须以出处片段 + 评分点 rubric 为准
- 出处本身模糊/矛盾的题 → 直接 `quarantined`，不进入题库
- 讲解和提示也必须引用来源；"扩展知识"要显式标记为外部知识

---

## 2. 可控：隐藏与抽离

### 2.1 先分清三个轴

| 轴 | 需求 | 机制 | 只做过滤够吗 |
| --- | --- | --- | --- |
| **展示隐藏** | 搜索/回答里不出现 | 查询时 scope 过滤 | 够 |
| **参与隔离** | 不被检索、引用、学习系统使用 | 索引级硬抽离 + 级联清理 | 不够 |
| **隐私隔离** | 不发给云端 LLM | 本地模型 / 不索引 / 加密保险库 | 完全不够 |

**只做查询过滤，不能解决"已经发给云端""派生数据里还有""日志里还有"这三个问题。**

### 2.2 元数据

```yaml
# Markdown frontmatter（源文件侧，人类可读）
scope: private          # default | work | private | secret
tags: [work, project-x]
exclude_from_learning: true
```

```sql
-- 主库侧（权威）
ALTER TABLE docs_meta ADD COLUMN scope TEXT NOT NULL DEFAULT 'default';
ALTER TABLE docs_meta ADD COLUMN exclude_from_learning INTEGER NOT NULL DEFAULT 0;
```

规则：

- 默认 `default`，敏感内容必须显式标 `private/secret`
- `scope` 决定是否可检索；`exclude_from_learning` 单独控制是否进入概念图/题库/推荐
- 子 chunk 的元数据**必须**从父文档复制，否则过滤会漏

### 2.3 过滤层清单

隐藏一篇文档，以下**每一层**都要生效：

| 层 | 做什么 | 漏了会怎样 |
| --- | --- | --- |
| 向量库（Chroma） | `where` 过滤 scope/status | 被检索出来 |
| 父文档库（SQLite） | 查询加 scope 条件 | 父文档扩写时泄漏 |
| 概念与 mentions（study 库） | 移除隐藏文档的 mention；概念无可见来源则隐藏 | 概念页/地图泄漏 |
| 题库（items） | 全部来源被隐藏的题 → `quarantined` | 每日队列出现 |
| 队列/推荐 | 只从可见概念取题 | 间接泄漏 |
| 报告/覆盖度 | 排除隐藏文档重新计算 | 周报泄漏 |
| 缓存 | 失效相关缓存 | 旧答案继续返回 |
| 日志/导出 | 不记录原文；导出排除 | 备份泄漏 |

Chroma 过滤示例：

```python
where = {"$and": [
    {"scope": {"$ne": "private"}},
    {"scope": {"$ne": "secret"}},
    {"status": {"$eq": "active"}},
]}
collection.query(query_embeddings=[vec], n_results=k, where=where)
```

概念可见性规则：

```text
concept.visible = 存在至少一个 visible 文档的 mention
概念页展示时，只展示 visible 文档的 snippets
仅由隐藏文档支撑的 concept → hidden（不删数据，标记状态）
```

### 2.4 级联清理 checklist

`hide` / `delete` / `archive` 时逐项确认（由 `doc_events` 驱动，见第 6 节）：

- [ ] 向量库：删除或标记 chunk（软/硬模式不同）
- [ ] 父文档：保留但标记，或加密归档
- [ ] `mentions`：移除该文档的记录
- [ ] `items`：来源全部隐藏 → 隔离；混合来源 → 重新锚定到可见来源，否则隔离
- [ ] 概念页：重算来源与摘要
- [ ] 地图/覆盖度/周报：重算
- [ ] 学习队列与推荐：重算
- [ ] 缓存：失效
- [ ] 审计日志：记录操作（不含原文）
- [ ] **云端 LLM**：已发送的内容无法撤回 → 敏感内容必须发送前就排除

### 2.5 三种处理方式的取舍

| 方式 | 做法 | 适用 | 代价 |
| --- | --- | --- | --- |
| **软隐藏** | 保留索引，查询时过滤 | 临时不相关、需要随时恢复 | 过滤 bug 风险；日志/缓存风险 |
| **硬抽离** | 删除 chunk/向量；父文档加密归档 | 敏感但偶尔需要恢复 | 恢复需重建 |
| **不索引** | 只进加密保险库，永不参与 RAG | 真机密 | 助手完全无法使用 |

高敏感内容建议**直接不索引**：embedding 本身携带语义信息，过滤不是可靠的安全边界。

### 2.6 命令与审计

```bash
kb hide <doc_id> --mode soft|hard --reason "离职资料"
kb unhide <doc_id>                 # 触发重建
kb vault add <path>                # 加密存储，不索引
kb vault list
kb audit --since 2026-10-01
```

- 显式访问隐藏内容需要二次确认，并记录审计事件
- `unhide` 是异步操作：重新抽取、出题、加入队列需要时间，命令应立即返回任务 ID

### 2.7 测试用例（必须自动化）

1. 用隐藏文档的**精确标题**做 `search` / `ask` → 无结果 / 拒答
2. `study today` 不出现隐藏来源的题
3. 概念页不出现隐藏文档的 snippets
4. 周报/覆盖度数字排除隐藏文档
5. `kb unhide` 后（等待重建）恢复检索
6. 直接调用 `retriever.search()` **不带过滤器**时，测试应该失败或强制要求传 scope 参数（防止未来某条代码路径绕过）

第 6 条最重要：**让"忘记过滤"变成编译期/测试期错误**，而不是运行时漏洞。

---

## 3. 可演进 A：更换 embedding model

### 3.1 核心事实

- 不同模型的向量**不可比较、不可混用**，必须全量重建索引
- 查询端和索引端必须使用**同一个模型版本**
- 可以做到不停机、可评估、可回滚
- 你的重建路径：**重读源文件**（[ADR 0002](./adr/0002-no-original-duplication.md)）。v0.1.0 曾把父全文放在 SQLite，迁移完成后不再依赖这份副本。源文件不在时，该文档的向量索引无法重建。

### 3.2 版本化清单

```sql
CREATE TABLE embedding_manifest (
  id              TEXT PRIMARY KEY,      -- 如 bge-m3@v1/c1
  model           TEXT NOT NULL,
  revision        TEXT,
  dim             INTEGER NOT NULL,
  normalize       INTEGER NOT NULL DEFAULT 1,
  chunker_version TEXT NOT NULL,
  collection      TEXT NOT NULL,
  status          TEXT NOT NULL,         -- building|active|retired|failed
  doc_count       INTEGER DEFAULT 0,
  created_at      TEXT NOT NULL,
  activated_at    TEXT
);

CREATE TABLE runtime_pointer (
  name  TEXT PRIMARY KEY,               -- active_embedding
  value TEXT NOT NULL                   -- -> embedding_manifest.id
);
```

### 3.3 迁移步骤

1. **登记新 manifest**，新 collection 命名带模型+维度+切块版本（如 `chunks_bge_m3_1024_c1`）
2. **后台重建**：从 SQLite 父文档重新切块/嵌入；批量、限流、可断点续跑
3. **双写**：迁移窗口内新入库同时写新旧两边（或进补写队列）
4. **影子评估**：100–200 条 golden query，比较 `recall@k`、`ndcg`、p95 延迟、存储占用
5. **重校准阈值**：相关度 τ、去重阈值、MMR 参数全部依赖分数分布，必须重标定
6. **原子切换**：更新 `runtime_pointer`；每个查询请求开始时读取一次，避免半途换模型
7. **回滚窗口**：旧 collection 保留 7 天，指针可切回
8. **同步派生向量**：概念消歧、题目去重用的 embedding 也要重建（独立 namespace）
9. **清理**：确认稳定后删除旧 collection

### 3.4 为什么必须先影子评估

新模型不一定更好：

- 可能在某类语料上更差
- 维度更高 → 存储和延迟上升
- 分数分布变化 → 原有 τ 失效，拒答率/召回率同时漂移

先花 1–2 小时在 100 条真实查询上比，再决定是否值得全量重嵌入。

### 3.5 派生向量也要版本化

| 用途 | 数据 | 版本方式 |
| --- | --- | --- |
| 文档检索 | Chroma chunks | `embedding_manifest` |
| 概念消歧 | 概念名/定义向量 | `concept_embedding_manifest` |
| 题目去重 | 题干向量 | `item_embedding_manifest` |

三者可以共用同一模型，但必须各自记录版本；换模型时一起重建。

---

## 4. 可演进 B：知识过期

### 4.1 四种"过期"

| 类型 | 例子 | 判据 | 处理 |
| --- | --- | --- | --- |
| **事实被取代** | "Python 3.9 是当前版本" | 新文档声明 `supersedes` | 检索沿链指向最新 |
| **内容失效** | 旧 API 用法、失效链接 | `valid_until` / `review_by` | 默认不检索或降权 |
| **个人相关性衰减** | 前公司流程 | 手动归档 | `archived`，不删 |
| **无法判断** | 一般笔记 | 周期 `review_by` | 进复核队列 |

### 4.2 元数据

```sql
ALTER TABLE docs_meta ADD COLUMN valid_from   TEXT;
ALTER TABLE docs_meta ADD COLUMN valid_until  TEXT;  -- 到该日期后视为过期
ALTER TABLE docs_meta ADD COLUMN review_by    TEXT;  -- 到期提醒复核，不自动过期
ALTER TABLE docs_meta ADD COLUMN last_verified TEXT;
ALTER TABLE docs_meta ADD COLUMN authority    INTEGER DEFAULT 3;  -- 1..5
ALTER TABLE docs_meta ADD COLUMN status       TEXT DEFAULT 'active';
-- status: active | superseded | archived | hidden | deleted
```

关系用独立表（多对多、可人工确认）：

```sql
CREATE TABLE doc_relations (
  src_id     TEXT NOT NULL,
  dst_id     TEXT NOT NULL,
  type       TEXT NOT NULL,          -- supersedes | merged_into | contradicts | related
  confidence REAL DEFAULT 1.0,
  created_by TEXT,                   -- llm | user
  status     TEXT DEFAULT 'pending', -- pending | confirmed | rejected
  created_at TEXT NOT NULL,
  PRIMARY KEY (src_id, dst_id, type)
);
```

### 4.3 入库检测

1. LLM 抽取**时效性声明**（"目前""截至 2024""即将"）
2. 新文档入库 → 找 embedding 邻居（同概念/高相似）→ LLM 判断关系
3. 结果进 `doc_relations`，`status='pending'`
4. 人工确认后才生效（周报里批量确认，一次 2 分钟）

### 4.4 "更新"与"冲突"不是一回事

| 新文档 vs 旧文档 | 判定 | 动作 |
| --- | --- | --- |
| 同一事实，新文档带更晚日期 | 更新 | `supersedes`（需确认） |
| 观点不同、无时间先后 | 冲突 | `contradicts`，答案并列呈现 |
| 补充细节 | 相关 | `related` |
| 无法判断 | 待定 | 保留 `pending`，不自动处理 |

**不要静默合并冲突**：让用户看到两种说法及各自出处，本身就是知识管理价值。

### 4.5 检索与生成策略

```sql
-- 默认检索
WHERE status = 'active'
  AND (valid_until IS NULL OR valid_until > :now)
```

- **历史查询**（"2023 年怎么做"）：允许过期来源，但标注时间
- **答案标注**：使用过期来源时加"注：[1] 最后更新于 2024-03，可能已过时"
- **排序**：同等相关时，优先 `authority` 高、`last_verified` 新
- **冲突**：两条都返回，明确写"存在两种说法"

### 4.6 与学习系统联动

- 题目的来源文档变为过期/被取代 → 题目 `quarantined`，重新生成
- 概念标记"需重新学习"，但**不清零 mastery**（你学过旧版本，不是没学过）
- 周报固定列出：即将过期、已过期、待确认冲突、逾期未复核
- 覆盖度计算只用 `active` 文档

### 4.7 指标

`expired_rate`、`stale_answer_rate`、`conflict_open_count`、`review_overdue_count`。

---

## 5. 可演进 C：知识重复

### 5.1 四个层次

| 层次 | 判据 | 处理 |
| --- | --- | --- |
| **完全相同** | 规范化后内容哈希相同 | 入库直接跳过（已有） |
| **近似重复** | SimHash/MinHash；chunk 向量余弦 > 0.95 | 标 `duplicate_of`，不重复索引 |
| **包含关系** | 一篇的段落被另一篇完整包含 | 合并建议 |
| **事实重复** | 多个来源陈述同一事实 | 概念页聚合，**不合并源文档** |

### 5.2 检测方法

```text
① 精确：NFKC + 空白折叠 → sha256
② 近似：SimHash 64 位，汉明距离 ≤ 3；或 MinHash Jaccard ≥ 0.9
③ 语义：chunk 向量余弦 ≥ 0.95（阈值随模型重标定）
④ 事实：同概念的高相似文档对 → LLM 判断"是否陈述同一事实"
```

分块级和文档级都做：

- 文档级重复 → 提示用户，不重复入库
- 分块级重复 → 不重复写向量，保留 `duplicate_of` 映射（节省空间、避免 top-k 被占满）

### 5.3 检索时去重（容易被忽略）

即使入库去重了，同一篇的不同块、或近义块仍会占满 top-k：

```text
检索 top-N → 按父文档/内容簇折叠（保留最高分）
          → MMR 增加多样性（λ ≈ 0.7）
          → 再重排 top-k
```

### 5.4 合并流程

1. LLM 提议合并（基于相似度 + 事实重叠）
2. **人工确认**（自动只允许"完全相同"）
3. 建立 `merged_into` 指针，保留全部 provenance
4. 级联：mentions 合并、题目退役/重生成、概念页重算
5. **不自动删除源文件**

### 5.5 冗余 vs 复述

重复不总是坏事。学习场景里，同一概念的不同例子、不同角度是有价值的：

| | 冗余 | 复述 |
| --- | --- | --- |
| 信息增量 | 无 | 有（新例子/新角度/更清晰） |
| 事实集合 | 相同 | 相同或更多 |
| 处理 | 去重/合并 | **保留**，概念页并列展示 |
| 判据 | 余弦高 + 事实集合相同 + 无新例证 | 事实相同但例证/表达不同 |

**只合并冗余，保留复述。** 激进合并会毁掉学习价值。

### 5.6 题库去重

- 题干向量 + 答案相似度判重
- 同一概念的**变体题**允许（防止背题）
- 完全相同的题不允许
- 一个概念同一时间 active 题上限 3 道

### 5.7 指标

`duplicate_rate`、`topk_dup_ratio`、`merge_backlog`、`redundant_chunk_ratio`。

---

## 6. 共用基础设施

### 6.1 `docs_meta`

```sql
CREATE TABLE docs_meta (
  doc_id             TEXT PRIMARY KEY,
  content_hash       TEXT NOT NULL,             -- 规范化后全文哈希
  scope              TEXT NOT NULL DEFAULT 'default',
  status             TEXT NOT NULL DEFAULT 'active',
  authority          INTEGER NOT NULL DEFAULT 3,
  valid_from         TEXT,
  valid_until        TEXT,
  review_by          TEXT,
  last_verified      TEXT,
  exclude_from_learning INTEGER NOT NULL DEFAULT 0,
  updated_at         TEXT NOT NULL
);
```

### 6.2 `doc_events`：变更 outbox

**为什么需要**：隐藏、删除、更新会牵动向量库、父文档、概念、题目、队列、报告、缓存。逐处手写级联必然漏，必须有一个事件表驱动。

```sql
CREATE TABLE doc_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id       TEXT NOT NULL,
  event        TEXT NOT NULL,   -- ingest|update|hide|unhide|archive|delete|supersede|merge
  payload_json TEXT,
  created_at   TEXT NOT NULL,
  processed_at TEXT
);

CREATE TABLE consumer_cursor (
  consumer   TEXT PRIMARY KEY,  -- vector_index|mentions|items|reports|queue|cache
  last_event INTEGER NOT NULL DEFAULT 0
);
```

流程：

1. 写 `docs_meta` 与 `doc_events` 在**同一事务**内
2. 各 consumer 轮询未处理事件，处理完成后更新自己的 cursor
3. 失败重试 + 退避；毒事件进 `dead_letter` 表并告警
4. `kb repair` 可从任意 cursor 重放，保证最终一致

### 6.3 派生数据清单

| Consumer | 维护的数据 | 可重建 | 幂等键 |
| --- | --- | --- | --- |
| `vector_index` | Chroma chunks | ✅ 从父文档 | chunk_hash + manifest |
| `mentions` | 概念出现记录 | ✅ 重抽取 | chunk_hash + prompt_version |
| `items` | 题库 | ✅ 重抽取 + 校验 | content_hash + prompt_version + type |
| `concept_pages` | 派生 Markdown | ✅ 重生成 | concept_id + updated_at |
| `reports` | 周报 | ✅ 重算 | week_id |
| `queue` | 今日队列 | ✅ 重算 | date |
| **`attempts` / `learner_state`** | **学习历史** | ❌ **不可重建** | 必须单独备份 |

最后一行是关键：**派生数据可随意重建，学习历史不行。** 备份策略要区别对待。

### 6.4 一致性原则

- 事件处理必须**幂等**（重复处理不产生重复数据）
- consumer cursor 与数据写入**同一事务**
- 允许最终一致，但用户可见操作（hide/delete）应同步返回"已生效"，后台完成级联
- 提供 `kb repair --consumer items` 之类的命令，人工修复

### 6.5 审计

```sql
CREATE TABLE audit_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  actor      TEXT,
  action     TEXT NOT NULL,       -- hide|unhide|delete|merge|supersede|vault_add|unlock
  target     TEXT,
  detail_json TEXT,               -- 不含原文
);
```

---

## 7. 实施优先级

| 阶段 | 内容 |
| --- | --- |
| **P0（已完成 2026-10-30）** | `docs_meta`、`doc_events`、`consumer_cursor`、`content_hash`、scope 过滤、`embedding_manifest` + 指针、`answer_log`、`audit_log`、只读/admin Key、隐藏文档测试 ✅ |
| **P1** | 闸 1 可答性、闸 2 引用契约、级联 consumers（vector/mentions/items/reports）、库外评测集、检索 fail-closed scope |
| **P2** | 闸 3 声明校验（严格模式）、过期检测与冲突队列、近似去重、题库去重 |
| **P3+** | 历史查询（as-of）、事实级共识、embedding 迁移演练、段落级隐藏 |

**P0 的治理元数据是刚需**：如果先做题库再补 scope/时效/哈希，所有后续数据都要回填，成本高且容易漏。

---

## 8. 指标汇总

| 指标 | 定义 | 目标 | 阶段 |
| --- | --- | --- | --- |
| `faithfulness` | 答案事实被来源支持的比例 | ≥ 0.9 | P1 |
| `citation_accuracy` | 引用正确率 | ≥ 0.95 | P1 |
| `refusal_recall` | 库外问题正确拒答率 | ≥ 0.9 | P1 |
| `over_refusal` | 可回答问题误拒率 | ≤ 0.1 | P1 |
| `unsupported_claim_rate` | 校验器判定无据的陈述占比 | ≤ 0.05 | P2 |
| `hidden_leak_count` | 隐藏内容泄漏次数 | 0 | P0 |
| `expired_rate` | 过期文档占比 | 监控 | P2 |
| `stale_answer_rate` | 使用过期来源且未标注的比例 | ≤ 0.02 | P2 |
| `duplicate_rate` | 重复文档占比 | < 0.1 | P2 |
| `topk_dup_ratio` | top-k 中重复内容占比 | < 0.2 | P2 |
| `embedding_rollback_time` | 切回旧索引所需时间 | < 5 分钟 | P3 |

---

## 9. 开放问题

1. 隐藏粒度：文档级够用，还是需要段落级？（段落级成本显著更高）
2. 是否需要只读分享 / 访客访问？
3. 过期判断是否全部接受人工确认，还是允许高置信度自动 `supersedes`？
4. 严格模式是否默认开启？（成本 vs 可信度）
5. 是否接受"复述类"知识保留带来的存储与检索开销？
6. 学习历史（`attempts` / `learner_state`）的备份策略与加密要求？
7. 是否需要历史查询（"截至 2024 年"）作为一等能力？
