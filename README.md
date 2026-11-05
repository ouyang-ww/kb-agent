# kb-agent — 个人知识库 RAG Agent

Python 实现的本地学习知识助手（包 `kbagent`，CLI `kb`），同时是可供 Grok、DeepSeek Harness 调用的知识库组件。

1. **学习闭环（当前 MVP 主路径）**：问答、闪卡 / 测验、复习与进度。实现为独立模块，不耦合进检索核心。托盘 / 小窗是产品界面，原文用 Obsidian 或系统默认应用打开
2. **知识库组件**：CLI + REST + MCP + Python API。学习模块可关；关闭后摄入、检索、问答仍然完整

核心能力：数据清洗、切块、Cross-Encoder 重排、意图识别与查询改写、评估体系（Recall@k / Accuracy / Faithfulness 等）、多模态（图片）RAG。

> 定位见 [PLAN v4 §0.1](PLAN.md)。学习模块解耦扩展；工作模块是未来扩展，不是当前 MVP。
> 绑定 Vault / 文件夹的原件不写入应用库，见 [ADR 0002](docs/adr/0002-no-original-duplication.md)。抽取与总结可选本地模型，否则默认 DeepSeek。
> 不执行 shell、HTTP、文件系统或任意代码。打开原文是本机固定动作。

## 快速开始

```bash
cd kb-agent
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 配置 LLM（任意 OpenAI 兼容服务，如 DeepSeek / Kimi / Qwen / Ollama）
cp .env.example .env        # 编辑 KB_LLM_API_KEY、KB_LLM_BASE_URL、KB_LLM_MODEL

# 入库（md/txt/pdf，自动去重）
.venv/bin/kb ingest examples/sample_notes

# 检索与问答
.venv/bin/kb search "什么是向量检索"
.venv/bin/kb ask "RAG 的优缺点是什么？"

# 服务与界面
.venv/bin/kb serve          # REST API → http://127.0.0.1:8710 (docs: /docs)
.venv/bin/kb chat           # Gradio，当前的开发界面 → http://127.0.0.1:7860
```

产品主界面按计划是托盘或小窗，尚未实现。Gradio 保留给开发和回归。

> 首次使用本地嵌入模型会下载约 100MB（`BAAI/bge-small-zh-v1.5`），重排模型约 1.1GB（`BAAI/bge-reranker-base`，可设 `KB_RERANK_ENABLED=false` 关闭）。国内网络可先 `export HF_ENDPOINT=https://hf-mirror.com`。

## 配置（.env，前缀 `KB_`）

| 项 | 默认 | 说明 |
| --- | --- | --- |
| KB_STORAGE_DIR | ~/.kbagent | 数据目录（chroma / sqlite / media / hf-cache） |
| KB_VECTOR_STORE / KB_DOC_STORE | chroma / sqlite | 存储后端（迁移策略见 [ADR 0001](docs/adr/0001-storage.md)） |
| KB_VECTOR_METRIC / KB_EMBEDDING_MANIFEST_ID / KB_CHUNKER_VERSION | l2 / 空 / c1 | 向量度量与版本标识（manifest 用于迁移/回滚） |
| KB_IGNORE_HTTP_PROXY | true | 忽略失效代理直连 LLM/HF（需要代理时设 false） |
| KB_EMBEDDING_MODEL | BAAI/bge-small-zh-v1.5 | 本地嵌入模型 |
| KB_CHUNK_SIZE / KB_CHUNK_OVERLAP | 800 / 100 | 切块参数 |
| KB_RERANK_ENABLED | true | Cross-Encoder 重排 |
| KB_RERANK_CANDIDATES / KB_TOP_K | 20 / 5 | 候选数 / 最终返回数 |
| KB_INTENT_ENABLED / KB_QUERY_REWRITE_ENABLED | true / true | 意图识别 / 查询改写 |
| KB_MAX_PARENT_CHARS | 4000 | 从源文件读入的父文档窗口上限 |
| KB_LLM_BASE_URL / KB_LLM_MODEL / KB_LLM_API_KEY | deepseek-chat | LLM（任意 OpenAI 兼容） |
| KB_MULTIMODAL_ENABLED | true | 图片提取 + 描述（视觉模型） |
| KB_VISION_LLM_MODEL | 空 | 答案生成时看图（如 qwen-vl / glm-4v） |
| KB_OCR_ENABLED | false | PaddleOCR（需 `pip install kb-agent[vision]`） |
| KB_API_KEY / KB_API_READ_KEY / KB_API_ADMIN_KEY | 空 | REST 鉴权；`KB_API_KEY` 兼容旧配置，等同 admin |
| KB_STUDY_ENABLED / KB_STUDY_* | false / true | 学习扩展默认值；运行时用 `kb config set` 热切换 |

