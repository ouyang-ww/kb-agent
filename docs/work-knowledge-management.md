# 工作知识管理规划：记录层、回顾引擎与办公文件导入

> 状态：**未来扩展，不是当前 MVP**。当前计划以 [PLAN v4 §0.1](../PLAN.md) 为准。本文保留到该模块启动时再用。
> 与 [个人档案管理](./personal-archive.md)、[架构评估](./architecture-readiness-personal.md)、[PLAN v4](../PLAN.md) 配套。
>
> 已确认的约束：
> 1. **输入以办公文件为主**——Word / Excel / PPT / 纯文本 / Markdown 是主要来源；若后续接入任务工具，仍按"源系统为事实来源、单向导入"处理，v1 不回写。
> 2. **普通工作内容允许云端 LLM；confidential 数据一律本地**——事故、人事、客户、密钥等敏感内容不进云端。
> 3. **用户侧导出统一为 Markdown**——周/月/年报、记录导出都用 Markdown；内部备份仍保留结构化格式。
> 4. **年度报告按自然年**；**事故分级用户自定义**；**报告面向自己回顾**。

---

## 1. 定位与边界

工作内容不是一个整体，而是三层：

| 层 | 内容 | 形态 | 处理方式 |
| --- | --- | --- | --- |
| **知识型** | 操作手册、SOP、流程模板、复盘、会议纪要 | 版本化文档 | 现有 RAG + 版本/时效 |
| **记录型** | 项目、任务、进度、目标、亮点、事故、优化建议 | 结构化记录 + 事件流 | 新记录层，不进向量库 |
| **派生型** | 周报、月报、年报、看板 | 从上面两层计算 | 回顾引擎生成，不是源数据 |

一句话：**手册用来查，任务用来算，报告用来看。**

### 1.1 不做的事

- **不做 Jira 克隆**：不做看板、甘特图、双向同步、多人协作
- **不做绩效系统**：报告是给自己回顾的草稿，不是考核依据
- **不自动改写源工具**：v1 只有导入，没有回写
- **不让 LLM 推断任务状态**：状态变化必须来自导入或显式确认
- **不把工作与个人数据混检**：默认互相不可检索

### 1.2 产品形态

- 日常：用现有工具或文件管理任务；kb-agent 不做日常看板
- 摄入：`kb work import-doc` 导入 Word/PDF/PPT/文本，`kb work import-table` 导入 Excel/CSV 记录
- 周期：`kb review work --period ...` 生成周/月/年报，导出 Markdown
- 随时：`kb search` 查手册、复盘、会议纪要（带版本与出处）
- 记录：`kb work highlight / incident / suggestion` 低摩擦补充结构化记录

---

## 2. 与个人档案共用底座

个人档案与工作档案形状相同：**带时间的记录 + 叙事文档 + 周期回顾**。不要写两套。

```
共享记录层 kbagent/records/
  items / item_events / links / periods / reports
   ├── domain=personal  → 事实、事件、指标、回顾
   └── domain=work      → 项目、任务、目标、亮点、事故、建议、报表
```

- 代码共用：`RecordStore` Protocol、`ReviewEngine`、`ReviewSection`、期间模型、链接表
- 数据分开：`personal.sqlite3` / `work.sqlite3`，便于隔离、备份、删除
- 健康数据仍是独立的加密 `health.sqlite3`，不进共享层

这样周报引擎和个人周回顾共用同一套骨架，只是 section 不同。

---

## 3. 数据模型

### 3.1 核心表

