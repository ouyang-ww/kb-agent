# kb-agent 项目结构

> 项目版本：**v0.1.0**
> 结构快照生成时间：**2026-08-25 12:16:24 CST**
> 项目路径：`/home/ouyang/deepseek_workspace/default/kb-agent`
> 说明：已排除 `.venv`、`.hf-cache`、`__pycache__`、`.pytest_cache`、`eval/.kb-e2e` 等缓存/运行目录

## 目录树

```
kb-agent/
├── eval/
│   ├── datasets/
│   │   └── baseline.jsonl
│   └── reports/
│       ├── report_20260825_022100.md
│       ├── report_20260825_022100_top_k5_rerankFalse_rewriteTrue.json
│       ├── report_20260825_022128.md
│       ├── report_20260825_022128_top_k5_rerankFalse_rewriteTrue.json
│       ├── report_20260825_022436.md
│       ├── report_20260825_022436_top_k3_rerankFalse_rewriteTrue.json
│       ├── report_20260825_022436_top_k3_rerankTrue_rewriteTrue.json
│       ├── report_20260825_022436_top_k5_rerankFalse_rewriteTrue.json
│       ├── report_20260825_022436_top_k5_rerankTrue_rewriteTrue.json
│       ├── report_20260825_022505.md
│       └── report_20260825_022505_top_k5_rerankFalse_rewriteTrue.json
├── examples/
│   ├── sample_notes/
│   │   ├── 01_rag基础.md
│   │   ├── 02_向量检索.md
│   │   ├── 03_python异步.md
│   │   └── 04_健康饮食.md
│   ├── call_api.py
│   └── use_as_library.py
├── src/
│   └── kbagent/
│       ├── api/
│       │   ├── __init__.py
│       │   ├── schemas.py
│       │   └── server.py
│       ├── document_store/
│       │   ├── __init__.py
│       │   └── sqlite_store.py
│       ├── embedding/
│       │   ├── __init__.py
│       │   └── local.py
│       ├── eval/
│       │   ├── __init__.py
│       │   ├── dataset.py
│       │   ├── judge.py
│       │   ├── metrics.py
│       │   ├── reports.py
│       │   └── runner.py
│       ├── generation/
│       │   ├── __init__.py
│       │   ├── llm.py
│       │   └── pipeline.py
│       ├── ingestion/
│       │   ├── __init__.py
│       │   ├── chunker.py
│       │   ├── cleaner.py
│       │   ├── indexer.py
│       │   └── loaders.py
│       ├── retrieval/
│       │   ├── __init__.py
│       │   ├── parent_expander.py
│       │   ├── query_processor.py
│       │   ├── reranker.py
│       │   └── retriever.py
│       ├── ui/
│       │   ├── __init__.py
│       │   └── chat.py
│       ├── vectorstore/
│       │   ├── __init__.py
│       │   └── chroma_store.py
│       ├── vision/
│       │   ├── __init__.py
│       │   ├── captioner.py
│       │   ├── extractor.py
│       │   ├── indexer.py
│       │   └── ocr.py
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── errors.py
│       ├── kb.py
│       └── models.py
├── tests/
│   ├── conftest.py
│   ├── helpers.py
│   ├── test_api.py
│   ├── test_chunker.py
│   ├── test_cleaner.py
│   ├── test_document_store.py
│   ├── test_eval_runner.py
│   ├── test_ingest.py
│   ├── test_metrics.py
│   ├── test_pipeline.py
│   ├── test_query_processor.py
│   ├── test_retriever.py
│   └── test_vision.py
├── .env.example
├── .gitignore
├── PLAN.md
├── pyproject.toml
├── README.md
└── STRUCTURE.md```

## 模块职责速览

| 目录/文件 | 职责 |
| --- | --- |
| `src/kbagent/kb.py` | KnowledgeBase 门面（唯一业务入口，线程安全） |
| `src/kbagent/ingestion/` | 写入链路：loaders → cleaner(清洗) → chunker → indexer |
| `src/kbagent/retrieval/` | 读取链路：query_processor(意图/改写) → retriever → reranker(重排) → parent_expander(子母文档) |
| `src/kbagent/generation/` | RAG 生成：llm(OpenAI 兼容) + pipeline(编号引用/多模态) |
| `src/kbagent/vectorstore/` + `document_store/` | Chroma(子chunk) + SQLite(父文档全文) |
| `src/kbagent/vision/` | 多模态：图片提取 → 描述 → OCR → 图文单元 |
| `src/kbagent/eval/` | 评估：评测集 / 指标(recall@k, accuracy…) / LLM-as-judge / 对比实验 |
| `src/kbagent/api/` + `ui/` | FastAPI REST + Gradio 聊天界面 |
| `src/kbagent/cli.py` | CLI：ingest / search / ask / list / delete / stats / serve / chat / eval / eval-build |
| `tests/` | 71 个测试，覆盖全部模块 |
| `examples/` | 组件集成示例 + 示例知识库文档 |
| `eval/` | 基线评测集与评估报告 |

## 测试状态

```
71 passed in ~6s
```
