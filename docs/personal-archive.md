# 个人档案管理规划：时间线、回顾与本地健康

> 状态：**规划文档**，尚未实现。与 [PLAN v4](../PLAN.md)、[愿景](./vision.md) 配套。
> 架构支持度与前置优化见 [架构评估](./architecture-readiness-personal.md)。
> 共用同一记录底座的另一个域见 [工作知识管理](./work-knowledge-management.md)。
> 两个已确定的约束：
> 1. **用途是给自己回看**——不是给 AI 建人格画像，也不以聊天个性化为主入口。
> 2. **健康数据只留在本地**——不出机器、不进云端 LLM、不进向量库。

---

## 1. 定位

知识库回答"世界上有什么"；个人档案回答"我发生过什么、我这一段时间过得怎么样"。

两者语义不同：

| | 知识库 | 个人档案 |
| --- | --- | --- |
| 权威来源 | 文档 | **本人** |
| 核心问题 | 检索与问答 | 时间线、趋势、回顾 |
| 数据形态 | 叙事文本 | 事实 / 事件 / 指标 / 叙事混合 |
| 冲突处理 | 新文档取代旧文档 | 本人确认，或新记录取代旧记录 |
| 主要界面 | 搜索、聊天 | 时间线、周/月/年回顾、趋势图 |

因此档案不是"另一类笔记"，需要独立的数据模型与入口，但**复用**现有的 `scope` 治理、文档库、事件与运行时开关。

### 1.1 目标

- 把履历、人生事件、健康记录、日记/复盘统一到一个可回看的时间轴上
- 按周/月/年生成回顾报告，数字由代码计算，叙述才交给模型
- 健康数据全程本地处理，默认不可被普通检索或云端模型触达
- 源文件（日记、报告原件）不被系统改写

### 1.2 非目标

- 人格画像、心理诊断、医疗建议
- 云端同步、多人共享、社交功能
- 图数据库、完整本体
- 自动改写或合并日记
- 把健康指标向量化

---

## 2. 数据分类：四类，四种处理方式

| 类别 | 例子 | 主存 | 检索 / 使用方式 |
| --- | --- | --- | --- |
| **事实型** | 教育、职业、居住地、家庭、价值观自述 | `personal.sqlite3` / `facts` | 时点（as-of）与条件查询 |
| **事件型** | 入职、离职、搬家、重大决定、就诊、手术 | `events` | 时间范围、时间线聚合 |
| **指标型** | 体重、血压、睡眠、化验值、运动 | 健康类进 `health.sqlite3`，其余 `metrics` | SQL 聚合、趋势；**不 embedding** |
| **叙事型** | 日记、复盘、想法、思维笔记 | 现有 documents + Chroma | 现有向量检索（带 scope 过滤） |

硬规则：

1. **只有叙事型进向量库**。把数值压成 embedding 后，趋势和阈值查询都会失真。
2. **健康数据的数值、事件、报告正文都不进 Chroma**，也不进 `documents.sqlite3`。
3. 每类记录都带 `source_doc_id` 指回原始材料，保证可溯源。

---

## 3. 时间性

档案里没有"当前值"，只有"截至某时刻的值"。

- 事实采用**版本化而非覆盖**：`valid_from` / `valid_to`，新值插入并关闭旧值。
- 每条记录区分 `observed_at`（发生时间）与 `recorded_at`（记录时间）。
- 必须支持 as-of 查询："2019 年时我的工作"、"上次体检时的指标"。
- 事件可以有起止时间与"进行中"状态（当前工作、当前居住地）。
- 回顾报告只读取时间窗口内的数据，窗口外的数据不参与。

复用现有治理的 `valid_until` / `supersedes` 思路（[知识治理](./knowledge-governance.md)），把粒度从"文档"降到"事实"。

---

## 4. 存储模型

### 4.1 `personal.sqlite3`（非健康档案）

