# 项目进度

> 更新时间：2026-11-10
> 当前状态：**v0.1.0（L1 已交付）+ P0 完成 + 原件不双写已落地 + P1 可信与治理已落地**
> 配套：[PLAN v4](../PLAN.md) · [架构设计](./architecture.md) · [知识治理](./knowledge-governance.md)

---

## 1. 已完成

### L1 检索助手（v0.1.0）

- 摄入：loaders → cleaner → chunker → 索引；哈希去重、更新/删除
- 检索：意图识别 → 查询改写 → 向量检索 → 重排 → 父文档扩写
- 生成：编号引用、多模态图片描述、降级策略
- 接口：CLI / Gradio / FastAPI / Python API
- 评估：Recall@k / Accuracy / Faithfulness / 对比实验
- 测试：原有 71 个基线测试全部保持

### P0（2026-10-30 完成）

- **存储抽象**：`VectorStore` / `DocumentStore` Protocol、中立 Filter、工厂、契约测试
- **运行时开关**：`runtime_flags` + `FeatureFlags`，支持热切换与跨进程刷新
- **治理骨架**：`docs_meta` / `doc_events` / `consumer_cursor`
- **scope 隔离**：`default` / `private` / `secret`；隐藏文档默认不出现在检索、列表、问答
- **隐藏/恢复**：`kb hide` / `kb unhide`，REST 对应端点
- **答案日志**：`answer_log`，记录 query / 检索结果 / 答案 / 引用 / 延迟
- **审计日志**：`audit_log`，记录 hide / unhide / delete / flag 变更 / backfill
- **向量版本**：`embedding_manifest` + `runtime_pointer`（`active_embedding`）
- **API 鉴权**：只读 Key / admin Key 分权，常量时间比较；旧 `KB_API_KEY` 兼容为 admin
- **旧文档回填**：`kb governance backfill`。P1 起同时把 scope/status 写回已有切块

### P1 可信与治理（2026-11-10）

- **闸 1 可答性**：重排之后、生成之前。`top1 < τ_low` 直接拒答且不调用生成；中间带仍作答，但置信度降为 low，并提示「依据较弱」；达到 `τ_high` 正常作答。拒答是正常答案，写入 `answer_log.refusal_reason` / `confidence`
- **τ 要重标**：向量默认 `KB_ANSWER_TAU_LOW` / `KB_ANSWER_TAU_HIGH` = **0.52 / 0.54**。这是 2026-11-10 在 `examples/sample_notes` 上、用 `BAAI/bge-small-zh-v1.5`、L2 经 `1/(1+d)` 量出来的：库外 32 题 top1 最高 0.4959，基线五题最低 0.5440。重排默认 `KB_ANSWER_RERANK_TAU_LOW` / `KB_ANSWER_RERANK_TAU_HIGH` = **0.50 / 0.90**，尺子是 `BAAI/bge-reranker-base` 的 `predict`（sentence-transformers 里已经是 Sigmoid 概率；库外最高 0.3319，基线最低 0.9406）。概率不再做第二次 sigmoid。logit 模型只在重排器里做一次数值稳定的 sigmoid。换嵌入、度量、重排模型或语料后这些数都会失效，要用库外集和基线集重新看 top1 再改
- **闸 2 引用契约**：提示词只允许编号来源、事实句带 `[n]`、资料不足用固定句、末尾一行 `SELF_CHECK`。后检剥掉不在检索集合里的编号；全部编造或没有任何有效编号时改成固定拒答。不做闸 3 的陈述校验
- **库外拒答集**：`eval/datasets/out_of_kb.jsonl`（32 题，当前样例笔记覆盖不到）。`kb eval --mode refusal --dataset eval/datasets/out_of_kb.jsonl` 计算 `refusal_recall`。这个模式不调用裁判；没有 LLM key 时，低于 τ 的拒答仍然计入，检索分够高但生成失败的那条不算拒答
- **检索 fail-closed**：默认查询把 `status=active` 和 `scope` 排除 private/secret 写进向量 `where`。`retriever.search()` 不传这道过滤时由检索器补上；补不上就抛错，不会静默搜全库。`include_hidden=True` 才是旁路。文档行 / 治理元数据仍是第二道，隐藏近邻占满窗口时仍会扩大窗口
- **hide / unhide** 会把切块 `status` 写回去。`kb governance backfill` 给旧切块补 scope/status，否则缺这两项的旧切块不会被默认检索命中
- **级联**：`vector_index` 消费 `doc_events`（hide / unhide / delete / pending_cleanup），失败进 `consumer_dead_letter` 后跳过。`mentions` / `items` / `reports` / `concept_pages` / `queue` / `cache` 只有名字和游标，游标停在 0，没有做学习模块

