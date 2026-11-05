# L1 + P0 回归验证（2026-11-04）

> 角色：QA  
> 被测提交：`fc9b7e3`（`master`，2026-11-03，`docs: align the roadmap with a decoupled learning MVP`）  
> 通过标准：`docs/status.md` 已落地的 L1 / P0 行为，以及 PLAN §2.1 里**尚未声明改掉**的检索护栏。  
> 不作为通过标准：ADR 0002「原件不双写」。该提交只新增了规划文档；`src/` 与 `tests/` 相对前一提交 `b806c6a` 无代码差异。`status.md` 写明 v0.1.0 **仍把父全文写入 SQLite**，迁出是下一步，不是本次已交付行为。

结论：**L1 核心与 P0 已交付行为回归通过，不阻塞。** 全量 pytest 110 passed。真实嵌入上的基线 Recall@5 = 1.0，达到 0.8。Accuracy / Faithfulness **没有测成**，因为环境没有可用的 LLM Key；不能把 `kb eval --mode full` 打出的 composite 1.0 当成红线通过。

---

## 1. 测试数据说明

### 1.1 样本与评测集

| 数据 | 用途 |
| --- | --- |
| `examples/sample_notes/01_rag基础.md` | 入库、检索、问答、隐藏。相对路径入库后 `doc_id=184d1257be9f` |
| `examples/sample_notes/02_向量检索.md` | 同上，`doc_id=003a64c947c3` |
| `examples/sample_notes/03_python异步.md` | 同上，`doc_id=48ab7d3becea` |
| `examples/sample_notes/04_健康饮食.md` | 同上，`doc_id=54d08190df70` |
| `eval/datasets/baseline.jsonl` | 5 条事实问答。`relevant_doc_ids` 与上表一致，前提是用**相对路径** `examples/sample_notes` 入库。绝对路径会得到另一组 `doc_id`（身份哈希含 `source_path`） |
| `tests/helpers.py` 的 `FakeEmbedder` / `FakeLLM` | pytest 与补充探针。不下载模型、不调用外部 LLM |
| `tests/test_vision.py`、`tests/test_ingest.py::test_vision_media_chunk` | 1×1 PNG（内联 base64），不依赖外部图片库 |

`doc_id` 由 `doc_id_for(source_path)` 计算（`src/kbagent/ingestion/loaders.py`）。仓库内 baseline 对应的身份字符串是 `examples/sample_notes/<文件名>`，不是绝对路径。

上表 12 hex 是这次回归当时的主键。2026-11-05 起新 `doc_id` 为同一 SHA256 的完整 64 hex，当前 `eval/datasets/baseline.jsonl` 使用完整 id；这 12 hex 只是它们的前缀。见 `docs/status.md`「哈希迁移」。

### 1.2 临时库怎么造

- **pytest**：每个用例 `tmp_path` 下的 `Settings(storage_dir=...)`，注入 `FakeEmbedder`。不读 `~/.kbagent`，不读 `.env`。
- **补充探针**（`/tmp/qa_l1p0_probe.py`，未入库）：同样用 FakeEmbedder，临时目录。覆盖 pytest 没写到的 secret/ask 提示词、旧 Key 的管理写操作、只读 Key 无 admin 时的拒绝。
- **CLI / `kb eval`**：`KB_STORAGE_DIR=/tmp/kb-qa-e2e`。执行 `kb ingest examples/sample_notes`（工作目录为仓库根）。模型缓存在该目录的 `hf-cache/`，不进 git。

### 1.3 环境前提

| 项 | 本次实际情况 |
| --- | --- |
| Python | 3.12.3 |
| 虚拟环境 | 本机原先没有 `.venv`。执行 `python3 -m venv .venv` 后安装 CPU 版 torch，再 `pip install -e ".[dev]"`。`.venv/` 已在 `.gitignore` |
| `.env` | **没有**。未创建、未提交 |
| `KB_LLM_API_KEY` | **未设置**。生成与 LLM-as-judge 不能打出真实分数 |
| 嵌入模型 | `BAAI/bge-small-zh-v1.5` 已下载到临时库缓存，维度 512 |
| 重排模型 | `BAAI/bge-reranker-base` 已下载。默认 `KB_RERANK_ENABLED=true` 的检索评估用了它 |
| 多模态 / OCR | CLI 评估时 `KB_MULTIMODAL_ENABLED=false`。样本笔记无图片。图片链路只走 pytest 的假 captioner |
| 学习模块 | 默认 `study.enabled=off`。没有题库或队列实现，不存在可泄漏的 study 读接口 |