```sql
CREATE TABLE facts (
  id            TEXT PRIMARY KEY,
  category      TEXT NOT NULL,        -- identity | education | career | family | values | ...
  key           TEXT NOT NULL,        -- job_title / city / degree ...
  value         TEXT NOT NULL,
  valid_from    TEXT,
  valid_to      TEXT,                 -- NULL 表示当前有效
  observed_at   TEXT,
  recorded_at   TEXT NOT NULL,
  source_doc_id TEXT,
  confidence    REAL DEFAULT 1.0,
  scope         TEXT NOT NULL DEFAULT 'personal'
);

CREATE TABLE events (
  id            TEXT PRIMARY KEY,
  type          TEXT NOT NULL,        -- work | move | health | decision | milestone | ...
  title         TEXT NOT NULL,
  description   TEXT,
  ts_start      TEXT NOT NULL,
  ts_end        TEXT,
  place         TEXT,
  people        TEXT,                 -- JSON 数组
  source_doc_id TEXT,
  recorded_at   TEXT NOT NULL,
  scope         TEXT NOT NULL DEFAULT 'personal'
);

CREATE TABLE metrics (
  id            TEXT PRIMARY KEY,
  metric        TEXT NOT NULL,        -- weight / sleep_hours / resting_hr ...
  ts            TEXT NOT NULL,
  value         REAL NOT NULL,
  unit          TEXT,
  context       TEXT,
  source_doc_id TEXT,
  recorded_at   TEXT NOT NULL
);

CREATE INDEX idx_facts_key     ON facts(key, valid_from);
CREATE INDEX idx_events_ts     ON events(ts_start);
CREATE INDEX idx_metrics_ts    ON metrics(metric, ts);
```

### 4.2 `health.sqlite3`（独立库，加密，纯本地）

```sql
CREATE TABLE health_metrics (
  id            TEXT PRIMARY KEY,
  metric        TEXT NOT NULL,        -- blood_pressure / ldl / weight / sleep ...
  ts            TEXT NOT NULL,
  value         REAL,                 -- 标量指标
  value_text    TEXT,                 -- 复合值：120/80、阴性/阳性
  unit          TEXT,
  context       TEXT,
  source_doc_id TEXT,
  recorded_at   TEXT NOT NULL
);

CREATE TABLE health_events (
  id            TEXT PRIMARY KEY,
  type          TEXT NOT NULL,        -- visit | diagnosis | medication | surgery | symptom
  title         TEXT NOT NULL,
  note          TEXT,
  ts_start      TEXT NOT NULL,
  ts_end        TEXT,
  source_doc_id TEXT
);

CREATE TABLE health_docs (
  id            TEXT PRIMARY KEY,
  path          TEXT NOT NULL,        -- 加密存储的原件路径
  sha256        TEXT NOT NULL,
  doc_type      TEXT,                 -- report | prescription | imaging ...
  extracted_text TEXT,                -- 本地解析结果，仍在本库
  recorded_at   TEXT NOT NULL
);
```

**为什么单独一个库**：

- 加密与删除的边界清晰
- 从代码层面杜绝被误传入通用摄入 / 向量化流程
- 可独立备份、独立销毁

### 4.3 加密

MVP 先用文件权限 `0600` + 独立目录；随后二选一：

- **SQLCipher**（整库加密，推荐）：`sqlcipher3` 作为可选 extra，密钥来自环境变量或本地 key 文件
- **字段级 Fernet**：`cryptography` 作为可选 extra，对 `value_text` / `note` / `extracted_text` 加密

不把密钥写入 `.env` 以外的日志、prompt、回答与审计记录。

---

## 5. 回看的产品形态

自用回看的主入口是**时间线与回顾报告**，不是聊天。

### 5.1 时间线

```bash
kb personal timeline --from 2019-01 --to 2026-10
kb personal timeline --type work
kb personal fact job_title --as-of 2019-06-01
```

按"章节"聚合人生阶段（教育 / 职业 / 城市），章节是**派生视图**，不作为源数据。

### 5.2 周期性回顾