```sql
-- work.sqlite3
items(
  id            TEXT PRIMARY KEY,
  domain        TEXT NOT NULL DEFAULT 'work',
  type          TEXT NOT NULL,        -- project | task | goal | highlight | incident | suggestion
  title         TEXT NOT NULL,
  status        TEXT NOT NULL,
  start_ts      TEXT,
  end_ts        TEXT,
  due_ts        TEXT,
  parent_id     TEXT,                 -- 子任务 / 子目标
  project_id    TEXT,
  period_key    TEXT,                 -- 2026 / 2026-M10 / 2026-W43 / 2026-Q4
  target        REAL,
  current       REAL,
  unit          TEXT,
  payload_json  TEXT DEFAULT '{}',    -- 类型特有字段
  source_doc_id TEXT,                 -- 关联的叙事文档
  scope         TEXT NOT NULL DEFAULT 'work',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

item_events(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id    TEXT NOT NULL,
  ts         TEXT NOT NULL,
  type       TEXT NOT NULL,           -- created | status_changed | progress | imported | linked | ...
  from_status TEXT,
  to_status   TEXT,
  note       TEXT,
  actor      TEXT DEFAULT 'local'
);

links(
  from_id TEXT NOT NULL,
  to_id   TEXT NOT NULL,
  type    TEXT NOT NULL,              -- task_of | goal_of | caused_by | action_of | implements | mentions
  PRIMARY KEY (from_id, to_id, type)
);

periods(
  type      TEXT NOT NULL,            -- year | quarter | month | week
  key       TEXT NOT NULL,            -- 2026 / 2026-Q4 / 2026-M10 / 2026-W43
  start_ts  TEXT NOT NULL,
  end_ts    TEXT NOT NULL,
  PRIMARY KEY (type, key)
);

reports(
  id            TEXT PRIMARY KEY,
  domain        TEXT NOT NULL DEFAULT 'work',
  period_key    TEXT NOT NULL,
  status        TEXT NOT NULL,        -- draft | final
  generated_at  TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  prompt_version TEXT,
  content_doc_id TEXT,
  meta_json     TEXT DEFAULT '{}'
);
```

### 3.2 为什么这样分

- `items` 用 `type` + 少量可查询列（状态、时间、父级、目标值），类型特有字段放 `payload_json`
- 任务、亮点、事故、建议共享事件与链接，查询仍走索引列
- 进度**不覆盖**：每次状态/百分比变化写 `item_events`，历史可回放
- 目标进度不静默推断：`current` 显式更新，或由已定义的关联任务规则汇总

### 3.3 类型特有字段（payload 示例）

```json
// incident
{"severity": "S2", "impact": "支付失败 20 分钟", "root_cause": "...",
 "detected_at": "...", "resolved_at": "...", "actions": ["..."]}

// suggestion
{"expected_impact": "...", "actual_impact": "...", "decision": "accepted",
 "decided_at": "..."}

// highlight
{"impact": "...", "evidence": ["item:...", "doc:..."]}

// task
{"source_priority": "P1", "blocked_reason": "..."}
```

---

## 4. 导入：办公文件优先，任务工具可后接

工作内容的来源不确定，v1 按"文件优先"设计：

| 输入 | 去向 | 说明 |
| --- | --- | --- |
| **文档型**：Word / PDF / PPT / 文本 / Markdown | 知识层（手册、复盘、会议纪要） | 切块后进 RAG，保留出处定位 |
| **表格型**：Excel / CSV | 记录层（任务、目标、指标） | 必须提供字段映射，映射结果可预览 |
| **任务工具**：Jira / 飞书 / Notion | 记录层 | 后续单向快照导入；v1 不接 API |

### 4.1 文档型导入

| 格式 | 解析内容 | 出处定位 |
| --- | --- | --- |
| `.docx` | 段落、标题、表格、脚注；不执行宏 | 标题路径 / 段落号 |
| `.pdf` | 现有 pypdf；扫描件可选本地 OCR | 页码 |
| `.pptx` | 每页文本 + 演讲者备注 | 幻灯片号 |
| `.xlsx` | 工作表单元格缓存值（不求值公式） | `工作表!单元格` |
| `.txt` / `.md` | 现有 loaders | 行号 / 标题 |
| `.doc` / `.xls` / `.ppt` | 旧二进制格式不支持 | 提示先转 OOXML 或纯文本 |

规则：

