# 架构评估：现有 kb-agent 对个人档案扩展的支持度

> 状态：**评估文档**，对应 [个人档案管理规划](./personal-archive.md)。
> 结论先行：**存储与治理可以复用；读取链路、模型策略、scope 强制需要现在就补缝，否则后续会侵入核心。**
> 排期：检索 fail-closed 归入治理 P1（[status §5](./status.md) 第 2 条）。健康和敏感范围的模型出口、日志脱敏是人生时间线的前置（status 第 8 条），不插进原件不双写和治理 P1/P2 之前。本文自己的 P0/P1/P2 是档案前置清单，不是治理阶段编号。

---

## 1. 结论

个人档案需要第三种能力：**结构化 + 时间序列 + 本地私有数据**。现有系统是"文档 → 向量 → 检索 → 生成"的单一路径。

| 能力 | 现状 | 能否直接扩展 |
| --- | --- | --- |
| 新存储后端 | `DocumentStore` / `VectorStore` Protocol + 工厂 + 契约测试 | ✅ 照抄即可 |
| 结构化查询 | 后端中立 `Filter` + `compile_sql`，支持 `gt/gte/lt/lte` | ✅ 可直接复用于时间范围 |
| 运行时开关 | `FeatureFlags` 注册表 + 热切换 | ✅ 加名字即可 |
| 变更事件 / 审计 | `doc_events` / `consumer_cursor` / `audit_log` | ⚠️ 事件绑死 `doc_id` |
| scope 隔离 | `docs_meta.scope` + 检索后过滤 | ⚠️ 是"事后过滤"，不是强制策略 |
| 查询入口 | 固定 `search` → 向量，`ask` → 固定四种意图 | ❌ 没有路由到结构化查询的缝 |
| 模型调用 | 全局单一云端 `LLMClient`，注入到所有组件 | ❌ 没有按数据源的策略与出口管控 |
| 敏感数据摄入 | 图片描述默认走云端，且不区分 scope | ❌ 健康/私有图片会在入库时出境 |
| 日志 | `answer_log` 明文记录 query 与 answer | ❌ 健康文本会落到未加密库 |
| schema 演进 | 只有临时 `ALTER TABLE`，无迁移框架 | ⚠️ 加表前应补 |

**判断**：可以做成"独立模块 + 独立存储 + 独立入口"，但上述 4 个 ❌ 是横切关注点，晚改成本更高。

---

## 2. 已经可复用的扩展点（不要重造）

| 扩展点 | 位置 | 复用方式 |
| --- | --- | --- |
| Store Protocol + 工厂 | `src/kbagent/document_store/base.py:15`、`src/kbagent/storage/factory.py:24` | 新增 `PersonalStore` Protocol，不改 `DocumentStore` |
| 后端中立过滤 | `src/kbagent/storage/filters.py:131`、`:206` | 时间范围、类别、owner 查询直接用 `compile_sql` |
| 契约测试模式 | `tests/test_storage_abstraction.py` | 为 `PersonalStore` 写同样的契约测试 |
| 运行时开关 | `src/kbagent/flags.py:17` | 注册 `personal.enabled` / `health.enabled` |
| 治理元数据与审计 | `src/kbagent/governance/store.py:20`、`:60` | 复用 scope 语义与 `audit_log` |
| CLI 子命令 | `src/kbagent/cli.py:20-26` | `app.add_typer(personal_app, name="personal")` |
| 测试替身 | `tests/helpers.py` | `FakeLLM` 可扩展为"断言零云端调用" |
| 索引删除级联 | `src/kbagent/ingestion/indexer.py:233` | 复制到 `PersonalStore.delete_by_source` |

关键结论：**结构化数据不要塞进 `DocumentStore`**。它的契约是"父文档全文"，加事实/事件/指标会让契约膨胀。正确做法是并列一个新 Protocol，并让两边的删除/审计通过 `source_doc_id` 关联。

---

## 3. 不支持的地方（按风险排序）

### 3.1 健康数据目前无法做到"只留本地"

三个独立的泄漏点，任意一个都足以让"本地健康"失效：

1. **意图识别先于 scope 判定，且走云端**
   `RAGPipeline.ask` 在检索前调用 `QueryProcessor.process`（`src/kbagent/generation/pipeline.py:59-66`），后者直接 `llm.chat_json`（`src/kbagent/retrieval/query_processor.py:66`）。此时还不知道问题是健康类，**问题原文已经发给云端**。