```bash
kb review week  --period 2026-W43
kb review month --period 2026-10
kb review year  --period 2026
```

报告结构固定，先生成 Markdown 到 `~/.kbagent/reviews/`，默认不回灌知识库（避免自我引用污染）：

| 章节 | 内容 | 生成方式 |
| --- | --- | --- |
| 本期事件 | 时间窗口内的事件 | SQL |
| 健康 | 指标变化、就诊事件 | **本地计算 + 模板句** |
| 新记录 | 新增日记/笔记 | 文档库 |
| 主题 | 反复出现的词与话题 | 本地关键词统计优先 |
| 变化 | 权重/事实的更替 | 版本化事实 diff |
| 待整理 | 孤儿、重复、过期、未回顾 | 治理数据 |
| 自问 | 给未来的自己的 3 个问题 | 模板或本地模型 |

原则：**数字由代码算，模型只负责组织语言**。健康章节默认只用模板，不调用任何模型。

### 5.3 趋势

```bash
kb health trend blood_pressure --since 2023-01
kb health trend ldl --since 2024 --chart
```

趋势、均值、变化率全部在本地计算。输出可以直接看，也可以导出。

### 5.4 检索边界

- 普通 `kb search` / `kb ask` 默认**看不到** `personal` / `health` 内容。
- 显式输入 `kb personal ...` / `kb review ...` 才进入档案入口。
- 将来若接入聊天，档案查询是白名单工具，且健康数据仍强制本地模型。

---

## 6. 本地健康的技术保证

"只留本地"必须是**可测试的机制**，不是一句承诺。

### 6.1 策略

| 数据 | 云端 LLM | 本地 LLM | 代码计算 |
| --- | --- | --- | --- |
| 健康指标 / 事件 / 报告 | **禁止** | 允许 | 趋势、聚合 |
| 非健康个人事实 / 事件 | 允许（可脱敏） | 允许 | 查询、聚合 |
| 日记 / 复盘 | 允许（可配） | 允许 | — |
| 回顾报告的健康章节 | **禁止** | 允许 | 默认模板 |

### 6.2 强制机制

```python
LOCAL_ONLY_SCOPES = frozenset({"health", "health_private"})

def resolve_llm(scope: str) -> LLMClient | None:
    if scope in LOCAL_ONLY_SCOPES:
        if not settings.local_llm_configured:
            return None          # 调用方退回模板，绝不降级到云端
        return LocalLLM(...)     # base_url 必须是 loopback
    return cloud_llm
```

补充规则：

- 本地模型地址必须是 `127.0.0.1` / `localhost` / `::1`，否则启动时报错。
- `KB_HEALTH_LOCAL_ONLY=true` **不可通过运行时开关关闭**；只允许改环境变量并重启，变更写入审计。
- 健康文档不走云端视觉模型描述；PDF 解析与 OCR 使用本地库。
- 测试中用 mock 断言：任何健康路径对云端客户端的调用次数为 **0**。

---

## 7. 与现有代码的映射

新增可选模块 `src/kbagent/personal/`，与 `study` 同构：单向依赖核心、默认关闭、失败不阻塞主链路。

```
kbagent/personal/
  models.py        # Fact / Event / Metric / HealthRecord
  store.py         # PersonalStore Protocol + SQLite 实现
  health_store.py  # 独立加密库
  importer.py      # CSV / JSON / YAML / Markdown front-matter
  timeline.py      # 时间线、章节视图
  review.py        # 周/月/年回顾
  policy.py        # LOCAL_ONLY_SCOPES 与 LLM 路由校验
  cli.py
```

复用：

- `capability/governance`：`scope`、`doc_events`、`audit_log`、`runtime_flags`
- 文档库与 Chroma：仅用于叙事型日记
- 存储抽象：新增 `PersonalStore` Protocol，延续 [ADR 0001](./adr/0001-storage.md)