- 内容哈希去重；保留原始文件路径与哈希；源文件不改
- 加密、损坏、超限文件跳过并告警（单文件大小 / 页数设上限）
- 图片仍走 `VisionIndexer`，但 `scope=work.confidential` 时**禁止云端描述**
- 文档元数据可带 `doc_kind` / `version` / `valid_from` / `valid_until`（见第 6 节）
- 解析结果只是派生物，可重建

### 4.2 表格型导入（映射到记录）

Excel/CSV 只有在提供映射时才转成记录；否则按文档处理。

```yaml
source: excel
file: tasks.xlsx
sheet: Sprint
columns:
  title: 任务名称
  status: 状态
  due_ts: 截止日期
  project: 项目
  source_id: 编号
status_map:
  待办: todo
  进行中: doing
  阻塞: blocked
  完成: done
```

- 未映射的列保留到 `payload_json`
- 无法识别的状态/日期进 `import_inbox`，不静默猜测
- 重复导入按 `(source_system, source_id, entity_type)` 幂等；缺少 `source_id` 时用内容哈希 + 标题作弱标识，并在报告中提示
- `--dry-run` 输出新增 / 更新 / 未映射 / 冲突，确认后再写入

### 4.3 未来任务工具导入

```sql
import_sources(
  id            TEXT PRIMARY KEY,
  source_system TEXT NOT NULL,        -- jira | feishu | notion | excel | manual
  source_id     TEXT NOT NULL,
  entity_type   TEXT NOT NULL,        -- task | project | goal
  local_id      TEXT NOT NULL,
  source_hash   TEXT NOT NULL,
  source_updated_at TEXT,
  last_seen_at  TEXT NOT NULL,
  UNIQUE(source_system, source_id, entity_type)
);
```

规则：

1. **幂等**：`(source_system, source_id, entity_type)` 唯一
2. **快照而非事件流**：只有 `source_hash` 变化才写 `imported` 事件
3. **删除用墓碑**：源里消失的记录标记 `source_missing`，不硬删除
4. **字段所有权**：源系统拥有状态、指派人、截止日期；本地拥有亮点、复盘、建议、备注，导入**不得覆盖本地字段**
5. **未映射进收件箱**：`import_inbox` 等人工确认
6. **只读**：v1 不调用源系统写接口，不保存源系统凭证

字段所有权矩阵：

| 字段 | 源系统 | 本地 | 冲突处理 |
| --- | --- | --- | --- |
| title / status / assignee / due_ts | ✅ | ❌ | 以源为准 |
| project / parent | ✅ | 可补充 | 源优先，本地补充写事件 |
| description | ✅ | ✅ | 源更新覆盖，本地修订另存文档 |
| highlight / incident / suggestion | ❌ | ✅ | 永不覆盖 |
| goal current | 可选 | ✅ | 手工或显式汇总，不猜 |

### 4.4 导入入口

```bash
kb work import-doc ~/work/manuals/                 # 文档型 → 知识层
kb work import-doc report.pptx --scope work.confidential
kb work import-table tasks.xlsx --mapping mappings/tasks.yaml --dry-run
kb work import-table tasks.csv --mapping mappings/tasks.yaml
# 后续
kb work import jira-export.csv --source jira --dry-run
```

先支持本地文件，不接 API。API 连接器作为后续可选 extra，且必须本地运行、凭证不入库。

---

## 5. 类型语义

### 5.1 项目与任务

- 项目：`type=project`，状态 active / paused / done / archived
- 任务：`type=task`，`project_id` 指向项目，`parent_id` 支持子任务
- 状态建议统一为 `todo | doing | blocked | done | cancelled`；导入时做映射
- 若没有任务工具，手工录入或表格导入就是事实来源；状态变化必须显式，不从聊天推断
- 阻塞必须记录 `blocked_reason` 与进入/离开时间，月报才能算阻塞时长
- 流程模板是文档，不是任务状态机；任务实例通过 `links(type=implements)` 指向当时使用的模板版本

### 5.2 目标

