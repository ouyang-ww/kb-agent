# L1 → L3 最小可行闭环拆解

> 状态：**实施建议**。与 [PLAN v4](../PLAN.md) 配套；本文档细化阶段与验收，排期以 PLAN 为准。
> 前置阅读：[愿景](./vision.md)、[知识图谱与本体学习笔记](./knowledge-graph-and-ontology.md)、[知识治理](./knowledge-governance.md)。
> 目标：用最小工程量证明"**采集 → 结构化 → 出题 → 复习 → 反馈**"这条闭环值得继续投入，并达到 L3（学习教练）。

---

## 0. 范围

**做**：概念抽取（本体-lite）、题库生成、每日复习队列、学习者模型、周报、最小知识地图、学习效果评估。

**不做**（MVP 非目标）：

- L4 能力：目标管理、主动 agent、跨笔记综述、教-回（teach-back）
- 完整 OWL 本体、图数据库（先用 SQLite 关系表）
- 花哨的可视化、移动端、多用户
- 自动改写/合并源笔记
- 社区摘要（微软 GraphRAG 式全局问答）

---

## 1. 最小闭环：唯一的验收对象

整个 MVP 只服务这一条链路，任何新增功能都必须先回答"它让这条链路更顺了吗"：

```
① kb study ingest note.md
      → 抽取 3–7 个概念、3–5 道带出处的题、建立与已有概念的链接

② 次日 kb study today
      → 题目出现在队列里（不需要主人记得"该复习了"）

③ 作答 + 信心值（1–3）
      → MCQ 精确判分；简答按评分点判分

④ 记录 attempts → 更新 mastery → 写入下次复习时间

⑤ 第 7 天 kb study report
      → 保持率、衰退概念、覆盖变化、下周建议

⑥ kb study map / concept <id>
      → 能看到掌握度与概念页的变化

⑦ 循环
```

**闭环判据**：链路上任何一步需要主人**手工记账**（"我记得明天要复习 X"），闭环就没有闭合，先修那一步。

---

## 2. 先做纵向切片（第 1 周，约 5 人日）

**不要先做出题流水线。** 先用硬编码内容把 UX 走通，验证"每天真的会用"：

| 天 | 做什么 | 产出 |
| --- | --- | --- |
| D1 | 建 `study.sqlite3` 表 + 3 道硬编码题（来自 `examples/sample_notes/01_rag基础.md`） | 数据能落盘 |
| D2 | `kb study today` / `kb study answer`，SM-2 简化版 | 能答题、能排下次时间 |
| D3 | mastery 显示 + 概念页骨架（Markdown） | 能看到状态变化 |
| D4 | 记录信心值 + 一行周报摘要 | 校准数据开始积累 |
| D5 | **自己真实用 3 天**，记录每个摩擦点 | 一份摩擦清单，决定后续取舍 |

**切片验收**：每天操作 < 2 分钟，无手工记账，现有 71 个测试不受影响。

> 这一步的产出不是功能，而是**证据**：证明你会每天打开它。如果 D5 发现自己不想用，先改交互，别继续堆功能。

---

## 3. 阶段拆解

> 估算按单人、熟悉本仓库、每天有效 4–6 小时计。包含 P0 治理骨架后，总计约 **7–10 周**（业余）或 **3.5–4.5 周**（全职）。

### P0 基础设施 + 治理骨架（5–8 人日）

**学习侧**

- `study.sqlite3` 独立库（不复用 `documents.sqlite3`；除学习历史外均可重建）
- 版本化 migration（`schema_version` 表）
- `src/kbagent/study/` 包骨架 + `KB_STUDY_ENABLED` 开关
- 幂等：`content_hash = sha256(规范化文本)` + `prompt_version`
- 统一 LLM 调用封装：重试、限流、JSON 模式、schema 校验

**治理侧**（详见 [知识治理](./knowledge-governance.md)）

- `docs_meta`：`scope` / `status` / `authority` / `valid_from` / `valid_until` / `review_by` / `exclude_from_learning`
- `doc_events` + `consumer_cursor`：变更 outbox，隐藏/更新/删除可级联到向量库、概念、题目、报告
- 检索入口**强制传 scope**（未传则报错，而不是默认放行），防止未来代码路径绕过过滤
- `embedding_manifest` + `runtime_pointer`：索引版本化，为后续换模型留好接口
- `answer_log`：回答、检索结果、引用、校验结果落库（P1 接入闸门后开始有价值）
- 自动化测试：隐藏文档的精确标题查询在 `search` / `ask` / `study` 三处均无结果