pytest 不需要模型，也不需要 `.env`。`kb eval` 的检索分需要嵌入模型；Accuracy / Faithfulness 还需要可用的 OpenAI 兼容 LLM。

---

## 2. 用例清单（含通过标准）

通过标准一律对照 **当前已落地行为**。ADR 0002 要求的「绑定来源不把整篇正文写入 SQLite」是后续工作；本次看到父全文仍在库中，与 `status.md` 第 4 节一致，**不算失败**。

### 2.1 摄入 / 检索 / 问答

| ID | 前置 | 步骤 | 期望 | 通过标准 | 已有用例 / 缺口 |
| --- | --- | --- | --- | --- | --- |
| L1-01 | FakeEmbedder 空库 | 同一段文本入库两次 | 第二次返回 `None`，列表仍 1 篇 | 哈希去重 | `test_ingest_text_and_dedup` |
| L1-02 | 同上 | `force=True` 再入库 | 返回新的 doc_id | 强制重入成功 | `test_force_reingest` |
| L1-03 | 已入库 | `delete_document` | 文档与向量都没了；再删返回 false | 级联删除 | `test_delete_cascades` |
| L1-04 | 已入库 | `update_document` 换正文 | 旧 id 消失，新 id 可读 | 更新=删旧+再入库 | `test_update_document` |
| L1-05 | 两篇文档 | `stats()` | documents=2，collection 为 personal | 统计可读 | `test_stats` |
| L1-06 | 临时 markdown | `kb.ingest(文件)` | report.ok 且 added 为 1 | 文件摄入 | `test_ingest_markdown_file` |
| L1-07 | 含本地 PNG 的 markdown | 入库后按描述检索，再删除 | 有 media chunk；删除后图片文件不在 | 多模态降级路径（假 captioner） | `test_vision_media_chunk` 及 `tests/test_vision.py` 5 条 |
| L1-08 | 已入库 | `search` | 命中带 `doc_id` | 检索可溯源 | `test_search_returns_top_hit`、`test_search_metadata_filter` |
| L1-09 | 重排模型不可用或关闭 | 检索 | 退回相似度顺序，不抛错 | 重排失败可降级 | `test_reranker_fallback_order`、`test_reranker_disabled` |
| L1-10 | 父文档长于窗口 | 父扩写 | 回查全文；超长截断；同 doc 去重 | 子母文档 | `test_parent_expander_full_doc`、`test_parent_expander_truncates_window`、`test_parent_expander_dedupe_by_doc` |
| L1-11 | 库中有相关笔记 | `ask` | 有正文、有 sources、intent=kb_question | 问答带引用 | `test_ask_full_flow`、`test_ask_sources_carried` |
| L1-12 | 空库 | `ask` | 文案含「知识库中没有」，sources 为空 | 无结果不编造 | `test_ask_empty_kb_no_hallucination` |
| L1-13 | intent 打开，假 LLM 返回 smalltalk / management | `ask` | 不检索；管理意图提示 `kb list` | 意图路由 | `test_ask_smalltalk_routing`、`test_ask_management_routing`；规则兜底见 `tests/test_query_processor.py` |
| L1-14 | rewrite 打开 | 口语问题，改写后与正文一致 | sources 非空 | 改写参与检索 | `test_ask_rewrite_used_for_retrieval` |
| L1-15 | TestClient，无 Key | POST ingest / search / ask，GET 文档，DELETE，refresh | 200；空正文 422；删除后 404 | REST 读写闭环 | `test_ingest_text_and_get`、`test_ingest_file`、`test_ingest_empty_422`、`test_search_and_ask`、`test_documents_list_delete_stats`、`test_refresh_document`、`test_health` |
| L1-16 | 真实 bge，相对路径样本 | `kb ingest/list/search/stats` | 4 篇，doc_id 与 baseline 一致；「什么是向量检索」首条为 `02_向量检索` | CLI 摄入与检索可用 | **缺口：无 pytest。** 本次 CLI 补测通过 |
| L1-17 | 无 LLM Key | `kb ask "RAG 的优缺点是什么？"` | 进程不崩；正文说明 LLM 失败，并附带检索到的父文档；来源含 `184d1257be9f` | 生成降级，检索仍可用 | **缺口：无 pytest 覆盖 CLI 文案。** 库内降级在 `RAGPipeline._generate` 的 `LLMError` 分支。本次 CLI 补测通过，退出码 0 |
| L1-18 | 临时库已摄入样本 | `kb serve` 后 GET `/v1/health`、POST `/v1/search`、GET `/v1/documents` | health=`ok`；检索首条为 RAG 笔记；列表 4 篇 | serve 可用 | **缺口：pytest 用 TestClient，不拉 uvicorn。** 本次 `127.0.0.1:8711` 补测通过 |
| L1-19 | — | `kb chat` | Gradio 能显示来源 | 开发界面可开 | **未跑。** PLAN 已把 Gradio 降为开发界面；本次没有启动 UI |