新增配置：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `KB_PERSONAL_ENABLED` | false | 档案模块总开关（运行时以 flag 为准） |
| `KB_PERSONAL_DB` | `~/.kbagent/personal.sqlite3` | 非健康档案库 |
| `KB_HEALTH_ENABLED` | false | 健康子模块开关 |
| `KB_HEALTH_DB` | `~/.kbagent/health.sqlite3` | 独立健康库 |
| `KB_HEALTH_LOCAL_ONLY` | true | 不可运行时关闭 |
| `KB_LOCAL_LLM_BASE_URL` | `http://127.0.0.1:11434/v1` | 本地模型（Ollama 等） |
| `KB_LOCAL_LLM_MODEL` | 空 | 未配置时健康章节只用模板 |
| `KB_PERSONAL_ENCRYPTION` | off | off / sqlcipher / fernet |

导入方式：

```bash
kb personal import facts.csv
kb personal import events.yaml
kb personal import metrics.csv
kb personal import-notes ~/journal/        # 复用现有摄入链路
kb health import labs.csv
kb health import-report 2023-05-体检.pdf   # 本地解析，原件加密留存
```

---

## 8. 阶段计划

| 阶段 | 内容 | 人日 | 验收 |
| --- | --- | --- | --- |
| **A. 存储与查询** | `PersonalStore`、四类导入、时间线、as-of 查询、CLI、测试；**不接 LLM** | 2–3 | 随机 10 条事实 as-of 正确；源文件零修改 |
| **B. 回顾报告** | 周/月/年模板报告、章节视图、数字一致性测试 | 3–5 | 报告数字与源数据 100% 一致 |
| **C. 本地健康** | 独立库、加密、导入、趋势、本地模型路由与强制测试 | 3–4 | 云端调用次数 = 0；趋势与原始数据一致 |
| **D. 打磨** | 本地模型摘要、自问问题、导出/删除、备份 | 3–5 | 删除可级联、导出完整、审计可查 |

顺序说明：**C 的本地强制机制不能晚于任何一次真实健康数据接入**。若先接数据后加限制，等于数据已经出过境。因此若健康数据是首要需求，可把 C 提前到 A 之后立即做。

---

## 9. 验收标准

- [ ] `KB_PERSONAL_ENABLED=false` 时，行为与当前版本完全一致
- [ ] 事实的 as-of 查询正确，历史值可追溯
- [ ] 回顾报告中的每个数字都能点回源记录
- [ ] 健康路径对云端 LLM / 云端视觉的调用次数为 0（自动化测试）
- [ ] 健康库与档案库不在任何普通检索结果中出现
- [ ] 按 owner / 类别删除与导出可用，且写审计
- [ ] 源文件哈希不变
- [ ] 备份恢复后，回顾报告可重建

---

## 10. 风险与反模式

| 风险 | 应对 |
| --- | --- |
| 把档案当知识库，全部向量化 | 只有叙事型进向量库；数值走结构化查询 |
| 健康数据"临时"发给云端做摘要 | 代码级 LOCAL_ONLY 路由 + 测试断言 + 无云端降级 |
| 回顾报告数字被模型编造 | 数字代码算，模型只写叙述；健康章节模板化 |
| 事实更新丢历史 | 版本化插入，不原地 UPDATE |
| 健康库与普通库混用 | 独立文件、独立连接、独立删除 |
| 隐私承诺无验证 | 审计 + 出口测试 + 禁网测试 |
| 记录负担过重 | 导入优先、能自动就不手填、周回顾 10 分钟内完成 |

明确不做：人格画像、心理诊断、医疗建议、自动改写日记、云端同步、健康数据向量化。

---

## 11. 开放问题

1. 健康原件是否需要本地 OCR？（本地 PaddleOCR 可做，扫描件才需要）
2. 加密选 SQLCipher 整库，还是字段级 Fernet？
3. 回顾频率：周 + 月，还是只做月 + 年？
4. 非健康个人数据是否允许云端 LLM 参与叙述？
5. 是否需要从日历 / 手环导入事件与指标？（先 CSV，不接第三方 API）