- 年度 / 季度 / 月度通过 `parent_id` 建层级，用 `period_key` 绑定周期
- 目标有 `target` / `current` / `unit`；没有 `current` 的目标显示为"未追踪"，**不按 0 计算**
- 目标变更写 `item_events(type=goal_changed)`，年报能解释"目标为什么改了"
- 进度来源必须可配置：手工 / 关联 KR / 关联任务计数；不同目标不要混用而未标注

### 5.3 亮点

- 发生时记录，不等写月报再回忆
- 结构：标题、影响、证据链接（任务/文档/指标）、项目
- 报告中区分"事实与证据"和"自我评价"，避免不可核实的绩效陈述

### 5.4 事故

双表示：

- 结构化 `type=incident`：严重度、起止、影响、根因、状态（open / mitigated / closed）
- 严重度**用户自定义**：默认 S1–S4，可在 `work_config` 或 `KB_WORK_SEVERITIES` 配置有序列表；未知标签保留并排在已知级别之后，不报错
- 叙事复盘文档：进 RAG，可检索、可引用
- 行动项转成任务或建议，通过 `links(type=action_of)` 关联
- 默认 `scope=work.confidential`，不进云端

### 5.5 优化建议

- 生命周期：`proposed → evaluating → accepted / rejected → implemented → verified`
- 记录预期收益与实际收益，用于统计**采纳率**和**落地率**
- 关联事故/任务，年报能回答"今年提了多少建议、落地了多少、带来什么变化"

---

## 6. 手册与 SOP 的版本化

现有代码只做了一半：`docs_meta` 有 `valid_from` / `valid_until` / `review_by` 字段（`src/kbagent/governance/store.py:26-27`），但检索不使用有效期；`doc_relations` 与 `supersede` 只在文档设计里，代码未实现。

工作手册需要补齐：

### 6.1 文档元数据

```yaml
doc_kind: manual          # manual | sop | retrospective | meeting_note | work_report
version: 3
applies_to: [role:oncall, process:release]
valid_from: 2026-01-01
valid_until: null
supersedes: doc_abc123
review_by: 2026-06-30
scope: work
```

### 6.2 关系表

```sql
doc_relations(
  from_doc_id TEXT NOT NULL,
  to_doc_id   TEXT NOT NULL,
  type        TEXT NOT NULL,   -- supersedes | implements | related | contradicts
  created_at  TEXT NOT NULL,
  PRIMARY KEY (from_doc_id, to_doc_id, type)
);
```

### 6.3 检索规则

- 默认只返回当前有效版本：`status=active` 且 `valid_until` 为空或晚于当前时间
- 被取代的版本默认不返回；`--as-of 2025-09-01` 时返回当时有效版本
- 回答必须标注手册版本与生效日期
- 任务实例可链接"执行时使用的模板版本"，复盘时能对齐

---

## 7. 回顾引擎：周报 / 月报 / 年报

### 7.1 生成原则

1. **数字由代码算**：完成数、周期时间、阻塞时长、事故数、建议采纳率、目标进度
2. **模型只组织语言**：叙述可由云端或本地模型生成；confidential 内容只用本地模型或模板
3. **可复现**：报告记录 `snapshot_hash`、`generated_at`、`prompt_version`；同一快照重跑结果一致
4. **草稿与定稿分离**：生成稿标记 `draft`；人工修改后另存为 `final` 文档，不覆盖生成稿

### 7.2 报告结构

| 报告 | 章节 |
| --- | --- |
| **周报** | 本周完成 / 进行中 / 阻塞、亮点、事故、建议、目标进展、下周重点 |
| **月报** | 目标进度、项目状态、周期时间与吞吐、事故趋势、建议采纳与落地、亮点、下月目标 |
| **年报** | 目标达成、重大项目、流程与能力变化、事故与改进、关键经验、下一年目标 |

周期约定：年度按**自然年**（1 月 1 日–12 月 31 日）；季度、月度按自然边界；周报用 ISO 周。财年暂不支持。

### 7.3 指标定义（自用，不做绩效）