**验收**：

- `kb study stats` 可运行；开关关闭时现有行为完全不变；现有测试全绿
- 隐藏测试 6 条全过（见治理文档第 2.7 节）
- 事件重放可从任意 cursor 重建派生数据
- 现有检索评估无退化

### P1 知识结构（8–12 人日）

- 本体-lite YAML 加载与校验
- 概念抽取：按 chunk 调用 LLM，输出严格 JSON（实体 + 定义 + 别名）
- 规范化 + 别名表 + 嵌入相似度候选合并
- `mentions` 写入（带 doc_id / chunk_id / 原文片段）
- 概念页生成（派生的 Markdown，不写回源目录）
- 地图文本视图（按领域分组 + 掌握度标记）
- 存量回填：`kb study ingest --backfill --dry-run` 先出审核报告

**验收**：

- 4 篇示例笔记产出 ≤ 15 个概念，人工抽检 20 篇真实笔记，概念"有意义"比例 ≥ 90%
- 重复概念率 < 10%
- **任何源文件内容不变**（用哈希校验）

### P2 题库（10–15 人日）

- 出题：3 种题型（辨析单选 / 纠错判断 / 简答），每题绑定出处
- 自动校验：答案必须能由出处片段推出，否则丢弃
- 去重（题干嵌入相似度）+ 标记 + 隔离
- 答题交互（CLI 优先，Web 次之）
- 判分：MCQ 精确匹配；简答走评分点 rubric
- 题目统计（曝光数、正确率、被标记率）

**验收**：

- 首批 50 题人工审核，无效题 < 10%
- 100% 的题带可定位出处；答案为"出处片段可推出"
- 被标记题能进入隔离区，不再出现

### P3 学习者模型 + 调度（8–12 人日）

- 每个概念的 mastery（Elo 简化版）
- 每个题目的调度（SM-2 简化版；可选 FSRS 包）
- 每日队列策略（到期上限、新概念规则、时间预算、交错）
- 积压与休假处理
- 校准指标

**验收**：

- 7 天自测：每日队列 < 15 分钟，无同日重复题，积压上限生效
- mastery 能点开看到最近 10 次作答依据
- 答对/答错能稳定改变下次复习时间（单元测试覆盖）

### P4 闭环报告 + 最小地图（5–8 人日）

- 周报生成（Markdown → `~/.kbagent/reports/`）
- 最小地图：文本视图 + 一个简单 HTML 页（`/study/map`）
- 卫生指标：孤儿、重复、未处理合并建议、覆盖率
- 可选：链接建议（"这两篇可能相关"）

**验收**：周报数字与数据库查询一致；主人每周会看一次；地图能一眼看出"下周该学什么"。

### P5 评估与打磨（5–8 人日）

- 学习评估脚本（见第 11 节）
- 题目质量内部看板
- prompt 调优 + 版本升级 + 选择性重抽取
- 性能：批量、并发、失败重试、增量

**验收**：现有检索评估无退化；14 天试点达到第 13 节的完成定义。

---

## 4. 数据模型

独立库 `study.sqlite3`（所有表均为派生数据，可从源文件重建）：