### 原件不双写（2026-11-04，ADR 0002）

- **绑定 Vault / 文件夹**：SQLite 只存源路径、内容哈希、元数据、摘要、切块定位，不存整篇正文
- **向量库**：仍保存切块文本与嵌入（检索用派生副本）
- **父文档扩写**：按路径回读并重新清洗源文件，再按切块定位截取。先核对 `content_hash`，不一致则退回切块文本。`KB_MAX_PARENT_CHARS` 只限制返回窗口
- **inline**：没有外部原件的提交（含 API 文本和上传）正文留在应用库，来源标为 `inline`
- **已入库父全文**：`kb governance migrate-parents`，或 admin `POST /v1/governance/migrate-parents`。仅当源文件哈希与已入库哈希一致时删除库内全文并标为 `bound`；不一致则保留正文并标为 `diverged`（提示重新入库）。源文件不在则标为 `orphaned_copy`。定位从已有切块对齐，不按当前切块器重切

## 2. 当前验证

- 全量测试：**202 passed**（2026-11-10）。默认 τ 下库外集重排关、重排开的 `refusal_recall` 都是 1.0（样例笔记四篇，bge-small-zh-v1.5 / bge-reranker-base，无 LLM key）
- `STRUCTURE.md` 已补上源文件回读与父全文迁移相关文件
- 测试历史：本地 `test_history.log`（已 gitignore，不随仓库发布）

## 3. 主要提交

```
3a27c59 feat: complete remaining P0 observability and access controls
7f65f61 docs: mark P0 governance and runtime flags as implemented
7467cca feat: add governance metadata, events, and scope filtering
11bbead feat: refresh runtime flags across processes
003630c feat: add runtime feature flags and hot toggling
5b26e8d docs: add system architecture diagrams and data flows
```

## 4. 已知边界

- 默认检索的向量 `where` 要求切块带 `status=active`，且 `scope` 不是 private/secret。文档行和治理元数据仍是第二道。升级前缺这两项元数据的旧切块要跑一次 `kb governance backfill`
- 同路径再入库或替换时，如果调用方没有显式传入新的 scope，会保留原来的 scope 和 hidden / private 状态，不会洗成默认可检索。显式传入 scope 才会按新范围写入并把状态放回 active
- 多个进程共用一个 Chroma 目录时，另一个进程的 `kb repair` 或向量改写会让本进程已经加载的内存索引过期：`get` 能看到新记录，`query` 仍可能返回已删除的 id，默认检索因此变空。下一次检索会对比目录签名并重新打开客户端。跨进程改写之后，同一进程里随后的检索可能因为 mtime/fingerprint 继续漂移而每次都重新打开；结果仍然正确，本轮不处理。`kb repair` 的结果里有 `notice`，命令行也会打到 stderr：若检索仍然为空，重启那个进程。Chroma 不保证多个进程同时写入同一目录是安全的
- `answer_log` / `audit_log` 暂无保留期与轮转策略
- 学习扩展（概念、题库、队列、周报）尚未实现。按 PLAN §0.1，它是当前 MVP 主路径，仍然是独立模块：托盘默认开，库与组件默认关
- 父文档扩写会整篇回读并重新清洗源文件；`KB_MAX_PARENT_CHARS` 只限制返回窗口
- 绑定文件在入库后被修改时，扩写核对 `content_hash`，不一致则退回切块文本，不把漂移后的文件送进提示词；文件缺失同样退回切块文本。切块与源文件重新对齐要重新入库
- 对外 REST 默认去掉绝对 `source_path` / `image_path`（`KB_EXPOSE_SOURCE_PATHS` 默认关）。`GET /v1/documents/{doc_id}` 默认不含全文，`include_text=true` 才返回 inline 正文。用 Obsidian 或系统应用打开原文仍留在 §5 第 4 条
- `content_hash` 与新文档 `doc_id` 存完整 SHA256（64 hex）。展示可以只用前 12 hex（`hash_display`），去重和主键不用截断值。见下方「哈希迁移」
- `orphaned_copy` 表示源文件已经不在，库内全文留到用户处理。把文件放回且哈希一致后再跑 `migrate-parents`，会升为 `bound` 并删掉副本
- `diverged` 表示源文件还在，但和已入库哈希不一致（不用 `orphaned_copy`，因为文件没丢）。迁移保留正文，并在结果里提示重新入库。文件改回与哈希一致后再跑一次，才会升为 `bound` 并删掉副本
- 闸 3 声明校验、过期/冲突检测、近似去重尚未实现（P2）。近邻陷阱集、冲突集、过期集也还没有
- 同步通道、托盘 / 小窗、MCP、打开原文尚未实现。MVP 不做移动端
- 工作知识管理不是当前 MVP

