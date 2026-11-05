"""同路径再入库替换旧向量，以及 refresh 失败时保留旧文档。"""

from __future__ import annotations

import pytest

from kbagent.config import Settings
from kbagent.errors import IngestError
from kbagent.kb import KnowledgeBase
from kbagent.models import SOURCE_BOUND, SOURCE_INLINE
from kbagent.storage.filters import eq

from helpers import FakeEmbedder, FakeLLM


def _settings(tmp_path, **overrides) -> Settings:
    data = dict(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
        llm_model="fake-model",
        chunk_size=64,
        chunk_overlap=0,
    )
    data.update(overrides)
    return Settings(**data)


def _kb(tmp_path, **overrides) -> KnowledgeBase:
    return KnowledgeBase(
        _settings(tmp_path, **overrides),
        embedder=FakeEmbedder(),
        llm=FakeLLM(),
    )


def _paragraphs(marker: str, count: int) -> str:
    return "\n\n".join(
        f"{marker} paragraph {i} talks about facts number {i}."
        for i in range(count)
    )


def _vectors(kb: KnowledgeBase, doc_id: str):
    return kb._vector_store.get(
        kb.settings.default_collection, where=eq("doc_id", doc_id)
    )


class _FailOnceAdd:
    """第一次 armed 的 add 抛错，随后的 add（回滚）放行。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.armed = False

    def add(self, *args, **kwargs):
        if self.armed:
            self.armed = False
            raise RuntimeError("injected chroma failure")
        return self._inner.add(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_same_path_reingest_replaces_vectors(tmp_path):
    kb = _kb(tmp_path)
    path = tmp_path / "note.md"
    path.write_text(_paragraphs("ALPHA_TOKEN", 12), encoding="utf-8")
    report = kb.ingest(path)
    assert report.ok and report.added
    doc_id = report.added[0]
    before = kb.get_document(doc_id)
    assert before is not None
    assert before.source_kind == SOURCE_BOUND
    assert before.full_text == ""
    old_ids = list(before.chunk_ids)
    old_hash = before.content_hash
    assert len(old_ids) > 1
    assert any("ALPHA_TOKEN" in row.document for row in _vectors(kb, doc_id))

    path.write_text("BETA_TOKEN only the new facts remain.", encoding="utf-8")
    report = kb.ingest(path)
    assert report.ok
    assert report.failed == []
    assert report.added == [doc_id]

    after = kb.get_document(doc_id)
    assert after is not None
    assert after.source_kind == SOURCE_BOUND
    assert after.full_text == ""
    assert after.content_hash != old_hash
    assert len(after.chunk_ids) < len(old_ids)
    assert len(kb.list_documents()) == 1

    rows = _vectors(kb, doc_id)
    assert rows
    assert len(rows) == len(after.chunk_ids)
    assert all("BETA_TOKEN" in row.document for row in rows)
    assert all("ALPHA_TOKEN" not in row.document for row in rows)

    stale = [chunk_id for chunk_id in old_ids if chunk_id not in set(after.chunk_ids)]
    assert stale
    coll = kb.settings.default_collection
    for chunk_id in stale:
        assert kb._vector_store.get(coll, ids=[chunk_id]) == []

    hits = kb.search("BETA_TOKEN", top_k=5)
    assert any("BETA_TOKEN" in hit.text for hit in hits)
    assert all("ALPHA_TOKEN" not in hit.text for hit in hits)
    manifest = kb.get_embedding_manifest()
    assert manifest is not None
    assert manifest["doc_count"] == 1


def test_same_path_reingest_keeps_created_at(tmp_path, monkeypatch):
    """同 doc_id 再次入库：created_at 不变，updated_at 变。"""
    import kbagent.ingestion.indexer as indexer_mod

    stamps = iter(["2024-01-01T00:00:00+00:00", "2024-01-02T00:00:00+00:00"])

    def fake_now() -> str:
        try:
            return next(stamps)
        except StopIteration as exc:
            raise AssertionError("indexer 调用 _utcnow 的次数超出预期") from exc

    monkeypatch.setattr(indexer_mod, "_utcnow", fake_now)
    kb = _kb(tmp_path)
    try:
        path = tmp_path / "note.md"
        path.write_text("第一版正文，用来记下创建时间。", encoding="utf-8")
        report = kb.ingest(path)
        assert report.ok and report.added
        doc_id = report.added[0]
        before = kb.get_document(doc_id)
        assert before is not None
        assert before.created_at == "2024-01-01T00:00:00+00:00"
        assert before.updated_at == "2024-01-01T00:00:00+00:00"

        path.write_text("第二版正文已经完全不同。", encoding="utf-8")
        report = kb.ingest(path)
        assert report.ok and report.added == [doc_id]
        after = kb.get_document(doc_id)
        assert after is not None
        assert after.created_at == before.created_at
        assert after.updated_at == "2024-01-02T00:00:00+00:00"
        assert after.content_hash != before.content_hash
    finally:
        kb.close()


def test_same_path_vector_write_failure_keeps_old_vectors(tmp_path):
    kb = _kb(tmp_path)
    wrapper = _FailOnceAdd(kb._indexer.vector_store)
    kb._indexer.vector_store = wrapper
    path = tmp_path / "note.md"
    path.write_text(_paragraphs("ALPHA_TOKEN", 8), encoding="utf-8")
    report = kb.ingest(path)
    assert report.ok
    doc_id = report.added[0]
    before = kb.get_document(doc_id)
    assert before is not None
    old_ids = list(before.chunk_ids)
    old_hash = before.content_hash

    wrapper.armed = True
    path.write_text("BETA_TOKEN should not land.", encoding="utf-8")
    with pytest.raises(RuntimeError, match="injected chroma failure"):
        kb.ingest(path)

    after = kb.get_document(doc_id)
    assert after is not None
    assert after.content_hash == old_hash
    assert after.chunk_ids == old_ids
    rows = _vectors(kb, doc_id)
    assert {row.id for row in rows} == set(old_ids)
    blob = "\n".join(row.document for row in rows)
    assert "ALPHA_TOKEN" in blob
    assert "BETA_TOKEN" not in blob
    hits = kb.search("ALPHA_TOKEN", top_k=5)
    assert any("ALPHA_TOKEN" in hit.text for hit in hits)
    assert all("BETA_TOKEN" not in hit.text for hit in hits)


def test_update_document_embed_failure_keeps_old_searchable(kb):
    doc_id = kb.ingest_text("旧内容 ALPHA_KEEP 仍然可读", title="笔记")
    assert doc_id

    class _Boom:
        def embed_texts(self, texts):
            raise RuntimeError("injected embed failure")

    kb._indexer.embedder = _Boom()
    with pytest.raises(RuntimeError, match="injected embed failure"):
        kb.update_document(doc_id, "新内容 BETA_SHOULD_NOT_LAND")

    doc = kb.get_document(doc_id)
    assert doc is not None
    assert "ALPHA_KEEP" in doc.full_text
    assert "BETA_SHOULD_NOT_LAND" not in doc.full_text
    assert len(kb.list_documents()) == 1
    rows = _vectors(kb, doc_id)
    assert rows
    assert all("ALPHA_KEEP" in row.document for row in rows)
    assert all("BETA_SHOULD_NOT_LAND" not in row.document for row in rows)
    hits = kb.search("ALPHA_KEEP", top_k=3)
    assert any("ALPHA_KEEP" in hit.text for hit in hits)
    assert all("BETA_SHOULD_NOT_LAND" not in hit.text for hit in hits)


def test_update_document_clean_failure_keeps_old_searchable(kb):
    doc_id = kb.ingest_text("清洗失败前的正文 KEEP_ME", title="笔记")
    assert doc_id

    def _boom(text):
        raise IngestError("injected clean failure")

    kb._indexer.cleaner.clean = _boom
    with pytest.raises(IngestError, match="injected clean failure"):
        kb.update_document(doc_id, "不会写入的新正文")

    doc = kb.get_document(doc_id)
    assert doc is not None
    assert "KEEP_ME" in doc.full_text
    hits = kb.search("KEEP_ME", top_k=3)
    assert any("KEEP_ME" in hit.text for hit in hits)


def test_update_document_success_still_replaces(kb):
    doc_id = kb.ingest_text("旧内容 UNIQUE_OLD", title="笔记")
    new_id = kb.update_document(doc_id, "新内容 UNIQUE_NEW")
    assert new_id and new_id != doc_id
    assert kb.get_document(doc_id) is None
    got = kb.get_document(new_id)
    assert got is not None
    assert got.source_kind == SOURCE_INLINE
    assert "UNIQUE_NEW" in got.full_text
    assert kb._vector_store.get(
        kb.settings.default_collection, where=eq("doc_id", doc_id)
    ) == []
    rows = _vectors(kb, new_id)
    assert rows
    assert all("UNIQUE_NEW" in row.document for row in rows)
    assert all("UNIQUE_OLD" not in row.document for row in rows)
    hits = kb.search("UNIQUE_NEW", top_k=3)
    assert any("UNIQUE_NEW" in hit.text for hit in hits)
    assert all("UNIQUE_OLD" not in hit.text for hit in hits)
    manifest = kb.get_embedding_manifest()
    assert manifest is not None
    assert manifest["doc_count"] == 1