```sql
-- 概念
CREATE TABLE concepts (
  id            TEXT PRIMARY KEY,          -- 稳定 ID，如 c_<hash>
  name          TEXT NOT NULL,
  normalized    TEXT NOT NULL,             -- NFKC + 小写 + 去标点
  domain        TEXT,
  definition    TEXT,
  aliases_json  TEXT DEFAULT '[]',
  status        TEXT DEFAULT 'active',     -- active | merged | archived
  created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_concepts_norm ON concepts(normalized, domain);

-- 概念关系（前置/包含/相关/矛盾/细化）
CREATE TABLE concept_edges (
  src_id     TEXT NOT NULL REFERENCES concepts(id),
  dst_id     TEXT NOT NULL REFERENCES concepts(id),
  type       TEXT NOT NULL,
  confidence REAL DEFAULT 1.0,
  provenance TEXT,                         -- doc/chunk 或 "user"
  PRIMARY KEY (src_id, dst_id, type)
);

-- 概念在文中的出现（溯源）
CREATE TABLE mentions (
  concept_id TEXT NOT NULL REFERENCES concepts(id),
  doc_id     TEXT NOT NULL,
  chunk_id   TEXT,
  snippet    TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX ix_mentions_concept ON mentions(concept_id);
CREATE INDEX ix_mentions_doc ON mentions(doc_id);

-- 题目
CREATE TABLE items (
  id              TEXT PRIMARY KEY,
  concept_ids     TEXT NOT NULL,           -- JSON 数组
  type            TEXT NOT NULL,           -- mcq | fix_wrong | short
  stem            TEXT NOT NULL,
  options_json    TEXT,
  answer          TEXT NOT NULL,
  rubric_json     TEXT,                    -- 评分点
  source_doc_id   TEXT NOT NULL,
  source_chunk_id TEXT,
  source_span     TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  model           TEXT,
  status          TEXT DEFAULT 'active',   -- active | retired | quarantined
  created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_items_idem ON items(content_hash, prompt_version, type);

-- 作答
CREATE TABLE attempts (
  id         TEXT PRIMARY KEY,
  item_id    TEXT NOT NULL REFERENCES items(id),
  ts         TEXT NOT NULL,
  response   TEXT,
  correct    REAL NOT NULL,                -- 0 / 0.5 / 1
  confidence INTEGER,                      -- 1..3，作答前采集
  latency_ms INTEGER,
  grader     TEXT,                         -- exact | llm | user
  feedback   TEXT
);

-- 学习者状态（按概念）
CREATE TABLE learner_state (
  concept_id   TEXT PRIMARY KEY REFERENCES concepts(id),
  mastery      REAL DEFAULT 0.3,           -- 0..1 能力估计
  stability    REAL DEFAULT 1.0,           -- 天
  difficulty   REAL DEFAULT 0.5,           -- 0..1
  last_review  TEXT,
  next_review  TEXT,
  attempts_n   INTEGER DEFAULT 0,
  updated_at   TEXT NOT NULL
);

-- 复习调度（按题目）
CREATE TABLE reviews (
  item_id     TEXT PRIMARY KEY REFERENCES items(id),
  ease        REAL DEFAULT 2.5,
  interval_d  REAL DEFAULT 0,
  reps        INTEGER DEFAULT 0,
  lapses      INTEGER DEFAULT 0,
  next_due    TEXT
);

-- 参照系（外部大纲）
CREATE TABLE outline_nodes (
  id         TEXT PRIMARY KEY,
  domain     TEXT NOT NULL,
  name       TEXT NOT NULL,
  aliases_json TEXT DEFAULT '[]',
  parent_id  TEXT,
  ord        INTEGER
);

-- 元信息
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
```

设计要点：

- **概念与题目分离**：概念是长期资产，题目是可替换的消耗品
- **出处强制**：`mentions` 与 `items.source_span` 保证一切可回溯
- **幂等**：同一 chunk + 同一 prompt 版本 + 同一题型 → 只生成一次
- **可重建**：清空库 + 重放源文件即可恢复（作答历史除外，需单独备份 `attempts` / `reviews`）

---

## 5. 算法选择

### 5.1 掌握度（按概念）

MVP 用 **Elo 简化版**，透明、可解释、实现成本低：

```text
s        ∈ [0,1]   概念能力分，初始 0.3
d        ∈ [0,1]   题目难度，初始由 LLM 估计（或 0.5），随个人作答微调
expected = 1 / (1 + exp(-(s - d) * 4))
s'       = clamp(s + 0.15 * (outcome - expected), 0.05, 0.95)
```

- `outcome`：0 / 0.5 / 1（对 / 部分 / 错）
- **不要**把"遗忘"混进 mastery：mastery 表示"你会多少"，衰退用调度状态表达（是否逾期、间隔长度）
- 展示时同时给出：mastery、最近复习、下次到期、最近 10 次作答

**升级路径**（数据足够后）：BKT（贝叶斯知识追踪）或 IRT。MVP 不做。

### 5.2 调度（按题目）

MVP 用 **SM-2 简化版**：

```text
grade: 对=4，部分=3，错=1（信心低但答对不加分）
ease'    = clamp(ease + (0.1 - (5-grade) * (0.08 + (5-grade) * 0.02)), 1.3, 2.8)
interval = 1, 3, round(interval * ease') ...
lapse    → interval = 1, ease - 0.2
```

- 备选：社区维护的 `fsrs` Python 包（选型时核实版本、许可与 API 稳定性）
- **概念到期时间** = 该概念 active 题目中最早的 `next_due`
- **题目生命周期**：连续 3 次答对且间隔 ≥ 7 天，或曝光 ≥ 6 次 → `retired`；概念 mastery 仍 < 0.5 且 active 题 < 2 时生成变体