| 指标 | 计算 |
| --- | --- |
| 吞吐 | 周期内 `done` 的任务数 |
| 周期时间 | `done_ts - start_ts` 的中位数 / p90 |
| 阻塞时长 | `blocked` 区间累计 |
| 事故 | 数量 + 严重度分布；可选 MTTR |
| 建议采纳率 | `accepted / proposed` |
| 建议落地率 | `implemented / accepted` |
| 目标进度 | `current / target`；未追踪单独标注 |

所有指标必须能点回原始 `items` / `item_events`。

### 7.4 入口

```bash
kb review work week  --period 2026-W43
kb review work month --period 2026-10
kb review work year  --period 2026
kb review work month --period 2026-10 --format md --open
```

生成稿存 `~/.kbagent/reviews/work/`；定稿可 `kb work report finalize <id>` 存为文档并设置 `doc_kind=work_report`。

### 7.5 导出格式：Markdown

用户侧统一 Markdown，带 YAML front-matter：

````markdown
---
domain: work
doc_kind: work_report
period: 2026-M10
generated_at: 2026-10-31T10:00:00+08:00
snapshot_hash: ...
sources: [item:..., doc:...]
status: draft
---

# 2026 年 10 月工作月报

## 目标进度
| 目标 | 当前 | 目标 | 状态 |
| --- | --- | --- | --- |
| ... | ... | ... | ... |
````

- 指标用 Markdown 表格；每条记录用 `[item:id]` 或 `[doc:id]` 标注出处
- 定稿另存，不覆盖生成稿
- Markdown 是**阅读与分享格式**，不是无损备份；内部备份仍用 SQLite 副本 + JSON 导出

---

## 8. 隐私与保密

| scope | 内容 | 云端 LLM | 本地 LLM | 向量化 |
| --- | --- | --- | --- | --- |
| `work` | 普通任务、目标、手册、周报 | ✅ 允许 | ✅ | ✅ 本地嵌入 |
| `work.confidential` | 事故复盘、客户信息、人事 | ❌ 禁止 | ✅ | ✅ 本地嵌入 |
| `work.secret` | 密钥、未公开并购等 | ❌ 禁止 | ❌（仅模板/结构化） | ❌ 或显式确认 |

报告面向自己回顾：模板以个人复盘语气为主，不追求对上汇报的措辞；v1 不提供"给上级"的导出路径，若将来需要，单独做脱敏导出。

规则：

- 工作域与个人域默认互相不可检索；跨域查询需要显式上下文
- 导入前扫描并拒绝密钥/token/密码类内容
- confidential 数据若未配置本地模型，报告用模板渲染，**绝不降级到云端**
- 导出给上级的报告只包含定稿内容，不含内部笔记与 confidential 事件
- 按项目/周期导出与删除，写审计
- 保留期可配置（例如事故复盘保留 3 年）

实现上依赖 [架构评估](./architecture-readiness-personal.md) 中的 `LLMGateway` + `ScopePolicy`，不接受"调用方自觉"。

---

## 9. 与现有架构的映射

### 9.1 新增包

```
kbagent/records/          # 共享记录层
  models.py               # Item / ItemEvent / Link / Period / Report
  store.py                # RecordStore Protocol + SQLite 实现
  periods.py              # 周期计算（年/季/月/ISO 周）
  review.py               # ReviewEngine + ReviewSection
kbagent/work/
  importer.py             # CSV / JSON / 字段映射 / 幂等 / 墓碑
  sections.py             # 周/月/年报告章节
  metrics.py              # 吞吐、周期时间、阻塞、建议采纳率
  cli.py
```

办公格式解析使用可选 extra，避免污染核心依赖：

```
work = ["python-docx", "openpyxl", "python-pptx"]
```

PDF 复用现有 `pypdf`；旧 `.doc` / `.xls` / `.ppt` 不支持。解析器统一返回带出处定位的段落/表格，交给现有 cleaner → chunker → 索引链路。

### 9.2 复用与前置