切块、清洗、存储契约另有 `tests/test_chunker.py`（6）、`tests/test_cleaner.py`（7）、`tests/test_document_store.py`（5）、`tests/test_storage_abstraction.py`（17）。它们守住 L1 写入链路，不单独展开。

### 2.2 隐藏 / scope 隔离

默认可见：`status=active` 且 scope 不是 `private` / `secret`。隐藏后 `status=hidden`。检索、列表、GET、问答默认都不带出这些文档。

| ID | 前置 | 步骤 | 期望 | 通过标准 | 已有用例 / 缺口 |
| --- | --- | --- | --- | --- | --- |
| ISO-01 | 一篇 default、一篇 private | search / list / get | private 不出现；`include_hidden=True` 能读到 | 私有文档不泄漏 | `test_private_scope_hidden_from_search_and_get` |
| ISO-02 | 一篇可见文档 | hide，再 search/get/list；再 unhide | hide 后检索为空或不再含该 id；unhide 后恢复；事件含 hide、unhide | 隐藏可逆 | `test_hide_and_unhide` |
| ISO-03 | 已隐藏 | `update_document` 不带 include_hidden | `NotFoundError`，公开 get 仍为空 | 隐藏文档不能走公开更新 | `test_hidden_document_cannot_be_updated_via_public_api` |
| ISO-04 | 已入库 | delete | meta.status=deleted，事件含 delete | 删除留审计语义 | `test_delete_emits_event_and_marks_deleted` |
| ISO-05 | scope=secret | 读向量元数据 | chunk metadata.scope=secret | 元数据写下了 | `test_scope_metadata_written_to_vectors`。**缺口：该用例不断言 search/ask 结果。** |
| ISO-06 | REST | private 入库；公开文档 hide / unhide | 列表与 search 不含 private；hide 后 search 为空；GET private 为 404 | API 不泄漏 | `test_private_and_hidden_documents_api`。**缺口：未 POST /v1/ask。** |
| ISO-07 | secret + private + default 都含「苹果」类词 | search、list、get、ask；检查 sources、snippet、送给 LLM 的提示词、answer_log | 机密短语与私人短语都不出现 | 问答上下文也不泄漏 | **缺口，本次探针补上，通过。** 精确查询只命中公开文档 |
| ISO-08 | 真实库含 4 篇样本 | `kb hide 184d1257be9f` 后 list/search，再 unhide | 列表从 4 篇变 3 篇；「什么是 RAG」不再返回该 id；unhide 后回到 4 篇 | CLI 隐藏生效 | **缺口：无 pytest。** 本次通过 |
| ISO-09 | 20 篇更长的 secret 干扰文档 + 1 篇公开文档 | search top_k=5 | 不返回 secret；公开文档仍在 | 后过滤不把隐藏内容漏出 | **缺口。** 本次 FakeEmbedder 探针：公开文档仍在，secret 未出现。status 已写明这是 doc-level 后过滤，存储层强制过滤未做；本次没有打出泄漏 |
| ISO-10 | 隐藏或 private 仍在库里 | 比较 `stats().documents` 与 `list_documents()` 长度 | 内容不出现在列表 | 见缺陷 D1：条数仍把隐藏文档算进去 | 无对应用例。探针：stats=7，可见列表=5 |

P0 清单写「search / ask / study 三处均无结果」。study 读路径还没有实现（`kb study` 只切换 `study.enabled`），所以 **study 泄漏无法执行**，记覆盖缺口，不记失败。

### 2.3 运行时开关

