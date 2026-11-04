"""ADR 0002：绑定原件不把整篇正文写入 SQLite，扩写回读源文件。"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from kbagent.api.server import create_app
from kbagent.config import Settings
from kbagent.document_store.sqlite_store import SQLiteDocumentStore
from kbagent.governance.originals import migrate_document
from kbagent.ingestion.loaders import content_hash
from kbagent.ingestion.source_text import read_cleaned_source
from kbagent.kb import KnowledgeBase
from kbagent.models import (
    SOURCE_BOUND,
    SOURCE_DIVERGED,
    SOURCE_INLINE,
    SOURCE_LEGACY,
    SOURCE_ORPHANED,
    ChunkLocation,
    Document,
    RetrievalHit,
)
from kbagent.retrieval.parent_expander import ParentExpander

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
    )
    data.update(overrides)
    return Settings(**data)


def _kb(tmp_path, **overrides) -> KnowledgeBase:
    return KnowledgeBase(
        _settings(tmp_path, **overrides),
        embedder=FakeEmbedder(),
        llm=FakeLLM(),
    )


def _hit(doc: Document, text: str, **kwargs) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=kwargs.pop("chunk_id", doc.chunk_locations[0].chunk_id if doc.chunk_locations else "c0"),
        doc_id=doc.doc_id,
        text=text,
        score=0.9,
        title=doc.title,
        source_path=doc.source_path,
        **kwargs,
    )


class _MemStore:
    def __init__(self, doc: Document) -> None:
        self.doc = doc

    def get(self, doc_id: str):
        return self.doc if doc_id == self.doc.doc_id else None


def test_bound_upsert_does_not_persist_body(tmp_path):
    store = SQLiteDocumentStore(tmp_path / "docs.sqlite3")
    store.upsert(
        Document(
            doc_id="b",
            title="绑定",
            full_text="整篇不应该落库",
            source_path=str(tmp_path / "note.md"),
            content_hash="abc",
            summary="摘要",
            source_kind=SOURCE_BOUND,
            chunk_locations=[ChunkLocation("b#0000", 0, 4, "正文")],
        )
    )
    got = store.get("b")
    assert got is not None
    assert got.full_text == ""
    assert got.source_kind == SOURCE_BOUND
    assert got.source_path.endswith("note.md")
    assert got.content_hash == "abc"
    assert got.summary == "摘要"
    assert got.chunk_locations[0].start == 0
    assert got.chunk_locations[0].end == 4
    assert got.chunk_locations[0].section == "正文"
    store.close()


def test_inline_ingest_keeps_body(tmp_path):
    kb = _kb(tmp_path)
    try:
        text = "仅经 API 提交的正文，没有外部文件。"
        doc_id = kb.ingest_text(text, title="inline")
        doc = kb.get_document(doc_id)
        assert doc is not None
        assert doc.source_kind == SOURCE_INLINE
        assert doc.source_path == ""
        assert text in doc.full_text
        assert doc.summary
        assert doc.content_hash
        assert doc.chunk_locations
    finally:
        kb.close()


def test_bound_ingest_stores_path_hash_summary_and_chunk_text(tmp_path):
    kb = _kb(tmp_path)
    try:
        body = "# 标题\n\n" + ("向量检索把文本变成向量。" * 40)
        path = tmp_path / "vault.md"
        path.write_text(body, encoding="utf-8")
        doc_id = kb.ingest(path).added[0]
        doc = kb.get_document(doc_id)
        assert doc is not None
        assert doc.source_kind == SOURCE_BOUND
        assert doc.source_path == str(path)
        assert doc.full_text == ""
        assert doc.content_hash
        assert doc.summary
        assert len(doc.summary) < len(body)
        assert len(doc.summary) <= 240
        cleaned = read_cleaned_source(path)
        assert doc.chunk_locations
        stored = {
            item.id: item
            for item in kb._vector_store.get(kb.settings.default_collection)
        }
        for loc in doc.chunk_locations:
            if loc.start < 0:
                continue
            piece = cleaned[loc.start : loc.end]
            assert stored[loc.chunk_id].document == piece
            assert stored[loc.chunk_id].metadata["char_start"] == loc.start
            assert stored[loc.chunk_id].metadata["char_end"] == loc.end
        row = sqlite3.connect(kb.settings.documents_db).execute(
            "SELECT full_text FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        assert row[0] == ""
    finally:
        kb.close()


def test_parent_expansion_reads_source_not_sqlite(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("磁盘上的父文档。包含可定位的句子。", encoding="utf-8")
    cleaned = read_cleaned_source(path)
    doc = Document(
        doc_id="d1",
        title="笔记",
        full_text="SENTINEL_SHOULD_NOT_LEAK",
        source_path=str(path),
        content_hash=content_hash(cleaned),
        source_kind=SOURCE_BOUND,
        chunk_locations=[ChunkLocation("d1#0000", 0, len(cleaned), "")],
    )
    hit = RetrievalHit(
        chunk_id="d1#0000",
        doc_id="d1",
        text="可定位的句子",
        score=0.9,
        char_start=0,
        char_end=len(cleaned),
    )
    ctx = ParentExpander(_MemStore(doc), max_parent_chars=4000).expand([hit])
    assert ctx[0].text == cleaned
    assert "SENTINEL_SHOULD_NOT_LEAK" not in ctx[0].text

    path.write_text("文件已改成 NEW_ON_DISK。", encoding="utf-8")
    ctx = ParentExpander(_MemStore(doc), max_parent_chars=4000).expand([hit])
    assert ctx[0].text == hit.text
    assert "NEW_ON_DISK" not in ctx[0].text
    assert "SENTINEL_SHOULD_NOT_LEAK" not in ctx[0].text

    path.unlink()
    ctx = ParentExpander(_MemStore(doc), max_parent_chars=4000).expand([hit])
    assert ctx[0].text == hit.text
    assert "SENTINEL_SHOULD_NOT_LEAK" not in ctx[0].text


def test_max_parent_chars_windows_source_file_not_sqlite_body(tmp_path):
    kb = _kb(tmp_path, max_parent_chars=40)
    try:
        inline = "甲" * 180
        inline_id = kb.ingest_text(inline, title="长 inline")
        inline_doc = kb.get_document(inline_id)
        assert inline_doc is not None
        assert len(inline_doc.full_text) == 180
        inline_ctx = kb._parent_expander.expand(
            [
                RetrievalHit(
                    chunk_id=inline_doc.chunk_locations[0].chunk_id,
                    doc_id=inline_id,
                    text=inline[:20],
                    score=1.0,
                    char_start=0,
                    char_end=40,
                )
            ]
        )
        assert len(inline_ctx[0].text) <= 40
        assert inline_ctx[0].text == inline[:40]

        body = "HITMARKER" + ("后面。" * 400)
        path = tmp_path / "long.md"
        path.write_text(body, encoding="utf-8")
        doc_id = kb.ingest(path).added[0]
        doc = kb.get_document(doc_id)
        assert doc is not None
        assert doc.full_text == ""
        assert len(doc.summary) <= 240
        loc = doc.chunk_locations[0]
        ctx = kb._parent_expander.expand(
            [
                RetrievalHit(
                    chunk_id=loc.chunk_id,
                    doc_id=doc_id,
                    text="HITMARKER",
                    score=1.0,
                    char_start=loc.start,
                    char_end=loc.end,
                )
            ]
        )
        assert len(ctx[0].text) <= 40
        assert ctx[0].text.startswith("HITMARKER")
        assert "HITMARKER" not in (doc.full_text or "")
    finally:
        kb.close()


def test_migrate_existing_source_drops_body_and_keeps_hash(tmp_path):
    kb = _kb(tmp_path)
    try:
        path = tmp_path / "keep.md"
        text = "迁移前已经写入库里的父文档全文。"
        path.write_text(text, encoding="utf-8")
        digest = content_hash(read_cleaned_source(path))
        kb._doc_store.upsert(
            Document(
                doc_id="old-keep",
                title="keep",
                full_text=text,
                source_path=str(path),
                content_hash=digest,
                source_kind=SOURCE_LEGACY,
            )
        )
        report = kb.migrate_parent_bodies()
        assert report["bound"] == 1
        assert "re_ingest_hint" not in report
        doc = kb.get_document("old-keep")
        assert doc is not None
        assert doc.full_text == ""
        assert doc.source_kind == SOURCE_BOUND
        assert doc.source_path == str(path)
        assert doc.content_hash == digest
        assert any(
            entry["action"] == "governance.migrate_parents" for entry in kb.list_audit()
        )
        again = kb.migrate_parent_bodies()
        assert again["unchanged"] == 1
        assert again["bound"] == 0
        audits = [
            entry
            for entry in kb.list_audit()
            if entry["action"] == "governance.migrate_parents"
        ]
        assert len(audits) == 1

        cleaned = read_cleaned_source(path)
        ctx = kb._parent_expander.expand(
            [
                RetrievalHit(
                    chunk_id="old-keep#0000",
                    doc_id=doc.doc_id,
                    text=cleaned[:12],
                    score=1.0,
                )
            ]
        )
        assert ctx[0].text == cleaned
    finally:
        kb.close()


def test_migrate_fills_hash_when_missing(tmp_path):
    kb = _kb(tmp_path)
    try:
        path = tmp_path / "hashed.md"
        text = "需要补上内容哈希。"
        path.write_text(text, encoding="utf-8")
        kb._doc_store.upsert(
            Document(
                doc_id="no-hash",
                title="h",
                full_text=text,
                source_path=str(path),
                content_hash="",
                source_kind=SOURCE_LEGACY,
            )
        )
        kb.migrate_parent_bodies()
        doc = kb.get_document("no-hash")
        assert doc is not None
        assert doc.full_text == ""
        assert doc.content_hash == content_hash(read_cleaned_source(path))
        assert doc.source_kind == SOURCE_BOUND
    finally:
        kb.close()


def test_migrate_drifted_file_keeps_body(tmp_path):
    kb = _kb(tmp_path)
    try:
        indexed = "索引时的正文，向量按这篇切块。"
        path = tmp_path / "drift.md"
        path.write_text(indexed, encoding="utf-8")
        cleaned_indexed = read_cleaned_source(path)
        digest = content_hash(cleaned_indexed)
        path.write_text("磁盘已被改成另一篇。", encoding="utf-8")
        kb._doc_store.upsert(
            Document(
                doc_id="drift",
                title="drift",
                full_text=cleaned_indexed,
                source_path=str(path),
                content_hash=digest,
                source_kind=SOURCE_LEGACY,
                collection="personal",
            )
        )
        report = kb.migrate_parent_bodies()
        assert report["diverged"] == 1
        assert report["bound"] == 0
        assert "重新入库" in report["re_ingest_hint"]
        doc = kb.get_document("drift")
        assert doc is not None
        assert doc.source_kind == SOURCE_DIVERGED
        assert doc.full_text == cleaned_indexed
        assert doc.content_hash == digest
        assert doc.source_path == str(path)

        again = kb.migrate_parent_bodies()
        assert again["unchanged"] == 1
        assert again["diverged"] == 0
        assert kb.get_document("drift").full_text == cleaned_indexed

        path.write_text(indexed, encoding="utf-8")
        promoted = kb.migrate_parent_bodies()
        assert promoted["bound"] == 1
        restored = kb.get_document("drift")
        assert restored.source_kind == SOURCE_BOUND
        assert restored.full_text == ""
        assert restored.content_hash == digest
    finally:
        kb.close()


def test_migrate_locations_follow_stored_chunks(tmp_path):
    kb = _kb(tmp_path)
    try:
        text = ("甲" * 40) + ("乙" * 40) + ("丙" * 40)
        path = tmp_path / "aligned.md"
        path.write_text(text, encoding="utf-8")
        cleaned = read_cleaned_source(path)
        pieces = {
            "aligned#0000": cleaned[10:35],
            "aligned#0001": cleaned[70:100],
        }
        ids = list(pieces)
        kb._vector_store.add(
            "personal",
            ids,
            kb._embedder.embed_texts(list(pieces.values())),
            list(pieces.values()),
            [
                {"doc_id": "aligned", "section": "甲段", "char_start": 10, "char_end": 35},
                {"doc_id": "aligned", "section": "乙段"},
            ],
        )
        kb._doc_store.upsert(
            Document(
                doc_id="aligned",
                title="aligned",
                full_text=cleaned,
                source_path=str(path),
                content_hash=content_hash(cleaned),
                source_kind=SOURCE_LEGACY,
                collection="personal",
                chunk_ids=ids,
            )
        )
        kb.settings.chunk_size = 16
        report = kb.migrate_parent_bodies()
        assert report["bound"] == 1
        doc = kb.get_document("aligned")
        assert doc is not None
        assert doc.full_text == ""
        by_id = {loc.chunk_id: loc for loc in doc.chunk_locations}
        assert set(by_id) == set(pieces)
        assert by_id["aligned#0000"].start == 10
        assert by_id["aligned#0000"].end == 35
        assert cleaned[by_id["aligned#0000"].start : by_id["aligned#0000"].end] == pieces["aligned#0000"]
        assert cleaned[by_id["aligned#0001"].start : by_id["aligned#0001"].end] == pieces["aligned#0001"]
        assert by_id["aligned#0001"].start == 70
        stored = {
            item.id: item.document
            for item in kb._vector_store.get("personal", ids=ids)
        }
        for chunk_id, piece in pieces.items():
            assert stored[chunk_id] == piece
            loc = by_id[chunk_id]
            assert cleaned[loc.start : loc.end] == stored[chunk_id]
    finally:
        kb.close()


def test_expansion_hash_mismatch_falls_back_to_chunk(tmp_path):
    kb = _kb(tmp_path)
    try:
        path = tmp_path / "note.md"
        original = "入库时的父文档。向量切块停在这句话。"
        path.write_text(original, encoding="utf-8")
        doc_id = kb.ingest(path).added[0]
        doc = kb.get_document(doc_id)
        assert doc is not None
        assert doc.source_kind == SOURCE_BOUND
        chunk = doc.chunk_locations[0]
        hit_text = read_cleaned_source(path)[chunk.start : chunk.end]
        path.write_text("DRIFTED_FILE 不应进入提示词。", encoding="utf-8")
        ctx = kb._parent_expander.expand(
            [
                RetrievalHit(
                    chunk_id=chunk.chunk_id,
                    doc_id=doc_id,
                    text=hit_text,
                    score=1.0,
                    char_start=chunk.start,
                    char_end=chunk.end,
                )
            ]
        )
        assert ctx[0].text == hit_text
        assert "DRIFTED_FILE" not in ctx[0].text
    finally:
        kb.close()


def test_migrate_missing_source_becomes_orphaned_copy(tmp_path):
    kb = _kb(tmp_path)
    try:
        missing = tmp_path / "gone.md"
        kb._doc_store.upsert(
            Document(
                doc_id="gone",
                title="gone",
                full_text="只剩库内副本",
                source_path=str(missing),
                content_hash="abc",
                source_kind=SOURCE_LEGACY,
            )
        )
        kb._doc_store.upsert(
            Document(
                doc_id="api",
                title="api",
                full_text="无路径的旧正文",
                source_kind=SOURCE_LEGACY,
            )
        )
        report = kb.migrate_parent_bodies()
        assert report["orphaned_copy"] == 1
        assert report["inline"] == 1
        orphan = kb.get_document("gone")
        assert orphan is not None
        assert orphan.source_kind == SOURCE_ORPHANED
        assert orphan.full_text == "只剩库内副本"
        assert orphan.source_path == str(missing)
        inline = kb.get_document("api")
        assert inline is not None
        assert inline.source_kind == SOURCE_INLINE
        assert inline.full_text == "无路径的旧正文"

        ctx = kb._parent_expander.expand(
            [RetrievalHit(chunk_id="gone#0000", doc_id="gone", text="副本", score=0.5)]
        )
        assert ctx[0].text == "只剩库内副本"

        again = kb.migrate_parent_bodies()
        assert again["unchanged"] == 2
        assert kb.get_document("gone").full_text == "只剩库内副本"
    finally:
        kb.close()


def test_legacy_schema_rows_default_to_legacy(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE documents (
          doc_id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          source_path TEXT DEFAULT '',
          doc_type TEXT DEFAULT 'text',
          content_hash TEXT DEFAULT '',
          metadata TEXT DEFAULT '{}',
          collection TEXT DEFAULT 'personal',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          full_text TEXT NOT NULL,
          chunk_ids TEXT DEFAULT '[]',
          media TEXT DEFAULT '[]',
          scope TEXT DEFAULT 'default',
          status TEXT DEFAULT 'active'
        )
        """
    )
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy-row",
            "旧",
            "",
            "text",
            "h",
            "{}",
            "personal",
            "2026-10-01T00:00:00+00:00",
            "2026-10-01T00:00:00+00:00",
            "旧库里的父全文",
            "[]",
            "[]",
            "default",
            "active",
        ),
    )
    conn.commit()
    conn.close()

    store = SQLiteDocumentStore(path)
    got = store.get("legacy-row")
    assert got is not None
    assert got.source_kind == SOURCE_LEGACY
    assert got.full_text == "旧库里的父全文"
    assert got.summary == ""
    assert got.chunk_locations == []
    outcome = migrate_document(got)
    assert outcome == "inline"
    store.upsert(got)
    saved = store.get("legacy-row")
    assert saved.source_kind == SOURCE_INLINE
    assert saved.full_text == "旧库里的父全文"
    assert saved.summary
    store.close()


