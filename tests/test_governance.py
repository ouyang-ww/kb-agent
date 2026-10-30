"""治理骨架：docs_meta、doc_events、cursor、scope 过滤、hide/unhide。"""

from __future__ import annotations

import pytest

from kbagent.config import Settings
from kbagent.document_store.sqlite_store import SQLiteDocumentStore
from kbagent.errors import NotFoundError
from kbagent.governance import DocMeta, GovernanceStore
from kbagent.kb import KnowledgeBase
from kbagent.models import Document

from helpers import FakeEmbedder, FakeLLM


def _kb(tmp_path) -> KnowledgeBase:
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
    return KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())


def test_ingest_creates_meta_and_event(tmp_path):
    kb = _kb(tmp_path)
    try:
        doc_id = kb.ingest_text("苹果是一种水果。", title="苹果")
        meta = kb.get_doc_meta(doc_id)
        assert meta is not None
        assert meta.scope == "default"
        assert meta.status == "active"
        assert [e["event"] for e in kb.list_events()] == ["ingest"]
    finally:
        kb.close()


def test_private_scope_hidden_from_search_and_get(tmp_path):
    kb = _kb(tmp_path)
    try:
        public = kb.ingest_text("苹果是一种水果。", title="苹果")
        private = kb.ingest_text(
            "苹果是一种秘密水果。", title="秘密苹果", scope="private"
        )
        hits = kb.search("苹果", top_k=5)
        assert hits
        assert all(h.doc_id != private for h in hits)
        assert kb.get_document(private) is None
        assert kb.get_document(private, include_hidden=True) is not None
        assert private not in {d.doc_id for d in kb.list_documents()}
        assert private in {d.doc_id for d in kb.list_documents(include_hidden=True)}
        assert public in {d.doc_id for d in kb.list_documents()}
    finally:
        kb.close()


def test_hide_and_unhide(tmp_path):
    kb = _kb(tmp_path)
    try:
        doc_id = kb.ingest_text("猫是常见的宠物动物。", title="猫")
        assert kb.search("猫", top_k=3)
        assert kb.hide_document(doc_id) is True
        assert kb.search("猫", top_k=3) == []
        assert kb.get_document(doc_id) is None
        assert kb.get_document(doc_id, include_hidden=True) is not None
        assert kb.unhide_document(doc_id) is True
        assert kb.search("猫", top_k=3)
        events = [e["event"] for e in kb.list_events()]
        assert "hide" in events
        assert "unhide" in events
    finally:
        kb.close()


def test_hidden_document_cannot_be_updated_via_public_api(tmp_path):
    kb = _kb(tmp_path)
    try:
        doc_id = kb.ingest_text("隐藏文档。", title="隐藏")
        assert kb.hide_document(doc_id) is True
        with pytest.raises(NotFoundError):
            kb.update_document(doc_id, "新内容")
        assert kb.get_document(doc_id) is None
    finally:
        kb.close()


def test_delete_emits_event_and_marks_deleted(tmp_path):
    kb = _kb(tmp_path)
    try:
        doc_id = kb.ingest_text("要删除的文档。", title="删除")
        assert kb.delete_document(doc_id) is True
        meta = kb.get_doc_meta(doc_id)
        assert meta is not None
        assert meta.status == "deleted"
        assert "delete" in [e["event"] for e in kb.list_events()]
        assert kb.get_document(doc_id, include_hidden=True) is None
    finally:
        kb.close()


def test_scope_metadata_written_to_vectors(tmp_path):
    kb = _kb(tmp_path)
    try:
        doc_id = kb.ingest_text("秘密内容。", title="秘密", scope="secret")
        doc = kb.get_document(doc_id, include_hidden=True)
        assert doc is not None
        rows = kb._vector_store.get("personal", ids=doc.chunk_ids)
        assert rows
        assert rows[0].metadata["scope"] == "secret"
        assert rows[0].metadata["status"] == "active"
    finally:
        kb.close()


def test_governance_store_events_and_cursor(tmp_path):
    store = GovernanceStore(tmp_path / "gov.sqlite3")
    try:
        store.upsert_meta(DocMeta(doc_id="a", scope="default"))
        first = store.emit_event("a", "ingest")
        second = store.emit_event("a", "hide")
        assert store.get_cursor("test-consumer") == 0
        store.set_cursor("test-consumer", second)
        assert store.get_cursor("test-consumer") == second

        events = store.list_events(after_id=first)
        assert [e["event"] for e in events] == ["hide"]

        store.mark_event_processed(first)
        pending = [e["id"] for e in store.list_events(unprocessed_only=True)]
        assert first not in pending
        assert second in pending
    finally:
        store.close()


def test_answer_log_recorded(tmp_path):
    kb = _kb(tmp_path)
    try:
        kb.ingest_text("向量检索的核心是 embedding。", title="向量")
        answer = kb.ask("向量检索的核心是什么？")
        rows = kb.list_answers()
        assert rows
        assert rows[0]["query"] == "向量检索的核心是什么？"
        assert rows[0]["answer"] == answer.text
        assert rows[0]["citations"]
        assert rows[0]["latency_ms"] is not None
    finally:
        kb.close()


def test_audit_log_recorded(tmp_path):
    kb = _kb(tmp_path)
    try:
        doc_id = kb.ingest_text("审计测试。", title="审计")
        kb.hide_document(doc_id)
        kb.unhide_document(doc_id)
        kb.set_flag("study.enabled", True)
        kb.delete_document(doc_id)
        actions = [e["action"] for e in kb.list_audit()]
        assert "document.hide" in actions
        assert "document.unhide" in actions
        assert "flag.set" in actions
        assert "document.delete" in actions
    finally:
        kb.close()


def test_embedding_manifest_and_pointer(tmp_path):
    kb = _kb(tmp_path)
    try:
        manifest = kb.get_embedding_manifest()
        assert manifest is not None
        assert manifest["id"] == kb.manifest_id
        assert manifest["model"] == "unused"
        assert kb.governance.get_pointer("active_embedding") == kb.manifest_id

        kb.ingest_text("manifest 计数测试。", title="计数")
        manifest = kb.get_embedding_manifest()
        assert manifest is not None
        assert manifest["doc_count"] == 1
    finally:
        kb.close()


def test_backfill_governance_for_legacy_docs(tmp_path):
    kb = _kb(tmp_path)
    try:
        kb._doc_store.upsert(
            Document(
                doc_id="legacy",
                title="legacy",
                full_text="legacy text",
                scope="default",
                status="active",
            )
        )
        assert kb.get_doc_meta("legacy") is None
        assert kb.backfill_governance() == 1
        meta = kb.get_doc_meta("legacy")
        assert meta is not None
        assert meta.doc_id == "legacy"
        assert kb.backfill_governance() == 0
        assert any(
            e["action"] == "governance.backfill" for e in kb.list_audit()
        )
    finally:
        kb.close()


def test_document_store_scope_and_status_roundtrip(tmp_path):
    store = SQLiteDocumentStore(tmp_path / "docs.sqlite3")
    try:
        store.upsert(
            Document(
                doc_id="a",
                title="a",
                full_text="x",
                scope="private",
                status="hidden",
            )
        )
        got = store.get("a")
        assert got is not None
        assert got.scope == "private"
        assert got.status == "hidden"
        assert got.is_hidden is True
        assert store.set_status("a", "active") is True
        assert store.get("a").status == "active"
    finally:
        store.close()