## 4.1 哈希迁移（12 hex → 64 hex）

2026-11-05 之前，`content_hash` 和 `doc_id` 只存 SHA256 的前 12 hex。48 bit 碰撞时 `get_by_hash` 会取到一行并跳过另一篇入库。

升级后的行为：

- 新写入的 `content_hash`、`doc_id` 都是 64 hex。数据库不用改表，列本来就是文本。
- 已有行的 12 hex 主键**保持不变**，向量和引用不用重写。再次入库同一路径，或同一标题下全文仍能对上的 inline 文档，会沿用这条 `doc_id`，并把 `content_hash` 写成完整 SHA256。路径或全文对不上时不覆盖，新文档用完整 id。
- 去重只认完整哈希。多行共用一个哈希且全文不一致时不任取一行，也不会因此跳过新内容。
- 父文档扩写仍接受旧的 12 hex：它必须是当前文件完整哈希的前缀，否则当作漂移，退回切块文本。
- `kb governance migrate-parents` 会在源文件或库内正文对得上时，把 12 hex 升成 64 hex（结果里计为 `bound` 或 `hash_upgraded`）。升完后重复执行是 `unchanged`。
- 评测集 `eval/datasets/baseline.jsonl` 已改成完整 `doc_id`。2026-11-04 QA 记录里的 12 hex 是这些 id 的前缀，不是当前主键。

## 5. 下一步

近期顺序以本节为准。治理阶段的 P1、P2 与 [知识治理](./knowledge-governance.md) 同名。学习阶段叫 L2、S1–S4，见 [PLAN §6](../PLAN.md)。

1. **P2 严格模式**：声明校验（闸 3 / `KB_ANSWER_STRICT`）、过期/冲突检测、近似去重。P1 已落地的闸门、引用契约、库外拒答集和 fail-closed scope 不在这里重做。mentions / items / reports 仍等学习模块，不要为了级联清单提前造这些表
2. **学习切片**：硬编码几道题、作答、下次到期。不抽取概念，不等 L2。托盘或小窗可以在此起步。正式闪卡生成是 S1，排在第 5 条 L2 之后
3. **本机 MCP**，以及用 Obsidian 或系统默认应用打开原文（含对外响应去掉绝对路径）。不挡住 ADR 0002 合并
4. **同步通道预留**：许可模型、稳定 id、本机路径映射。不做移动端，也不做完整同步传输
5. **L2 知识结构**：概念、出处、消歧、概念页。做完再进入 S1 正式出题
6. **人生时间线前置**：健康和敏感范围只走本地模型或直接拒绝；这些问答不进明文日志
7. **人生时间线**：事实、事件、健康。工作模块不是当前 MVP

## 6. 恢复方式

```bash
# 运行回归
.venv/bin/python -m pytest -q

# 查看运行时开关
.venv/bin/kb config get

# 查看治理状态
.venv/bin/kb embedding
.venv/bin/kb events
.venv/bin/kb answer-log
.venv/bin/kb audit-log

# 旧库补充 docs_meta，并给旧切块补上 scope/status
.venv/bin/kb governance backfill

# 消化 doc_events（目前只有 vector_index；其余消费者列在 residual）
.venv/bin/kb governance drain

# 库外拒答：refusal_recall。不调用裁判
.venv/bin/kb eval --mode refusal --dataset eval/datasets/out_of_kb.jsonl

# 迁出已入库父全文（源文件缺失的记录标为 orphaned_copy）
# REST（admin Key）：POST /v1/governance/migrate-parents
.venv/bin/kb governance migrate-parents
```

架构与数据流见 [architecture.md](./architecture.md)；阶段与验收见 [PLAN v4](../PLAN.md) 与 [L1→L3 拆解](./mvp-l1-to-l3.md)。