def test_private_bound_document_stays_hidden(tmp_path):
    kb = _kb(tmp_path)
    try:
        path = tmp_path / "secret.md"
        path.write_text("秘密正文不进检索。", encoding="utf-8")
        doc_id = kb.ingest(path, scope="private").added[0]
        assert kb.get_document(doc_id) is None
        hidden = kb.get_document(doc_id, include_hidden=True)
        assert hidden is not None
        assert hidden.source_kind == SOURCE_BOUND
        assert hidden.full_text == ""
        assert hidden.scope == "private"
        assert kb.search("秘密正文", top_k=5) == []
    finally:
        kb.close()


def test_update_without_path_becomes_inline(tmp_path):
    kb = _kb(tmp_path)
    try:
        path = tmp_path / "note.md"
        path.write_text("磁盘原文。", encoding="utf-8")
        doc_id = kb.ingest(path).added[0]
        new_id = kb.update_document(doc_id, "新提交的正文没有外部文件")
        assert new_id and new_id != doc_id
        updated = kb.get_document(new_id)
        assert updated is not None
        assert updated.source_kind == SOURCE_INLINE
        assert "新提交的正文" in updated.full_text
        assert kb.get_document(doc_id) is None
    finally:
        kb.close()


def test_api_bound_omits_body_and_migrate_is_admin_only(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("磁盘原文给 API。", encoding="utf-8")
    settings = _settings(tmp_path, api_read_key="read", api_admin_key="admin")
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        doc_id = kb.ingest(note).added[0]
        client = TestClient(create_app(kb=kb, settings=settings))
        read = {"X-API-Key": "read"}
        admin = {"X-API-Key": "admin"}
        listed = client.get("/v1/documents", headers=read)
        assert listed.status_code == 200
        body = client.get(f"/v1/documents/{doc_id}", headers=read).json()
        assert body["source_kind"] == SOURCE_BOUND
        assert body["full_text"] is None
        assert body["summary"]
        assert body["source_path"].endswith("note.md")

        inline = client.post(
            "/v1/ingest",
            headers=admin,
            json={"content": "仅 API 正文", "title": "api"},
        )
        assert inline.status_code == 200
        inline_doc = client.get(
            f"/v1/documents/{inline.json()['doc_id']}", headers=read
        ).json()
        assert inline_doc["source_kind"] == SOURCE_INLINE
        assert "仅 API" in inline_doc["full_text"]

        files = {"file": ("upload.txt", "上传的正文".encode("utf-8"), "text/plain")}
        uploaded = client.post("/v1/ingest/file", headers=admin, files=files)
        assert uploaded.status_code == 200
        uploaded_doc = client.get(
            f"/v1/documents/{uploaded.json()['doc_id']}", headers=read
        ).json()
        assert uploaded_doc["source_kind"] == SOURCE_INLINE
        assert "上传的正文" in uploaded_doc["full_text"]
        assert uploaded_doc["source_path"] == ""

        denied = client.post("/v1/governance/migrate-parents", headers=read)
        assert denied.status_code == 401
        allowed = client.post("/v1/governance/migrate-parents", headers=admin)
        assert allowed.status_code == 200
        assert "unchanged" in allowed.json()
    finally:
        kb.close()
