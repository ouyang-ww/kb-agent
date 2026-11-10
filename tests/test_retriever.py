"""检索/重排/父扩写测试。"""

from kbagent.models import Document, MediaRef, RetrievalHit
from kbagent.retrieval.parent_expander import ParentExpander
from kbagent.retrieval.reranker import Reranker
from kbagent.retrieval.retriever import Retriever

from helpers import FakeEmbedder


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
    assert out[0].score == 0.9
    assert out[0].rerank_score is None
    assert out[0].rerank_error and "model load failed" in out[0].rerank_error


class _Scores:
    def __init__(self, values):
        self._values = list(values)

    def tolist(self):
        return list(self._values)


class Sigmoid:
    """名字必须是 Sigmoid，和 torch.nn.Sigmoid 一样。"""


class Identity:
    pass


def _fake_cross_encoder(raw, **attrs):
    class Model:
        def predict(self, pairs):
            return _Scores(raw)

    for name, value in attrs.items():
        setattr(Model, name, value)
    return Model()


def _rerank_one(model, vector_score=0.42):
    reranker = Reranker("unused", enabled=True)
    reranker._lazy = lambda: model
    hit = RetrievalHit(chunk_id="a", doc_id="A", text="x", score=vector_score)
    return reranker.rerank("q", [hit], top_k=1)[0]


def test_st4_activation_fn_probability_is_not_sigmoided_again():
    model = _fake_cross_encoder([0.0], activation_fn=Sigmoid())
    hit = _rerank_one(model)
    assert hit.rerank_score == 0.0
    assert hit.rerank_error is None


def test_st3_default_activation_function_probability_is_not_sigmoided_again():
    model = _fake_cross_encoder([0.3319], default_activation_function=Sigmoid())
    hit = _rerank_one(model)
    assert hit.rerank_score == 0.3319
    assert hit.rerank_error is None


def test_explicit_logit_activation_is_sigmoided_once():
    from kbagent.retrieval.reranker import stable_sigmoid

    zero = _rerank_one(_fake_cross_encoder([0.0], activation_fn=Identity()))
    assert zero.rerank_score == stable_sigmoid(0.0) == 0.5
    assert zero.rerank_score != stable_sigmoid(0.5)

    huge = _rerank_one(_fake_cross_encoder([-1000.0], default_activation_function=Identity()))
    assert huge.rerank_score == 0.0
    assert huge.rerank_error is None


def test_missing_activation_attribute_does_not_sigmoid_probabilities():
    """两个属性都没有时，[0, 1] 的 predict 原样留下，不能再变成 sigmoid(0)=0.5。"""
    hit = _rerank_one(_fake_cross_encoder([0.0]))
    assert hit.rerank_score == 0.0
    assert hit.rerank_error is None

    outside = _rerank_one(_fake_cross_encoder([-1000.0]))
    assert outside.rerank_score == 0.0


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
            "scope": "default",
            "status": "active",
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


class _NearFarEmbedder:
    """查询与 HIDDEN 文本重合，VISIBLE 文本落在另一维，用来固定排序。"""

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if text.startswith("HIDDEN"):
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


class _ScriptedVectors:
    def __init__(self, hits) -> None:
        self.hits = hits
        self.asked: list[int] = []

    def query(self, collection, embedding, top_k, where=None):
        self.asked.append(top_k)
        return self.hits[:top_k]

    def count(self, collection) -> int:
        return len(self.hits)


def _hit(chunk_id: str, doc_id: str, score: float):
    from kbagent.vectorstore.base import VectorHit

    return VectorHit(
        id=chunk_id,
        document=doc_id,
        metadata={"doc_id": doc_id, "title": doc_id},
        score=score,
    )


def test_visible_hits_beyond_first_window(tmp_path):
    """前 top_k*3 全是隐藏时，仍返回后面的可见结果。"""
    from kbagent.config import Settings

    hidden = [_hit(f"h{i}", f"hid{i}", 1 - i * 0.01) for i in range(9)]
    visible = [_hit(f"v{i}", f"vis{i}", 0.2 - i * 0.01) for i in range(4)]
    store = _ScriptedVectors(hidden + visible)
    settings = Settings(storage_dir=tmp_path / "kb", embedding_model="unused")
    retriever = Retriever(
        FakeEmbedder(),
        store,
        settings,
        doc_visibility=lambda doc_id: str(doc_id).startswith("vis"),
    )
    hits = retriever.search("q", top_k=3)
    assert [hit.doc_id for hit in hits] == ["vis0", "vis1", "vis2"]
    assert store.asked == [9, 13]

    short = retriever.search("q", top_k=5)
    assert [hit.doc_id for hit in short] == ["vis0", "vis1", "vis2", "vis3"]


def test_hidden_window_expands_without_count(tmp_path):
    from kbagent.config import Settings

    class _NoCount(_ScriptedVectors):
        def count(self, collection) -> int:
            raise RuntimeError("count unavailable")

    hidden = [_hit(f"h{i}", f"hid{i}", 1 - i * 0.01) for i in range(9)]
    visible = [_hit(f"v{i}", f"vis{i}", 0.2) for i in range(2)]
    store = _NoCount(hidden + visible)
    settings = Settings(storage_dir=tmp_path / "kb", embedding_model="unused")
    retriever = Retriever(
        FakeEmbedder(),
        store,
        settings,
        doc_visibility=lambda doc_id: str(doc_id).startswith("vis"),
    )
    hits = retriever.search("q", top_k=2)
    assert [hit.doc_id for hit in hits] == ["vis0", "vis1"]
    assert store.asked[0] == 6
    assert store.asked[-1] >= 11


def test_search_finds_visible_docs_behind_hidden_neighbors(tmp_path):
    """治理把前排近邻标成隐藏/私有后，默认检索仍能凑满 top_k 条可见。"""
    from kbagent.config import Settings
    from kbagent.kb import KnowledgeBase

    from helpers import FakeLLM

    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
    )
    kb = KnowledgeBase(settings, embedder=_NearFarEmbedder(), llm=FakeLLM())
    try:
        hidden_ids = []
        for i in range(6):
            doc_id = kb.ingest_text(f"HIDDEN hide {i} alpha", title=f"h{i}")
            assert kb.hide_document(doc_id)
            hidden_ids.append(doc_id)
        for i in range(3):
            doc_id = kb.ingest_text(
                f"HIDDEN private {i} alpha", title=f"p{i}", scope="private"
            )
            hidden_ids.append(doc_id)
        visible_ids = []
        for i in range(3):
            doc_id = kb.ingest_text(f"VISIBLE public {i} beta", title=f"v{i}")
            visible_ids.append(doc_id)

        hits = kb.search("anything", top_k=3)
        assert len(hits) == 3
        assert {hit.doc_id for hit in hits} == set(visible_ids)
        assert all(kb.get_document(doc_id) is None for doc_id in hidden_ids)
        assert kb.get_doc_meta(hidden_ids[0]).status == "hidden"
        private_meta = kb.get_doc_meta(hidden_ids[-1])
        assert private_meta is not None
        assert private_meta.scope == "private"
        assert private_meta.status == "active"

        revealed = kb.search("anything", top_k=3, include_hidden=True)
        assert len(revealed) == 3
        assert {hit.doc_id for hit in revealed} <= set(hidden_ids)
    finally:
        kb.close()