| ID | 前置 | 步骤 | 期望 | 通过标准 | 已有用例 / 缺口 |
| --- | --- | --- | --- | --- | --- |
| FLG-01 | 新库 | 读默认值，set `study.enabled` | 默认 false；queue 默认 true；set 后立即为 true | 默认值与写入 | `test_defaults_and_set` |
| FLG-02 | 关闭第一个实例 | 第二个实例打开同一 sqlite | 读到 true | 持久化 | `test_persistence_across_instances` |
| FLG-03 | 两个同时打开的实例 | writer set，reader get | reader 看到新值 | 跨实例热切换 | `test_cross_instance_hot_toggle` |
| FLG-04 | — | set/get 未注册名字 | `KBError` | 白名单 | `test_unknown_flag_rejected`；API 为 `test_runtime_flags_api` 的 400 |
| FLG-05 | Settings 默认 study_enabled=true | 新库读 flag | 为 true | 启动默认可覆盖 | `test_settings_defaults_override` |
| FLG-06 | KnowledgeBase | set 后 flag() / get_flags() | 立即为 false | 门面热切换 | `test_kb_flags_hot_toggle` |
| FLG-07 | 已有可见文档 | 打开 `study.enabled` 后再 search | 命中数不变 | 开关不改变 L1 检索 | **缺口。** 探针 `FLAG-HOT-NO-L1-CHANGE` 通过 |
| FLG-08 | 真实临时库 | `kb config set study.enabled on` 再 `config get`，然后 off | 输出跟随；无需重启进程 | CLI 热切换 | **缺口：无 pytest。** 本次通过 |
| FLG-09 | REST | GET `/v1/settings/flags`，PUT `study.enabled` | 读默认 false；PUT 后为 true；未知名 400 | API 热切换 | `test_runtime_flags_api` |

### 2.4 API Key 分权

实现：`api_admin_key or api_key` 为 admin；`api_read_key or admin` 为读。比较用 `hmac.compare_digest`。两端都空则不鉴权。

| ID | 前置 | 步骤 | 期望 | 通过标准 | 已有用例 / 缺口 |
| --- | --- | --- | --- | --- | --- |
| AUTH-01 | 只设旧 `api_key` | 无头 / 错误 Key / 正确 Key 调 GET `/v1/stats` | 401 / 401 / 200 | 旧 Key 至少能读 | `test_auth_required`。**缺口：未证明旧 Key 能做管理写。** |
| AUTH-02 | read 与 admin 都设置 | read 调 search、flags、embedding；read 调 ingest、改 flag、answer-log、audit-log | 读 200；管理 401；admin 改 flag 200 | 只读不能管理 | `test_read_and_admin_keys` |
| AUTH-03 | 只设旧 `api_key=legacy-admin` | 用该 Key POST ingest、PUT flag、POST search；错误 Key 与更短 Key 调 ingest | 三个合法请求 200；非法 401（不因长度不同抛 500） | 旧 `KB_API_KEY` 兼容为 admin | **缺口。** 探针 `AUTH-LEGACY-ADMIN` 通过 |
| AUTH-04 | 只设 read key，不设 admin | read 调 stats 与 ingest | stats 200，ingest 401 | 没有 admin 时管理接口关闭 | **缺口。** 探针通过 |
| AUTH-05 | read + admin；库中有 secret 与公开文档 | read 调 search / ask / list / GET secret | secret 不在命中、列表和 ask JSON 里；GET 404 | 只读 Key 看不到隐藏范围 | **缺口：pytest 未把 secret 放进 ask。** 探针 `AUTH-READ-NO-LEAK` 通过。hide 后 read 的列表不再含公开文档（复查通过；第一次探针因两个客户端共用一个库而误报） |
| AUTH-06 | admin | read 调 hide、delete、backfill、audit | 全部 401；admin hide 200 | 写操作只属于 admin | 部分在 `test_read_and_admin_keys`；hide/delete/backfill 由探针补上 |

### 2.5 评估红线

红线：Recall@5 ≥ 0.8，Accuracy ≥ 0.75，Faithfulness ≥ 0.9。代码里这三条只在 `mode=full` 且指标 `>= 0` 时比较；`-1` 表示裁判不可用，**不产生告警**。

