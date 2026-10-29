"""存储抽象契约：过滤模型、编译、适配器一致性与 manifest 元数据。"""

from __future__ import annotations

import pytest

from kbagent.config import Settings
from kbagent.document_store.base import DocumentStore
from kbagent.document_store.sqlite_store import SQLiteDocumentStore
from kbagent.errors import KBError
from kbagent.kb import KnowledgeBase
from kbagent.models import Document
from kbagent.storage.factory import make_document_store, make_vector_store
from kbagent.storage.filters import (
    all_of,
    any_of,
    compile_chroma,
    compile_sql,
    eq,
    ne,
    normalize_filter,
)
from kbagent.vectorstore.base import (
    StoredVector,
    VectorHit,
    VectorStore,
    distance_to_score,
)
from kbagent.vectorstore.chroma_store import ChromaStore

from helpers import FakeEmbedder, FakeLLM


# ---------- 过滤模型 ----------


def test_normalize_legacy_eq():
    assert normalize_filter({"tag": "food"}) == eq("tag", "food")


def test_normalize_legacy_and_operators():
    f = normalize_filter(
        {"$and": [{"tag": {"$in": ["a", "b"]}}, {"year": {"$gte": 2020}}]}
    )
    assert f is not None
    assert len(f.conditions) == 2
    assert f.conditions[0].op == "in"
    assert f.conditions[1].op == "gte"


def test_normalize_legacy_or():
    f = normalize_filter({"$or": [{"tag": "a"}, {"tag": "b"}]})
    assert f is not None
    assert len(f.any_of) == 1
    assert len(f.any_of[0].any_of) == 2


def test_unknown_legacy_operator_rejected():
    with pytest.raises(KBError):
        normalize_filter({"tag": {"$regex": "x"}})


def test_filter_composition():
    f = eq("a", 1) & ne("b", 2)
    assert len(f.conditions) == 2
    g = all_of(eq("a", 1), ne("b", 2))
    assert g == f
    h = f | eq("c", 3)
    assert h.any_of


def test_compile_chroma():
    f = normalize_filter({"$and": [{"tag": "food"}, {"year": {"$gte": 2020}}]})
    assert compile_chroma(f) == {
        "$and": [{"tag": "food"}, {"year": {"$gte": 2020}}]
    }


def test_compile_chroma_or():
    assert compile_chroma(any_of(eq("tag", "a"), eq("tag", "b"))) == {
        "$or": [{"tag": "a"}, {"tag": "b"}]
    }


def test_compile_sql():
    sql, params = compile_sql(eq("collection", "c1"), {"collection"})
    assert sql == "collection = ?"
    assert params == ["c1"]


def test_compile_sql_rejects_unknown_field():
    with pytest.raises(KBError):
        compile_sql(eq("bad field", 1), {"collection"})


def test_compile_sql_or_group():
    sql, params = compile_sql(
        any_of(eq("collection", "c1"), eq("collection", "c2")), {"collection"}
    )
    assert "OR" in sql
    assert params == ["c1", "c2"]


# ---------- 分数语义 ----------


def test_distance_to_score():
    assert distance_to_score(0.0, "l2") == 1.0
    assert distance_to_score(0.0, "cosine") == 1.0
    assert distance_to_score(1.0, "cosine") == 0.0
    assert distance_to_score(2.0, "cosine") == 0.0  # 裁剪到 [0,1]
    assert distance_to_score(1.0, "ip") == 0.0
    with pytest.raises(KBError):
        distance_to_score(0.0, "unknown")


# ---------- 适配器契约 ----------


def test_document_store_filter(tmp_path):
    store = SQLiteDocumentStore(tmp_path / "docs.sqlite3")
    store.upsert(Document(doc_id="a", title="a", full_text="x", collection="c1"))
    store.upsert(Document(doc_id="b", title="b", full_text="y", collection="c2"))
    assert [d.doc_id for d in store.list(where=eq("collection", "c1"))] == ["a"]
    assert [d.doc_id for d in store.list(where=ne("collection", "c1"))] == ["b"]
    store.close()


def test_vector_store_filter_query_and_delete(tmp_path):
    store = ChromaStore(tmp_path / "chroma")
    store.add(
        "coll",
        ["1", "2"],
        [[1.0, 0.0], [0.0, 1.0]],
        ["apple", "dog"],
        [{"tag": "food"}, {"tag": "pet"}],
    )
    hits = store.query("coll", [1.0, 0.0], 2, where=eq("tag", "food"))
    assert [h.id for h in hits] == ["1"]
    assert isinstance(hits[0], VectorHit)
    assert hits[0].metric == "l2"
    assert hits[0].score == distance_to_score(hits[0].distance, "l2")
    store.delete("coll", where=eq("tag", "food"))
    assert store.count("coll") == 1
    store.close()


def test_store_get_returns_stored_vector(tmp_path):
    store = ChromaStore(tmp_path / "chroma")
    store.add("coll", ["1"], [[1.0, 0.0]], ["apple"], [{"tag": "food"}])
    rows = store.get("coll", ids=["1"])
    assert isinstance(rows[0], StoredVector)
    assert rows[0].document == "apple"
    store.close()


# ---------- 工厂 / Protocol ----------


def test_factory(tmp_path):
    settings = Settings(storage_dir=tmp_path / "kb")
    assert isinstance(make_vector_store(settings), ChromaStore)
    assert isinstance(make_document_store(settings), SQLiteDocumentStore)


def test_protocol_conformance(tmp_path):
    assert isinstance(ChromaStore(tmp_path / "chroma"), VectorStore)
    assert isinstance(SQLiteDocumentStore(tmp_path / "d.sqlite3"), DocumentStore)


# ---------- manifest 元数据 ----------


def test_chunk_metadata_contains_manifest(tmp_path):
    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
        llm_model="fake-model",
    )
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    doc_id = kb.ingest_text("向量检索的核心是 embedding。", title="向量笔记")
    assert doc_id
    doc = kb.get_document(doc_id)
    assert doc is not None
    rows = kb._vector_store.get("personal", ids=doc.chunk_ids)
    assert rows
    meta = rows[0].metadata
    assert meta["embedding_model"] == "unused"
    assert meta["embedding_manifest_id"] == settings.embedding_manifest
    assert meta["chunker_version"] == settings.chunker_version
    kb.close()