2. **图片描述在入库时无条件走云端**
   `Indexer` 调 `VisionIndexer.process_document` 时没有传 scope（`src/kbagent/ingestion/indexer.py:154-161`），而 `VisionIndexer` 默认构造 `VisionAPICaptioner(llm)`（`src/kbagent/vision/indexer.py:29`），对每张图调用 `captioner.caption`（`src/kbagent/vision/indexer.py:61-65`）。**当前 `private` / `secret` 文档的图片同样会被发送到云端**，这不只是新功能的问题。

3. **问答日志明文落库**
   `KnowledgeBase.ask` 把 query 与 answer 写入 `answer_log`（`src/kbagent/kb.py:383-401`），表结构没有 `scope` 列（`src/kbagent/governance/store.py:45-58`），库文件是未加密的 `documents.sqlite3`。健康问答会在这里留下明文。

### 3.2 没有"非向量查询"的路由缝

- `KnowledgeBase.search` 永远"嵌入 → Chroma"（`src/kbagent/kb.py:321-357`）。
- `RAGPipeline.ask` 把四种意图的响应硬编码成字符串（`src/kbagent/generation/pipeline.py:74-88`），没有 handler 注册机制；新增 `personal.fact` / `personal.metric` 只能继续加 `if`。
- `QueryProcessor` 的意图白名单也硬编码（`src/kbagent/retrieval/query_processor.py:47`），新增意图要改 prompt 与校验两处。
- `Answer.sources` 是文档引用模型（`src/kbagent/models.py:108-119`），没有"记录引用"的位置；指标结果只能塞进 `meta`。

### 3.3 scope 是"事后过滤"，不是强制策略

- `Retriever.search` 先多取候选，再用 `doc_visibility` 回调过滤（`src/kbagent/retrieval/retriever.py:37-45`）。过滤逻辑分散在 facade、回调与调用方。
- `include_hidden` 是一个布尔后门（`src/kbagent/kb.py:329`、`src/kbagent/cli.py:100`），CLI 可绕过全部 scope；REST 目前没暴露，但模型本身没有"禁止越权"的概念。
- 调用方传入的 `where` 不会被强制合并 scope（`src/kbagent/api/schemas.py:10-15`、`src/kbagent/api/server.py:102-109`）。文档里"检索入口强制传 scope"仍只是骨架。
- 没有针对"未声明 scope 时必须排除敏感域"的失败即拒绝测试。

### 3.4 事件与迁移是文档专用

- `doc_events.doc_id` 是 `NOT NULL` 且消费者按文档 id 理解（`src/kbagent/governance/store.py:32-38`）。事实版本更替、健康记录删除无法作为一等事件表达。
- 没有迁移框架：`SQLiteDocumentStore` 用 `PRAGMA table_info` + `ALTER TABLE` 临时补列（`src/kbagent/document_store/sqlite_store.py:70-82`），其余表只有 `CREATE IF NOT EXISTS`。新表一多就会失控。

### 3.5 组合与并发

- `KnowledgeBase` 在一个构造函数里装配 12 个组件（`src/kbagent/kb.py:34-127`），`close` 逐个关闭（`:415-419`）。加档案模块会继续膨胀。
- `KnowledgeBase.search` 在无锁状态下临时修改共享 `settings.query_rewrite_enabled`（`src/kbagent/kb.py:336-343`）；`ask` 持锁，但管线内部同样修改共享 `Settings`（`src/kbagent/generation/pipeline.py:59-66`）。这是潜在并发 bug，也会阻碍"按请求携带策略"。
- API 鉴权是 `create_app` 内的闭包（`src/kbagent/api/server.py:79-98`），新模块路由无法复用。

### 3.6 命名冲突

`default_collection` 当前就叫 `personal`（`src/kbagent/config.py:48`）。档案叙事若也用 `collection="personal"` 会与现有默认集合语义重叠。建议保留 `personal` 作为现有默认集合不动，档案叙事使用 `journal`，健康永不上向量。

---

## 4. 现在应该做的优化

### P0：加个人档案之前必须完成（约 4–6 人日）

