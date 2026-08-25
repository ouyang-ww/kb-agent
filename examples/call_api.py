#!/usr/bin/env python
"""组件集成示例 2：通过 REST API（跨语言）。

先运行 `kb serve`（默认 127.0.0.1:8710），再执行本脚本。
若配置了 KB_API_KEY，请设置 KB_API_KEY 环境变量。
"""

import os

import httpx

BASE = os.environ.get("KB_API_BASE", "http://127.0.0.1:8710")
KEY = os.environ.get("KB_API_KEY", "")
HEADERS = {"X-API-Key": KEY} if KEY else {}

client = httpx.Client(base_url=BASE, headers=HEADERS, timeout=30)

# 健康检查
print("health:", client.get("/v1/health").json())

# 入库（JSON 文本）
r = client.post("/v1/ingest", json={
    "content": "多模态 RAG 支持图片检索与视觉问答。",
    "title": "多模态笔记",
})
print("ingest:", r.json())

# 检索
r = client.post("/v1/search", json={"query": "图片检索", "top_k": 3})
for hit in r.json()["hits"]:
    print(f"hit: {hit['title']} score={hit['score']} doc_id={hit['doc_id']}")

# 问答
r = client.post("/v1/ask", json={"query": "什么是多模态 RAG？"})
body = r.json()
print("answer:", body["text"][:200])
print("meta:", body["meta"])

# 文档管理
print("documents:", len(client.get("/v1/documents").json()))
print("stats:", client.get("/v1/stats").json())