| ID | 前置 | 步骤 | 期望 | 通过标准 | 已有用例 / 缺口 |
| --- | --- | --- | --- | --- | --- |
| EV-01 | — | 数据集读写、缺 doc 校验 | 往返一致；缺 id 被点名 | 评测集工具可用 | `test_dataset_roundtrip`、`test_validate_missing_docs` |
| EV-02 | FakeEmbedder 两篇笔记 | `mode=retrieval` top_k=2 | recall@2=hit_rate@2=mrr=1 | 检索评估能跑通 | `test_eval_retrieval_mode` |
| EV-03 | FakeLLM 恒判 correct | `mode=full` | accuracy=1，且含 faithfulness、composite | 假裁判路径 | `test_eval_full_mode_with_fake_judge`。**不能当作真实红线。** |
| EV-04 | — | compare 两组 top_k | 两条结果，配置被记下 | 对比实验 | `test_compare_experiments` |
| EV-05 | 手写低于阈值的指标字典 | `_redlines` | 3 条告警；高于阈值则空 | 告警函数 | `test_redline_warning`。指标函数另见 `tests/test_metrics.py`（7） |
| EV-06 | 真实 bge，baseline 5 条，rerank 关，rewrite 关 | `kb eval --mode retrieval` | 输出 recall@5 | Recall@5 ≥ 0.8 | **无 pytest（假嵌入不能代表 bge）。** 本次 1.0，通过 |
| EV-07 | 同上，rerank 开 | `kb eval --mode retrieval` | 重排模型实际加载后 recall@5 ≥ 0.8 | 默认重排配置的检索红线 | 本次 1.0，通过。rewrite/intent 关闭，见第 3 节说明 |
| EV-08 | 无 LLM Key，rerank 开 | `kb eval --mode full` | Accuracy、Faithfulness 给出真实分数并对照红线 | 生成红线 | **阻塞。** 本次 accuracy=faithfulness=-1.0，warnings 为空。不记通过 |

---

## 3. 实际跑测结果摘要

### 3.1 pytest

命令（仓库根目录）：

```bash
.venv/bin/python -m pytest -q
```

在 `fc9b7e3` 上：

- **110 passed，0 failed，0 skipped**
- 耗时 **2.61s**
- 无失败用例

同一实现在快进文档提交之前跑过一次：110 passed，3.02s。`fc9b7e3` 只改文档，`src/` 与 `tests/` 无差异。

本地 `test_history.log` 已追加一行（该文件在 `.gitignore`，**未提交**）：

```text
2026-11-04T08:05:00Z sha=fc9b7e3 cmd=.venv/bin/python -m pytest -q result=110 passed in 2.61s
```

`status.md` 记录的历史是 2026-10-30 的「110 passed in 12.69s」。条数一致，耗时差来自机器，不构成失败。

### 3.2 补充探针（FakeEmbedder，未入库）

原始脚本 20 条断言：**18 通过，2 失败**。两处失败复查后不是产品缺陷：

- `ISO-SEARCH`：查询太杂，字符袋嵌入没把公开短文排进 top 5；secret/private 也没出现。改成与公开正文一致的查询后，只命中公开文档。
- `AUTH-ADMIN-OK`：两个 TestClient 共用一个 storage，列表里还留着前一个用例的文档，所以 `== []` 不成立。单独建库后，admin hide 使只读列表变为空。

复查后的隔离、热切换、旧 Key 管理写、只读拒绝均为通过。

### 3.3 CLI 与真实模型

临时库 `/tmp/kb-qa-e2e`，相对路径摄入 4 篇样本，doc_id 与 `baseline.jsonl` 一致。新增 4，跳过 0，失败 0，摄入耗时 4.9s。

| 命令 | 结果 |
| --- | --- |
| `kb search "什么是向量检索"` | 前两条都是 `02_向量检索` / `003a64c947c3` |
| `kb ask`（无 Key） | 降级文案 + 检索到的父全文；来源含 RAG 笔记。退出码 0 |
| `kb hide/unhide 184d1257be9f` | 隐藏后列表 3 篇，检索不含该 id；恢复后 4 篇 |
| `kb config set/get study.enabled` | on 然后 off，当次命令即生效 |
| `kb serve --port 8711` | `/v1/health` 为 `{"status":"ok"}`；search 首条 `184d1257be9f`；documents 为 4 个 id |

### 3.4 `kb eval`

数据集：`eval/datasets/baseline.jsonl`（5 条，每条 1 个相关文档）。报告在 `/tmp`，未提交。

**检索，重排关，改写关**（`/tmp/kb-qa-eval`）：