| # | 优化 | 具体动作 | 验收 |
| --- | --- | --- | --- |
| 1 | **查询路由缝** | 新增 `QueryHandler` Protocol 与路由器；把 `pipeline.py:74-88` 的四种硬编码意图迁成内置 handler；`QueryProcessor` 意图枚举改为可注册；`Answer` 增加 `route`，`SourceRef` 增加 `source_type` / `record_id`（默认值兼容） | 现有测试全绿，`ask` 行为不变；新增一个假 handler 可被路由 |
| 2 | **LLM 网关 + 数据源策略** | 新增 `LLMGateway`，带 `purpose` 与 `scope`；`LocalOnlyPolicy` 拒绝非 loopback；`QueryProcessor` 接受 scope，本地域用规则路由；`VisionIndexer` 接受 scope，敏感域禁用云端描述 | 本地域查询对云端客户端的调用次数 = 0（mock 断言） |
| 3 | **fail-closed scope 策略** | 引入 `ScopePolicy` / `RequestContext`，在 `search` / `ask` / store 读取的**单一入口**合并 scope；默认排除 `personal` / `health`；敏感 scope 入库走专用 sink 或拒绝；删除 `include_hidden` 的后门语义（改为显式 admin 上下文） | 不声明 scope 的检索永远看不到敏感域；健康图片入库不触发云端 |
| 4 | **通用变更事件 + 迁移框架** | `doc_events` 增加 `subject_type` / `subject_id`（迁移后 `doc_id` 可空）；新增 `PRAGMA user_version` 迁移器，所有 SQLite store 共用 | 旧库可平滑迁移；重复执行幂等 |
| 5 | **日志分级** | `answer_log` 增加 `scope`；`AnswerLogPolicy` 对敏感域跳过或脱敏；`audit_log` 只存 id 与动作，不存内容 | 健康问答后，未加密库中无健康文本 |
| 6 | **组合缝** | 抽出 `ServiceContext`（settings / stores / governance / flags / llm gateway），`KnowledgeBase` 与后续 `PersonalService` 共用；`KnowledgeBase.close` 统一关闭扩展 | 无行为变化；测试可构造并关闭一个空扩展 |
| 7 | **修并发隐患** | 去掉对共享 `Settings` 的临时修改，改为显式参数传递（与 #1 一起做） | 并发 `search` + `ask` 测试无串扰 |

### P1：开始写档案模块时做（约 1–2 人日）

| # | 优化 | 说明 |
| --- | --- | --- |
| 8 | SQLite 连接工厂 | 统一 `connect_sqlite(path, key=None)`，文件权限 `0600`；为 SQLCipher 留接口 |
| 9 | API 依赖提取 | 把 `auth_read` / `auth_admin` 移到 `api/deps.py`；模块路由可注册；健康端点强制 admin + loopback |
| 10 | 配置分组 | `PersonalSettings` / `LocalLLMSettings` 子模型，避免 `Settings` 继续膨胀 |
| 11 | 命名与集合 | 档案叙事用 `journal`，健康数据不进 Chroma；保留现有默认集合名不动 |
| 12 | 预留健康路由命名 | 现有 `/v1/health` 是服务存活检查，健康域统一放 `/v1/personal/health/*` |

### P2：功能落地后再做

- 备份 / 导出 / 级联删除服务（`PersonalStore.delete_by_source`）
- 后台 consumer 运行器（自动从日记抽取事件；按需生成可暂不需要）
- 档案评估指标（回顾数字一致性、健康路径零出境）
- 加密实现（SQLCipher 或 Fernet）与密钥管理

---

## 5. 明确不要做的事

- **不要扩展 `DocumentStore`** 去装事实/事件/指标——并列新 Protocol。
- **不要为插件而插件**：只需要"组合缝 + 路由注册"，不需要完整插件框架或动态加载。
- **不要让健康数据经过通用 `ask`**：即使加了策略，也优先用独立 `kb health` 入口，减少路径。
- **不要把 `include_hidden` 带进新 API**：敏感域只走显式入口。
- **不要先建图数据库**：回归报告用 SQL 聚合足够。

---

## 6. 架构护栏测试（现在就能加）

即使不开发功能，也可以先把契约固定下来：

1. **本地域零出境**：用 fake 云端客户端，断言 `scope=health` 的分类、生成、视觉路径调用次数为 0。
2. **fail-closed**：不带 `RequestContext` 的 `search` / `ask` 永远不返回 `personal` / `health` 记录。
3. **隐私入库**：`scope=private` 的图片文档在无本地视觉配置时，不得触发云端 caption。
4. **日志不落敏**：敏感域问答后，`documents.sqlite3` 中不存在原文/答案。
5. **迁移幂等**：对已有 v0.1.0 库连续跑两次迁移，schema 与数据不变。
6. **路由回归**：默认四种意图行为与当前完全一致。

---

## 7. 与实施阶段的关系

| 个人档案阶段 | 前置架构项 |
| --- | --- |
| A. 存储与查询 | P0 #1、#4、#6、#7 |
| C. 本地健康 | P0 #2、#3、#5；P1 #8、#9 |
| B. 回顾报告 | P0 #1、#3 |
| D. 打磨 | P2 备份/导出 |

也就是说：**#1–#7 不依赖档案功能，可以独立提交并回归验证**，属于对现有系统的加固，不会引入新功能面。
