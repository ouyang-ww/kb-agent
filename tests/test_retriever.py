"""检索/重排/父扩写测试。"""

from kbagent.models import Document, MediaRef, RetrievalHit
from kbagent.retrieval.parent_expander import ParentExpander
from kbagent.retrieval.reranker import Reranker
from kbagent.retrieval.retriever import Retriever


def test_search_returns_top_hit(kb):
    kb.ingest_text("猫是常见的宠物动物。", title="猫")
    kb.ingest_text("狗是人类忠诚的朋友。", title="狗")
    hits = kb.search("猫是常见的宠物动物。", top_k=2)
    assert len(hits) >= 1
    assert hits[0].title == "猫"
    assert hits[0].doc_id


def test_search_metadata_filter(kb):
    kb.ingest_text("苹果是一种水果。", title="苹果", metadata={"tag": "food"})
    kb.ingest_text("苹果公司发布新品。", title="苹果公司", metadata={"tag": "tech"})
    hits = kb.search("苹果", top_k=5, where={"tag": "food"})
    assert hits and all(h.doc_id for h in hits)


def test_reranker_fallback_order(monkeypatch):
    """模型加载失败 → 降级为原始相似度排序（monkeypatch _lazy，不依赖网络）。"""

    def boom(self):
        raise RuntimeError("model load failed")

    monkeypatch.setattr(Reranker, "_lazy", boom)
    hits = [
        RetrievalHit(chunk_id="a", doc_id="A", text="x", score=0.9),
        RetrievalHit(chunk_id="b", doc_id="B", text="y", score=0.8),
    ]
    r = Reranker("does-not-exist-model", enabled=True)
    out = r.rerank("q", hits, top_k=1)
    assert out[0].chunk_id == "a"  # 加载失败 → 降级原排序


def test_reranker_disabled():
    hits = [RetrievalHit(chunk_id="a", doc_id="A", text="x", score=0.9)]
    r = Reranker("any", enabled=False)
    assert r.rerank("q", hits, top_k=1) == hits


def test_parent_expander_full_doc(tmp_path):
    from kbagent.document_store.sqlite_store import SQLiteDocumentStore

    store = SQLiteDocumentStore(tmp_path / "pe1.sqlite3")
    doc = Document(doc_id="d1", title="t", full_text="完整父文档内容", source_path="")
    store.upsert(doc)
    exp = ParentExpander(store, max_parent_chars=4000)
    hits = [
        RetrievalHit(chunk_id="d1#0", doc_id="d1", text="片段", score=0.9, title="t")
    ]
    ctx = exp.expand(hits)
    assert len(ctx) == 1
    assert ctx[0].text == "完整父文档内容"
    assert ctx[0].doc_id == "d1"
    store.close()


def test_parent_expander_truncates_window(tmp_path):
    from kbagent.document_store.sqlite_store import SQLiteDocumentStore

    store = SQLiteDocumentStore(tmp_path / "pe2.sqlite3")
    long_text = "前置内容。" + "中间命中内容。" * 100 + "后续内容。"
    doc = Document(doc_id="d1", title="t", full_text=long_text)
    store.upsert(doc)
    exp = ParentExpander(store, max_parent_chars=100)
    hits = [
        RetrievalHit(
            chunk_id="d1#0", doc_id="d1", text="中间命中内容" * 5,
            score=0.9, title="t",
        )
    ]
    ctx = exp.expand(hits)
    assert len(ctx[0].text) <= 100
    store.close()


def test_parent_expander_dedupe_by_doc(tmp_path):
    from kbagent.document_store.sqlite_store import SQLiteDocumentStore

    store = SQLiteDocumentStore(tmp_path / "pe3.sqlite3")
    store.upsert(Document(doc_id="d1", title="t", full_text="f1"))
    exp = ParentExpander(store, max_parent_chars=4000)
    hits = [
        RetrievalHit(chunk_id="d1#0", doc_id="d1", text="a", score=0.9),
        RetrievalHit(chunk_id="d1#1", doc_id="d1", text="b", score=0.8),
    ]
    assert len(exp.expand(hits)) == 1
    store.close()


def test_media_in_retrieval_hit(kb):
    kb.ingest_text("包含图片描述的内容", title="图文")
    from kbagent.vectorstore.chroma_store import ChromaStore

    store = kb._vector_store
    store.add(
        "personal",
        ["img-chunk"],
        kb._embedder.embed_texts(["【图片描述】一张架构图"]),
        ["【图片描述】一张架构图"],
        [{
            "doc_id": "doc9",
            "title": "图",
            "media_type": "image",
            "image_path": "/tmp/img.png",
            "page": 1,
            "caption": "架构图",
            "section": "图片",
            "source_path": "",
        }],
    )
    retriever = Retriever(kb._embedder, store, kb.settings)
    hits = retriever.search("架构图", top_k=3)
    img_hits = [h for h in hits if h.media]
    assert img_hits and img_hits[0].media.image_path == "/tmp/img.png"