## 运行时开关与治理

```bash
kb config get                              # 查看全部开关
kb config set study.enabled on             # 热切换，无需重启
kb hide <doc_id> / kb unhide <doc_id>      # 隐藏 / 恢复文档
kb events                                  # 治理事件
kb answer-log / kb audit-log               # 答案日志 / 审计日志
kb governance backfill                     # 旧文档 docs_meta 回填
kb repair                                  # 对账向量与文档：恢复中断写入、清理孤儿向量
```

## 架构与数据流

完整的系统上下文、分层、模块、时序与数据流见 [docs/architecture.md](docs/architecture.md)。

```
写入：loaders → cleaner → chunker(子 chunk 挂 doc_id) → 嵌入 → Chroma（切块）
      绑定来源的正文留在源文件，扩写时按路径回读
      旧库父全文：kb governance migrate-parents（见 ADR 0002）
读取：意图识别 → query改写 → 检索top-N → 重排top-k → 父文档扩写 → 编号引用 → LLM → Answer
```

- **子母切块**：向量库存切块（携带 `doc_id`）。绑定来源的整篇正文留在源文件，扩写时回读。旧库用 `kb governance migrate-parents` 迁出，见 [ADR 0002](docs/adr/0002-no-original-duplication.md)。
- **意图识别**：LLM 单次调用返回 `{intent, rewritten_query}`（kb_question / smalltalk / management / out_of_scope），失败自动降级为规则关键词。
- **重排**：bge-reranker Cross-Encoder，模型加载失败自动降级为相似度排序。
- **多模态**：PDF/markdown 内图片 → 提取 → 视觉 LLM 描述（+可选 OCR）→ 图文单元与文本同库检索；配置 `KB_VISION_LLM_MODEL` 后答案生成可看图，否则用描述文本。

## 评估（kb eval）

```bash
# 生成评测集（LLM 辅助，需人工抽检修正）
.venv/bin/kb eval-build examples/sample_notes --output eval/datasets/baseline.jsonl

# 运行评估
.venv/bin/kb eval --dataset eval/datasets/baseline.jsonl --mode retrieval
.venv/bin/kb eval --dataset eval/datasets/baseline.jsonl --mode full

# 参数对比实验（rerank 开关 × top_k）
.venv/bin/kb eval --mode retrieval --compare "rerank=on,off;top_k=3,5"
```

评测集格式（JSONL）：`{"query", "expected_answer", "relevant_doc_ids", "category", "image_required"}`，
其中 `relevant_doc_ids` 必须为库中真实 doc_id（`kb list` 可查）。

**指标**：
- 检索：`recall@k`、`precision@k`、`hit_rate@k`、`mrr`、`ndcg@k`、检索延迟 p50/p95
- 生成（LLM-as-judge）：`accuracy`（对照参考答案）、`faithfulness`（无幻觉率）、`relevance`、`citation_accuracy`、复合分
- 红线：Recall@5 < 0.8 / Accuracy < 0.75 / Faithfulness < 0.9 输出告警
- 报告输出到 `eval/reports/`（JSON + Markdown），建议每次改动跑回归对比

## 组件集成

**方式一：Python 库**（另一个 agent 项目为 Python 时）

```python
from kbagent import KnowledgeBase
kb = KnowledgeBase()
kb.ingest("~/notes")                       # 入库
hits = kb.search("向量检索", top_k=5)      # 纯检索
ans  = kb.ask("混合检索的优缺点？")        # RAG 问答
doc  = kb.get_document(hits[0].doc_id)     # 子母文档：回查原文档
kb.delete_document(doc_id); kb.stats()
```

**方式二：REST API**（跨语言），`kb serve` 启动：