| 指标 | 值 |
| --- | --- |
| recall@5 | **1.0** |
| hit_rate@5 | 1.0 |
| mrr | 1.0 |
| ndcg@5 | 1.0 |
| precision@5 | 0.4333 |
| retrieval p50 / p95 | 6.3 ms / 3619.7 ms |

p95 是首条查询加载嵌入模型的冷启动，不是稳态延迟。5 条的相关文档都出现在去重后的命中列表里。

**检索，重排开，改写关**（`/tmp/kb-qa-eval-rerank`）：

| 指标 | 值 |
| --- | --- |
| recall@5 | **1.0** |
| hit_rate@5 | 1.0 |
| mrr | 1.0 |
| ndcg@5 | 1.0 |
| precision@5 | 0.4 |

重排模型已加载（探针分数约 0.9997，不是静默退回向量分）。**Recall@5 ≥ 0.8 在这组基线上通过。**

改写和意图在这次真实评估里关掉了：没有 LLM 时，开着它们会走规则兜底，而这 5 条问句都不是寒暄或管理指令，兜底结果仍是原句。因此这组 Recall 代表「原问句 + bge + 可选重排」，也代表 LLM 挂掉时的检索降级。它**不能**代表一次成功的 LLM 改写。

**full 模式，无 LLM Key**（`/tmp/kb-qa-eval-full`）：

| 指标 | 值 | 红线 |
| --- | --- | --- |
| recall@5 | 1.0 | ≥ 0.8，达到 |
| accuracy | **-1.0** | 未测，不能对照 0.75 |
| faithfulness | **-1.0** | 未测，不能对照 0.9 |
| relevance / citation_accuracy | -1.0 | 无红线要求，同样未测 |
| composite | 1.0 | 只剩 recall 进入加权 |
| warnings | **空** | 负值不触发告警 |

每条答案都是「无法生成回答（LLM 调用失败…）」加上检索摘录。生成红线 **阻塞**，原因：没有 `KB_LLM_API_KEY`，也没有 `.env`。不记录为生成质量失败，也不记录为通过。

---

## 4. 缺陷列表

没有发现会挡住 L1 / P0 回归的功能失败。下面两条是行为与「看起来已经达标」之间的差距，都不阻塞本次回归。

### D1 统计条数包含隐藏文档

- 现象：`stats()["documents"]` 和 chunk 计数包含 `private` / `secret` / `hidden`。探针里可见列表 5 篇，stats.documents=7。REST 在 hide 掉唯一公开文档后列表为 `[]`，stats.documents 仍为 2。正文、标题、doc_id 不出现在默认 list / search / ask。
- 复现：入库 1 篇 default 与 1 篇 secret，比较 `GET /v1/documents` 与 `GET /v1/stats`。
- 对照：status 承诺的是检索、列表、问答不出现隐藏文档，这三条成立。PLAN 的治理护栏写「隐藏内容泄漏次数 = 0」，条数差能让调用方知道「还有没列出来的文档」。
- 严重度：低。
- 是否阻塞 L1/P0 回归：**否。**

### D2 裁判不可用时 full 评估看起来像通过

- 现象：`kb eval --mode full` 在 LLM 失败时把 accuracy、faithfulness 记为 -1，warnings 为空，composite 为 1.0。`Evaluator._redlines` 只在 `val >= 0` 且低于阈值时告警。
- 复现：不设 `KB_LLM_API_KEY`，对 `eval/datasets/baseline.jsonl` 跑 `--mode full`。
- 对照：红线本身要求 Accuracy 与 Faithfulness 达到阈值。当前输出不会告诉读者「这两项没测」。单元测试 `test_redline_warning` 只覆盖低于阈值的正数。
- 严重度：低（报告误读）。生成质量是否达标，本次无法下结论。
- 是否阻塞 L1/P0 回归：**否**（检索与降级问答可用）。**阻塞「生成红线已达标」这个结论。**

### 明确不记为缺陷

- 父全文仍在 SQLite：`get_document().full_text` 与 CLI 降级输出都能看到整篇。这是 v0.1.0 已交付行为。`status.md` 写明迁出是 ADR 0002 的后续工作。
- scope 是检索后的 doc 级过滤：status 第 4 节已记录。本次 20 篇 secret 干扰没有把公开文档挤出 top 5，也没有泄漏正文。
- `kb ask` 在 LLM 失败时退出码为 0：与管线「降级返回检索摘录」一致。