> 题目退役很重要：防止"记住题目"而不是"掌握概念"。

### 5.3 判分

| 题型 | 判分 | 说明 |
| --- | --- | --- |
| 辨析单选 | 精确匹配 | 选项唯一 |
| 纠错判断 | 精确匹配 + 指出错误点 | 错误点用关键词匹配，可降级 |
| 简答 | LLM rubric | 3–5 个评分点，输出 `{score, missing[], wrong[]}` |

简答判分 prompt 的输出契约：

```json
{"score": 0.5, "missing": ["没有提到重排"], "wrong": ["把混合检索说成必须依赖图数据库"]}
```

- 判分必须引用**评分点**而非自由发挥
- 允许主人**覆盖判分**（`grader='user'`），这是校准数据的一部分
- 判分失败时降级为"自评 + 记录"

### 5.4 校准

```text
calibration_error = mean(|confidence/3 - correct|)
overconfidence    = P(confidence = 3 且 correct < 0.5)
```

每题**作答前**采集信心值（1 = 猜的，2 = 有点把握，3 = 确定）。

---

## 6. 出题与质量控制

这是整个 MVP 质量风险最高的部分。

### 6.1 生成约束

- 每题必须绑定**一个** chunk 的连续原文片段
- 答案必须能由该片段推出；推不出就丢弃
- 不用"以上都对/都不对"，不用双重否定
- 干扰项必须来自**同领域其他概念**，不能是明显荒谬项
- 题干不得直接复制原文整句（避免"关键词匹配"即可答对）
- 每个概念同一时间最多 3 道 active 题

### 6.2 两段式生成 + 校验

```
① 生成：LLM 输出题目 JSON（含 source_span、答案、评分点）
② 校验：另一次 LLM 调用（或规则）判断"仅凭 source_span 能否推出答案"
    → 不能：丢弃；能：入库
```

校验调用可以先和生成合并为一次（要求模型自检并给出依据），质量不够再拆开。

### 6.3 题目生命周期

```
生成 → active → (被标记) quarantined → 人工处理
              → (连续答对/曝光足够) retired
              → (概念仍弱且题不足) 生成变体
```

- 首批 50 题**必须人工全审**
- 被标记率 > 10% 时，暂停自动出题，先修 prompt
- 记录 `prompt_version`，升级后可选择性重生成

### 6.4 prompt 骨架

```text
你是出题器。基于给定片段出一道题，严格输出 JSON。
约束：
1. 只能使用片段中出现的事实，不得引入外部知识
2. 答案必须能仅凭片段推出
3. 干扰项要来自同领域、看起来合理
4. 不得出现"以上都对/都不对"
输出：
{"type": "mcq|fix_wrong|short", "stem": "...", "options": ["..."],
 "answer": "...", "explanation": "...", "rubric": ["..."],
 "source_span": "片段中的连续原文", "difficulty": 0.0-1.0}
```

---

## 7. 概念抽取与消歧（MVP 做法）

### 7.1 本体-lite

```yaml
domain: rag
entity_types: [Concept, Method, Tool, Metric]

relation_types:
  prerequisite:  { from: Concept, to: Concept, transitive: true }
  part_of:       { from: Concept, to: Concept }
  implemented_by:{ from: Method,  to: Tool }
  measured_by:   { from: Method,  to: Metric }
```

### 7.2 概念判据

**"能被问一道题的最小单元"**。这个判据同时解决"太细/太粗"：

- "RAG" 可以 → 可出"RAG 的核心流程是什么"
- "检索" 太泛 → 不可出题（除非有明确定义）
- "2024 年 8 月的那次实验" 太具体 → 归入事件而非概念

每 chunk 抽 0–3 个；每篇文档新增概念上限 7 个。

### 7.3 消歧顺序

1. **规范化**：NFKC、去空白、去标点、小写
2. **精确匹配**：`normalized + domain` 命中即复用
3. **别名表**：人工维护的高频别名
4. **嵌入候选**：相似度 > 0.88 且类型相同 → 标记为"建议合并"
5. **自动合并只允许第 1–2 步**；其余一律人工确认（`kb study concepts merge`）

---

## 8. 与现有代码的映射

### 8.1 新增包