| 端点 | 说明 |
| --- | --- |
| POST /v1/ask | RAG 问答（返回 text/sources/meta） |
| POST /v1/search | 语义检索 |
| POST /v1/ingest | JSON 文本入库 |
| POST /v1/ingest/file | 文件上传入库（multipart） |
| GET /v1/documents | 文档列表 |
| GET /v1/documents/{doc_id} | 原文档全文（含 media） |
| DELETE /v1/documents/{doc_id} | 删除（级联） |
| POST /v1/documents/{doc_id}/refresh | 更新内容 |
| POST /v1/documents/{doc_id}/hide / unhide | 隐藏 / 恢复 |
| GET /v1/settings/flags / PUT /v1/settings/flags/{name} | 运行时开关 |
| GET /v1/settings/embedding | 当前 embedding manifest |
| GET /v1/answer-log / GET /v1/audit-log | 答案日志 / 审计日志 |
| POST /v1/governance/backfill | 旧文档 docs_meta 回填 |
| GET /v1/stats / GET /v1/health | 统计 / 健康检查 |

鉴权：配置 `KB_API_KEY`（等同 admin）或 `KB_API_READ_KEY` / `KB_API_ADMIN_KEY` 后，业务端点要求 `X-API-Key` 头；只读 Key 不能执行入库、删除、开关等管理操作。

完整示例见 `examples/use_as_library.py` 与 `examples/call_api.py`。

## 开发与测试

```bash
.venv/bin/python -m pytest -q          # 110 个测试：切块/清洗/存储/检索/重排/意图/管线/评估/API/多模态/存储抽象/治理
```

## 目录结构

```
src/kbagent/
├── config.py  models.py  errors.py
├── flags.py                    # 运行时开关（热切换）
├── storage/                    # 中立过滤模型、存储工厂
├── governance/                 # docs_meta / doc_events / scope
├── embedding/local.py          # 本地 CPU 嵌入
├── vectorstore/                # VectorStore 协议 + Chroma 适配器
├── document_store/             # DocumentStore 协议 + SQLite 适配器
├── ingestion/                  # loaders → cleaner → chunker → indexer
├── retrieval/                  # query_processor / retriever / reranker / parent_expander
├── generation/                 # llm / pipeline
├── vision/                     # extractor / captioner / ocr / indexer（多模态）
├── eval/                       # dataset / metrics / judge / runner / reports
├── kb.py                       # KnowledgeBase 门面
├── cli.py  ui/chat.py  api/    # CLI / Gradio / FastAPI
```

## Roadmap

按 [status §5](docs/status.md) 推进（L1 与治理 P0 已交付）：

- [x] **L1 检索助手**：清洗、子母切块、重排、多模态、评估（v0.1.0）
- [x] **P0 治理骨架**：`docs_meta` / `doc_events` / scope 隔离 / embedding manifest
- [x] **原件不双写**：绑定源不存整篇正文，扩写回读源文件。旧库用 `kb governance migrate-parents`
- [ ] **P1 可信与治理**：可答性闸门、引用契约、级联 consumers、库外拒答评测、检索 fail-closed scope
- [ ] **P2 严格模式**：声明校验、过期/冲突检测、近似去重
- [ ] **学习切片**：硬编码题、作答、下次到期。不抽取概念。托盘 / 小窗可在此起步
- [ ] **本机 MCP**，以及用 Obsidian 或系统默认应用打开原文
- [ ] **同步通道预留**：许可模型与稳定 id。不做移动端，也不做完整传输
- [ ] **L2 知识结构**：概念与关系抽取、去重、缺口分析、概念页
- [ ] **S1 正式出题**：依赖 L2
- [ ] **人生时间线前置**：敏感范围只走本地模型或拒绝；问答不进明文日志
- [ ] **人生时间线**：事实、事件、健康
- [ ] **未来扩展**：工作知识管理模块
- [ ] **按需**：混合检索（BM25 + 向量）、更多向量库后端（FAISS / LanceDB）
- [ ] **L4 知识伙伴**：目标驱动、主动建议、跨笔记综述、教-回
- [ ] **远期**：音频/视频多模态、ragas 等第三方评估框架

规划文档：[愿景](docs/vision.md) · [架构设计](docs/architecture.md) · [当前进度](docs/status.md) · [L1→L3 最小闭环拆解](docs/mvp-l1-to-l3.md) · [知识治理](docs/knowledge-governance.md) · [知识图谱与本体](docs/knowledge-graph-and-ontology.md) · [ADR 0001 存储选型](docs/adr/0001-storage.md)
