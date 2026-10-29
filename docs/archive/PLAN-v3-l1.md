# 个人知识库 RAG Agent 实施计划（v3 最终版）

## 目标
在 `/home/ouyang/deepseek_workspace/default/kb-agent/` 实现 Python 个人知识库 RAG Agent（包 `kbagent`，CLI `kb`），双重用途：日常知识检索（CLI + Gradio 聊天）与组件化集成（另一个 AI agent 项目的知识库检索/管理组件，Python API + REST API）。功能：清洗、子母文档、重排、意图识别/查询改写、评估体系、多模态（图片）RAG。

## 成功标准
- `pip install -e .` 成功；`kb ingest/search/ask/list/delete/stats/serve/chat/eval` 可用
- 写入链路：loader → cleaner → chunker(子chunk挂doc_id) → 嵌入 → Chroma(子) + SQLite(父全文)，哈希去重
- 读取链路：意图识别 → query改写 → top-N → 重排 top-k → 父文档扩写 → 生成，各环节可开关、可观测（返回 intent/rewritten_query/sources）
- `get_document(doc_id)` 回查原文档；检索结果带 doc_id 溯源；delete 级联清理
- 多模态：PDF/markdown 图片提取→描述(视觉LLM API)→可选OCR→图文单元入库检索→vision生成；无 vision 模型自动降级
- 评估：`kb eval` 跑评测集输出 recall@k/accuracy/faithfulness 等指标报告，支持参数对比实验
- 无相关内容不臆造；问候/管理/无关意图正确路由；REST 全端点可用；Gradio 显示来源
- `pytest` 全部通过

## 技术决策
- LLM：任意 OpenAI 兼容（openai SDK），默认 deepseek-chat；judge/视觉模型独立可配
- 嵌入：本地 CPU `BAAI/bge-small-zh-v1.5`（懒加载 + bge 查询指令）
- 重排：Cross-Encoder `BAAI/bge-reranker-base`，默认开，失败降级相似度排序
- 意图/改写：单次 LLM JSON 调用，规则关键词兜底
- 存储：ChromaDB（子chunk向量）+ SQLite（父文档全文）
- 评估：自建轻量 eval 模块（LLM-as-judge）
- 多模态：PyMuPDF 提取 + 视觉 LLM API 描述（可插拔本地）+ PaddleOCR 可选
- 界面：CLI + Gradio + FastAPI（可选 X-API-Key）

## 项目结构
```
kb-agent/
├── pyproject.toml            # 依赖 + extras: [vision] [local-vision]
├── PLAN.md
├── README.md  .env.example
├── src/kbagent/
│   ├── __init__.py  config.py  models.py  errors.py
│   ├── embedding/local.py
│   ├── vectorstore/chroma_store.py
│   ├── document_store/sqlite_store.py
│   ├── ingestion/{loaders,cleaner,chunker,indexer}.py
│   ├── retrieval/{query_processor,retriever,reranker,parent_expander}.py
│   ├── generation/{llm,pipeline}.py
│   ├── vision/{extractor,captioner,ocr,indexer}.py
│   ├── eval/{dataset,metrics,judge,runner,reports}.py
│   ├── kb.py  cli.py  ui/chat.py  api/{schemas,server}.py
├── eval/datasets/  eval/reports/
├── tests/  examples/
```

## 数据流
- 写入：ingest → loader → cleaner → chunker → 嵌入 → Chroma(子chunk) + SQLite(父全文)；含图文档走 vision 提取图文单元
- 读取：ask → 意图识别 → 改写 → top-N → 重排 → 父扩写（超 KB_MAX_PARENT_CHARS 截 section）→ 编号引用提示词 → LLM（命中图文且有 vision 模型时附图片消息）→ Answer(text, sources[{doc_id,title,score,snippet,media?}], meta{intent,rewritten_query})

## 评估计划（kb eval）
- 评测集 `eval/datasets/*.jsonl`：{query, expected_answer, relevant_doc_ids, category, image_required?}，`kb eval-build` 辅助生成 + 人工抽检，git 版本化
- 检索指标：Recall@k、Precision@k、Hit Rate@k、MRR、NDCG@k、检索延迟
- 生成指标：Accuracy（judge 对照 expected_answer 判定正确/部分/错误）、EM、Faithfulness、相关性/完整性(1-5)、引用准确率、复合分（Recall@5 0.3+Accuracy 0.3+Faithfulness 0.2+相关性 0.2，权重可配）
- `kb eval --dataset x --mode retrieval|full --compare ...` 跑参数矩阵出对比报告；红线 Recall@5≥0.8 / Accuracy≥0.75 / Faithfulness≥0.9 告警
- 多模态专项：图文子集 image Hit Rate 与图文答案正确性

## 配置要点
KB_EMBEDDING_MODEL / KB_CHUNK_SIZE=800 / KB_CHUNK_OVERLAP=100 / KB_RERANK_ENABLED=true / KB_RERANK_CANDIDATES=20 / KB_TOP_K=5 / KB_INTENT_ENABLED=true / KB_QUERY_REWRITE_ENABLED=true / KB_MAX_PARENT_CHARS=4000 / KB_LLM_BASE_URL=https://api.deepseek.com/v1 / KB_LLM_MODEL=deepseek-chat / KB_MULTIMODAL_ENABLED=true / KB_VISION_LLM_MODEL= / KB_OCR_ENABLED=false / KB_EVAL_JUDGE_MODEL= / KB_STORAGE_DIR=~/.kbagent / KB_API_PORT=8710（全部 env 前缀 KB_，支持 .env）

## 边界与降级
清洗不破坏代码块/表格；级联删除（子chunk+父文档+media文件）；意图/改写/重排/视觉任一不可用降级不阻断；坏图/加密 PDF 警告跳过；空库明说无内容不臆造；评测集入库校验；API 不接受服务器路径、默认绑 127.0.0.1。

## 实施步骤
1. 保存 PLAN.md
2. 脚手架：pyproject（含 extras）、config.py、models.py、errors.py、.env.example、.gitignore；pip install -e .
3. 嵌入层 embedding/local.py → 存储层 chroma_store.py + sqlite_store.py
4. 索引层：loaders → cleaner → chunker → indexer（去重/更新/删除/级联）
5. 检索层：query_processor → retriever → reranker → parent_expander
6. 生成层：llm → pipeline；门面 kb.py（线程安全）
7. CLI + Gradio UI + FastAPI REST（含鉴权）
8. 评估模块：dataset/metrics/judge/runner/reports + kb eval / kb eval-build + 基线评测集（示例数据）
9. 多模态：vision 管线 + vision 生成 + UI 图片展示
10. 测试全量通过（含 test_eval / test_vision）；README、示例、端到端验证
11. 交付验收清单 + 说明（如何配置 key、如何跑评估、如何被另一项目集成）

## 假设
- 需用户提供 OpenAI 兼容 API Key（.env）完成 ask/eval 端到端验证；vision 需支持图片输入的模型，无则降级
- 首次下载嵌入 ~100MB、重排 ~1.1GB（可关）、可选 PaddleOCR ~200MB；国内可设 HF_ENDPOINT=https://hf-mirror.com
- torch ~2GB 安装耗时正常；vision/OCR 放 extras 按需安装
- 基线评测集人工参与（提供生成脚本 + 抽检）