```text
src/kbagent/study/
├── store.py        # study.sqlite3 访问、迁移、事务
├── schema.py       # 本体-lite 加载与校验
├── extract.py      # 概念/关系抽取
├── resolve.py      # 规范化、别名、候选合并
├── items.py        # 出题、校验、去重、生命周期
├── grade.py        # 判分（精确 + LLM rubric）
├── mastery.py      # Elo 简化版
├── scheduler.py    # SM-2、到期计算
├── queue.py        # 每日队列策略
├── outline.py      # 参照系导入与覆盖率
├── report.py       # 周报
└── service.py      # StudyService 门面（类似 kb.py 的 KnowledgeBase）
```

### 8.2 接入点

| 现有模块 | 改动方式 |
| --- | --- |
| `ingestion/indexer.py` | 不直接改；新增 `kb study ingest` 作为独立后处理，或加可选钩子 |
| `kb.py` | 新增 `study` 属性或独立门面，**不改变现有方法签名** |
| `cli.py` | 新增 `study` 子命令组 |
| `api/server.py` | 新增 `/v1/study/*` 路由，现有端点不动 |
| `config.py` | 新增 `KB_STUDY_*` 配置 |
| `eval/` | 新增学习评估模块，现有评估不动 |

### 8.3 CLI

```bash
kb study ingest <path> [--backfill] [--dry-run] [--force]
kb study today [--minutes 10] [--json]
kb study answer <item_id> --response "..." --confidence 2
kb study concepts [--domain rag] [--mastery below:0.5] [--due]
kb study concept <concept_id>          # 概念页
kb study concepts merge <a> <b> --dry-run
kb study map [--format text|html]
kb study outline import <file.yaml>
kb study report [--weekly|--monthly]
kb study items review                  # 被标记/待审题
kb study stats
```

### 8.4 REST

```
POST /v1/study/ingest
GET  /v1/study/today
POST /v1/study/attempts
GET  /v1/study/concepts/{id}
GET  /v1/study/map
GET  /v1/study/report/weekly
POST /v1/study/items/{id}/flag
```

### 8.5 配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `KB_STUDY_ENABLED` | false | 总开关，默认不影响现有行为 |
| `KB_STUDY_DAILY_MINUTES` | 10 | 每日时间预算 |
| `KB_STUDY_NEW_PER_DAY` | 1 | 每天新概念上限 |
| `KB_STUDY_ITEMS_PER_DOC` | 4 | 每篇出题上限 |
| `KB_STUDY_ONTOLOGY_PATH` | `~/.kbagent/ontology.yaml` | 本体-lite |
| `KB_STUDY_PROMPT_VERSION` | `r1` | 幂等与重生成 |
| `KB_STUDY_GRADER_MODEL` | 同 `KB_LLM_MODEL` | 判分模型（可换更便宜的） |

### 8.6 测试策略

- `scheduler` / `mastery`：纯函数 + 注入时钟，确定性单元测试
- `grade`：规则判分覆盖边界；LLM 判分用 fake client
- `items`：校验器拒绝"无出处/推不出"的题
- `queue`：时间预算、交错、积压上限
- 集成：一篇笔记 → 概念/题目/答题/到期时间全链路
- 回归：现有 71 个测试必须全绿

---

## 9. 每日队列策略

```python
def build_today(now, budget_seconds=600):
    due   = fetch_due(now, cap=20, order="overdue_desc")
    fresh = pick_new_concepts(n=1, prereq_mastery_ok=True)   # 每概念 2 题
    items = interleave(due, fresh, max_same_concept_in_row=1)
    return cut_by_time(items, budget_seconds)
```

规则：

- **到期优先**，但上限 20 道；积压超过 2 倍上限时，按"逾期最久 + 概念覆盖率"挑，不一次全推
- **新概念**只在有到期题时穿插，前置概念 mastery < 0.5 则不推
- **交错**：同一概念连续不超过 1 题
- **时间预算**到达即结束（剩余题顺延，不积累压力）
- **休假模式**：`kb study today --vacation` 只留 5 道，其余整体顺延
- 不做通知轰炸：每日最多 1 条提醒，可在配置关闭

---

## 10. 参照系与覆盖度

`outlines/<domain>.yaml`：

```yaml
domain: rag
source: "《RAG 实践》目录"      # 出处，便于判断权威性
nodes:
  - name: 向量检索
    aliases: [语义检索, dense retrieval]
    children:
      - name: 嵌入模型
      - name: 相似度度量
  - name: 重排
  - name: 混合检索
```

- 导入：用户粘贴目录 → LLM 转 YAML → 人工确认 → 入库
- 覆盖度：`outline 概念中有 mention 且 mastery ≥ 0.6 的比例`
- 缺口：有 outline 节点但无 mention 的概念
- MVP 只支持一个领域、一层主要结构即可

