#!/usr/bin/env python
"""组件集成示例 1：作为 Python 库（import kbagent）。

另一个 agent 项目通过该方式直接检索和管理知识库。
"""

from kbagent import KnowledgeBase

kb = KnowledgeBase()  # 读配置（.env 或 KB_* 环境变量）

# 1) 入库
report = kb.ingest("examples/sample_notes")
print("入库:", report)

# 2) 纯检索（返回带分数与 doc_id 的命中）
hits = kb.search("什么是向量检索", top_k=3)
for h in hits:
    print(f"[{h.score:.3f}] {h.title} | doc_id={h.doc_id} | {h.text[:60]}...")

# 3) 按 doc_id 回查原文档（子母文档）
if hits:
    doc = kb.get_document(hits[0].doc_id)
    print("\n原文档全文:\n", doc.full_text[:200])

# 4) RAG 问答（需要配置 LLM key）
ans = kb.ask("RAG 的优缺点是什么？")
print("\n答案:", ans.text)
for i, s in enumerate(ans.sources, 1):
    print(f"  [{i}] {s.title} ({s.doc_id}) score={s.score:.3f}")

# 5) 管理操作
print("\n文档数:", kb.stats()["documents"])
for d in kb.list_documents():
    print(" -", d.doc_id, d.title, f"({d.chunk_count} chunks)")