| 需要 | 来源 |
| --- | --- |
| 后端中立过滤 / 时间范围 | `src/kbagent/storage/filters.py:206` |
| 迁移框架、通用事件 | 架构评估 P0 #4 |
| 查询路由缝 | 架构评估 P0 #1 |
| LLM 网关与本地策略 | 架构评估 P0 #2 |
| fail-closed scope | 架构评估 P0 #3 |
| 日志脱敏 | 架构评估 P0 #5 |
| 文档版本 / 关系 / as-of | 本文第 6 节，需在 W0 规划 schema |

---

## 10. 阶段计划

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **W0 架构前置** | 复用个人档案 P0：路由缝、LLM 策略、scope 强制、通用事件、迁移、日志脱敏；规划文档版本 schema | 现有测试全绿；confidential 零出境测试通过 |
| **W1 记录层 + 文件导入** | `RecordStore`、项目/任务/目标/亮点/事故/建议、Word/Excel/PPT/文本解析、表格映射、字段所有权、墓碑、最小 CLI；不接 LLM | 四种办公格式可解析并保留出处；两次导入幂等；本地字段不被覆盖 |
| **W2 回顾引擎** | 周/月/年模板、确定性指标、快照与复现、草稿/定稿 | 报告数字与记录 100% 一致；同快照重跑一致 |
| **W3 手册版本化** | 手册元数据、`doc_relations`、默认只返回有效版本、`--as-of` 查询、流程模板链接 | 历史版本查询正确；过期版本不默认出现 |
| **W4 保密与打磨** | confidential 本地模型叙述、导出/删除、保留期、审计 | 导出不含敏感内容；删除级联完整 |

---

## 11. 验收标准

- [ ] 现有 `kb search` / `kb ask` 行为不变
- [ ] Word / Excel / PPT / 文本可解析，引用能定位到页 / 幻灯片 / 单元格
- [ ] 导入幂等，字段所有权明确，源系统删除不丢历史
- [ ] 周/月/年报中的每个数字都能点回原始记录
- [ ] 同一快照重跑报告结果一致
- [ ] 用户侧导出为 Markdown，包含出处与快照信息
- [ ] 年度周期按自然年，1 月 1 日–12 月 31 日
- [ ] 事故分级可由用户配置，未知级别不报错
- [ ] confidential 路径对云端 LLM 调用次数为 0（自动化测试）
- [ ] 工作域与个人域默认不可互相检索
- [ ] 手册默认只返回当前有效版本，`--as-of` 可查历史
- [ ] 不回写源系统，不保存源系统凭证
- [ ] 源文件哈希不变

---

## 12. 反模式

- 把任务、目标、报告都当笔记切块 → 搜得到但算不出
- 把周报当成唯一源数据 → 无法追溯、无法重算
- 在 kb-agent 里做看板/甘特图 → 与现有工具竞争且赢不了
- 让 LLM 从聊天推断任务状态 → 状态必须显式或导入
- 导入时覆盖本地复盘/建议 → 本地数据被源快照悄悄破坏
- 事故/人事数据默认上云 → 一次泄露不可逆
- 让 LLM 自动写绩效自评并当事实 → 报告只能是草稿，需人工定稿

---

## 13. 已确定与待定

已确定：

1. **输入以办公文件为主**：Word / Excel / PPT / 文本 / Markdown；任务工具导入后续再加
2. **用户侧导出统一为 Markdown**
3. **年度报告按自然年**
4. **事故分级用户自定义**
5. **报告面向自己回顾**，v1 不做对上汇报模板

待定（先按括号内默认执行）：

1. 会议纪要含客户名称时，默认归 `work.confidential`（保守），可按文件覆盖
2. 亮点是否同步到个人档案履历：默认**不同步**，需要时显式操作
3. 是否支持 `.doc` / `.xls` / `.ppt` 旧格式：默认不支持，提示先转换
4. 单文件大小 / 页数 / OCR 上限：默认单文件 50MB、500 页，OCR 默认关闭