---

## 11. 评估

### 11.1 离线评估（每次改动跑）

| 指标 | 方法 | 目标 |
| --- | --- | --- |
| 概念抽取质量 | 20 篇人工标注，算 P/R | P ≥ 0.8，R ≥ 0.7 |
| 概念重复率 | 自动 + 抽检 | < 10% |
| 题目有效率 | 50 题人工审核 | ≥ 90% |
| 题目可溯源性 | 自动检查 | 100% |
| 判分一致性 | 人工标注 30 条简答对比 | ≥ 85% 一致 |
| 检索基线 | 现有 `kb eval` | 无退化 |

### 11.2 试点评估（14 天真实使用）

| 指标 | 目标 |
| --- | --- |
| 每日队列完成率 | ≥ 80% |
| 单次时长 | 中位数 ≤ 12 分钟 |
| 7 天保持率 | ≥ 85%（“到期后首答正确”比例） |
| 校准误差 | ≤ 0.2，且趋势下降 |
| 被标记题目率 | < 10% |
| 周报打开率 | 每周 ≥ 1 次 |

> 14 天不足以测 30 天保持率；MVP 先看 7 天，30 天指标等 L3 稳定后再看。

### 11.3 护栏

- 源文件哈希不变
- 派生库可重建，重建后指纹一致（除作答历史）
- 关闭 `KB_STUDY_ENABLED` 后，L1 行为与性能不变
- LLM 调用失败不阻塞入库主流程（后处理可重试）

---

## 12. 风险与应对

| 风险 | 早期信号 | 应对 |
| --- | --- | --- |
| 出题质量差 | 标记率 > 10%、正确率异常高（题目太蠢） | 暂停自动出题；加校验；首批人工审 |
| 概念粒度失控 | 概念数爆炸、大量无定义名词 | "可出题的最小单元"判据 + 每篇上限 + 合并建议 |
| 主人不用 | 完成率 < 50% | 缩到 5 分钟；减少新概念；只看复习 |
| 掌握度不可信 | 自评与实际偏差大、用户不认同 | 展示作答依据；允许手动重置；先简单后复杂 |
| 维护负担 | 回填/重抽取耗时失控 | 批量、增量、幂等、限流、失败可续 |
| 隐私顾虑 | 不愿把笔记发给云端 | 本地模型选项；按目录排除；抽取前提示 |
| 过度设计 | 花两周做可视化 | 先文本视图；地图只在 P4 做最小版 |

---

## 13. L3 完成定义（DoD）

以下全部满足，才算 L3 达成：

- [ ] 新笔记入库后 3 分钟内产生概念、链接、带出处的题
- [ ] 次日题目自动出现在队列，无需手工记账
- [ ] 作答能更新 mastery 并正确计算下次复习时间
- [ ] 退役/变体机制防止"背题"（同一题曝光不超过 6 次）
- [ ] 每日队列在时间预算内完成，积压可控
- [ ] 周报能回答"这周学了什么、什么在衰退、下一步做什么"
- [ ] 概念页能看到定义、出处、相关笔记、掌握度、题目
- [ ] 参照系覆盖度可查询
- [ ] 离线评估达标（第 11.1 节）
- [ ] 14 天试点达标（第 11.2 节）
- [ ] 现有 L1 测试与评估无退化；源文件零修改

---

## 14. 建议的执行顺序（一句话版）

```
第 1 周：纵向切片 + 治理骨架（验证你会每天用）
第 2–3 周：知识结构（概念 + 出处 + 消歧）
第 4–6 周：题库 + 判分 + 质量闸门
第 7–8 周：掌握度 + 调度 + 每日队列
第 9–10 周：周报 + 最小地图 + 评估 + 14 天试点
```

**在第 5 天（纵向切片结束）设一个 Go/No-Go**：如果自己不愿意连续用 3 天，回到交互设计，不要进入 P1。

---

## 15. 需要决策的开放问题

1. 每天时间预算：5 / 10 / 15 分钟？
2. 首批领域：只做 RAG，还是同时做 Python / 健康饮食？
3. 出题语言与难度：面向"自己复习"还是"能讲给别人听"？
4. 是否接受云端 LLM 处理全部笔记？哪些目录要排除？
5. 是否愿意每周花 2 分钟确认概念合并建议？
6. 简答判分错误时，是否接受手动覆盖（会污染校准数据，需标记）？
