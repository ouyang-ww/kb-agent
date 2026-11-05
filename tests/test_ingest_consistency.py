"""向量库与 SQLite 不一致时的补偿、崩溃窗口和 repair。"""

from __future__ import annotations

import pytest

from kbagent.errors import IngestError
from kbagent.storage.filters import eq

from helpers import FakeEmbedder


def _vectors(kb, doc_id: str):
    return kb._vector_store.get(
        kb.settings.default_collection, where=eq("doc_id", doc_id)
    )


def _orphan_doc_ids(kb) -> set[str]:
    coll = kb.settings.default_collection
    missing: set[str] = set()
    for row in kb._vector_store.get(coll):
        doc_id = str((row.metadata or {}).get("doc_id") or "")
        if kb._doc_store.get(doc_id) is None:
            missing.add(doc_id)
    return missing


class _FailOnceUpsert:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.armed = False

    def upsert(self, *args, **kwargs):
        if self.armed:
            self.armed = False
            raise RuntimeError("injected sqlite failure")
        return self._inner.upsert(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FailDocDelete:
    def __init__(self, inner, times: int) -> None:
        self._inner = inner
        self.remaining = times

    def delete(self, doc_id: str) -> bool:
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("injected doc delete failure")
        return self._inner.delete(doc_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FailVectorDeletes:
    """前若干次 delete 抛错，之后放行。"""

    def __init__(self, inner, times: int) -> None:
        self._inner = inner
        self.remaining = times

    def delete(self, *args, **kwargs):
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("injected vector delete failure")
        return self._inner.delete(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CrashWindow(BaseException):
    """模拟进程在删除旧向量之后退出。不继承 Exception，内存回滚不会跑。"""


class _CrashAfterDelete:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.armed = False

    def delete(self, *args, **kwargs):
        result = self._inner.delete(*args, **kwargs)
        if self.armed:
            self.armed = False
            raise _CrashWindow("simulated crash after vector delete")
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_successful_ingest_repair_is_noop(kb):
    doc_id = kb.ingest_text("稳定正文 STABLE_TOKEN", title="稳定")
    assert doc_id
    report = kb.repair()
    assert report["errors"] == []
    assert report["orphans_removed"] == 0
    assert report["rolled_back"] == []
    assert report["cleanups"] == []
    assert report["vectorless"] == []
    assert report["restored"] == []
    doc = kb.get_document(doc_id)
    assert doc is not None
    assert "STABLE_TOKEN" in doc.full_text
    assert _vectors(kb, doc_id)


def test_new_ingest_sqlite_failure_leaves_no_orphan_vectors(kb):
    wrapper = _FailOnceUpsert(kb._indexer.doc_store)
    kb._indexer.doc_store = wrapper
    wrapper.armed = True
    with pytest.raises(RuntimeError, match="injected sqlite failure"):
        kb.ingest_text("孤儿向量不应该留下 ORPHAN_NEW", title="新文档")

    assert kb.list_documents() == []
    assert kb._vector_store.count(kb.settings.default_collection) == 0
    assert _orphan_doc_ids(kb) == set()
    report = kb.repair()
    assert report["errors"] == []
    assert report["orphans_removed"] == 0


def test_reingest_sqlite_failure_keeps_old_document_searchable(tmp_path, kb):
    path = tmp_path / "note.md"
    path.write_text("旧正文 ALPHA_KEEP 仍然可读。\n\n第二段也要留下。", encoding="utf-8")
    ingested = kb.ingest(path)
    assert ingested.ok and ingested.added
    doc_id = ingested.added[0]
    before = kb.get_document(doc_id)
    assert before is not None
    old_hash = before.content_hash
    old_ids = list(before.chunk_ids)

    wrapper = _FailOnceUpsert(kb._indexer.doc_store)
    kb._indexer.doc_store = wrapper
    wrapper.armed = True
    path.write_text("新正文 BETA_SHOULD_NOT_LAND", encoding="utf-8")
    with pytest.raises(RuntimeError, match="injected sqlite failure"):
        kb.ingest(path)

    after = kb.get_document(doc_id)
    assert after is not None
    assert after.content_hash == old_hash
    assert after.chunk_ids == old_ids
    rows = _vectors(kb, doc_id)
    assert {row.id for row in rows} == set(old_ids)
    blob = "\n".join(row.document for row in rows)
    assert "ALPHA_KEEP" in blob
    assert "BETA_SHOULD_NOT_LAND" not in blob
    assert _orphan_doc_ids(kb) == set()
    hits = kb.search("ALPHA_KEEP", top_k=3)
    assert any("ALPHA_KEEP" in hit.text for hit in hits)
    assert all("BETA_SHOULD_NOT_LAND" not in hit.text for hit in hits)
    manifest = kb.get_embedding_manifest()
    assert manifest is not None
    assert manifest["doc_count"] == 1


def test_sqlite_failure_and_rollback_delete_failure_repaired(kb):
    vector_store = _FailVectorDeletes(kb._indexer.vector_store, times=1)
    kb._indexer.vector_store = vector_store
    wrapper = _FailOnceUpsert(kb._indexer.doc_store)
    kb._indexer.doc_store = wrapper
    wrapper.armed = True
    with pytest.raises(IngestError, match="kb repair"):
        kb.ingest_text("半截写入 ORPHAN_TOKEN", title="半截")

    assert kb.list_documents(include_hidden=True) == []
    assert kb._vector_store.count(kb.settings.default_collection) > 0
    assert _orphan_doc_ids(kb)

    report = kb.repair()
    assert report["errors"] == []
    assert kb._vector_store.count(kb.settings.default_collection) == 0
    assert _orphan_doc_ids(kb) == set()
    again = kb.repair()
    assert again["orphans_removed"] == 0
    assert again["errors"] == []


def test_delete_sqlite_failure_restores_vectors_and_alerts(kb):
    doc_id = kb.ingest_text("删除失败仍应保留 KEEP_DELETE", title="笔记")
    assert doc_id
    kb._indexer.doc_store = _FailDocDelete(kb._indexer.doc_store, times=1)
    with pytest.raises(RuntimeError, match="injected doc delete failure"):
        kb.delete_document(doc_id)

    doc = kb.get_document(doc_id)
    assert doc is not None
    assert "KEEP_DELETE" in doc.full_text
    rows = _vectors(kb, doc_id)
    assert rows
    assert any("KEEP_DELETE" in row.document for row in rows)
    hits = kb.search("KEEP_DELETE", top_k=3)
    assert any("KEEP_DELETE" in hit.text for hit in hits)
    report = kb.repair()
    assert report["errors"] == []
    assert report["restored"] == []
    assert kb.get_document(doc_id) is not None


def test_delete_restore_failure_is_repaired(kb):
    doc_id = kb.ingest_text("恢复失败前的正文 KEEP_BOTH", title="笔记")
    assert doc_id

    class _FailRestoreDelete:
        """第一次 delete 真正删掉向量；第二次（补偿）失败，留下待修复日志。"""

        def __init__(self, inner) -> None:
            self._inner = inner
            self.calls = 0

        def delete(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("injected restore delete failure")
            return self._inner.delete(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    kb._indexer.vector_store = _FailRestoreDelete(kb._vector_store)
    kb._indexer.doc_store = _FailDocDelete(kb._indexer.doc_store, times=1)
    with pytest.raises(IngestError, match="kb repair"):
        kb.delete_document(doc_id)

    assert kb.get_document(doc_id) is not None
    assert _vectors(kb, doc_id) == []

    report = kb.repair()
    assert report["errors"] == []
    assert doc_id in report["restored"]
    doc = kb.get_document(doc_id)
    assert doc is not None
    assert "KEEP_BOTH" in doc.full_text
    rows = _vectors(kb, doc_id)
    assert rows
    assert any("KEEP_BOTH" in row.document for row in rows)
    hits = kb.search("KEEP_BOTH", top_k=3)
    assert any("KEEP_BOTH" in hit.text for hit in hits)
    again = kb.repair()
    assert again["restored"] == []
    assert kb.get_document(doc_id) is not None


def test_crash_after_vector_delete_repair_restores_search(tmp_path, kb):
    path = tmp_path / "note.md"
    path.write_text("ALPHA_TOKEN old facts stay searchable.\n\nSecond old paragraph.", encoding="utf-8")
    report = kb.ingest(path)
    assert report.ok and report.added
    doc_id = report.added[0]
    before = kb.get_document(doc_id)
    assert before is not None
    old_hash = before.content_hash
    assert _vectors(kb, doc_id)

    kb._indexer.vector_store = _CrashAfterDelete(kb._indexer.vector_store)
    kb._indexer.vector_store.armed = True
    path.write_text("BETA_TOKEN must not replace the old document.", encoding="utf-8")
    with pytest.raises(_CrashWindow):
        kb.ingest(path)

    # 崩溃窗口里内存回滚没有跑：父文档还是旧的，向量已经被删掉。
    mid = kb.get_document(doc_id)
    assert mid is not None
    assert mid.content_hash == old_hash
    assert _vectors(kb, doc_id) == []
    assert kb.search("ALPHA_TOKEN", top_k=5) == []

    repaired = kb.repair()
    assert repaired["errors"] == []
    assert doc_id in repaired["restored"]
    after = kb.get_document(doc_id)
    assert after is not None
    assert after.content_hash == old_hash
    rows = _vectors(kb, doc_id)
    assert rows
    blob = "\n".join(row.document for row in rows)
    assert "ALPHA_TOKEN" in blob
    assert "BETA_TOKEN" not in blob
    hits = kb.search("ALPHA_TOKEN", top_k=5)
    assert any("ALPHA_TOKEN" in hit.text for hit in hits)
    assert all("BETA_TOKEN" not in hit.text for hit in hits)

    again = kb.repair()
    assert again["restored"] == []
    assert again["rolled_back"] == []
    assert kb.get_document(doc_id) is not None
    assert any("ALPHA_TOKEN" in hit.text for hit in kb.search("ALPHA_TOKEN", top_k=5))


def test_repair_after_reingest_does_not_clobber_new_content(tmp_path, kb):
    """崩溃留下旧快照后，若又成功再入库，repair 只能丢掉过期日志。"""
    path = tmp_path / "note.md"
    path.write_text("ALPHA_TOKEN original facts.\n\nSecond old paragraph.", encoding="utf-8")
    report = kb.ingest(path)
    assert report.ok and report.added
    doc_id = report.added[0]
    original = kb.get_document(doc_id)
    assert original is not None
    original_hash = original.content_hash

    wrapper = _CrashAfterDelete(kb._indexer.vector_store)
    kb._indexer.vector_store = wrapper
    wrapper.armed = True
    path.write_text("BETA_TOKEN crashed attempt that must not stick.", encoding="utf-8")
    with pytest.raises(_CrashWindow):
        kb.ingest(path)
    assert kb.get_document(doc_id).content_hash == original_hash
    assert _vectors(kb, doc_id) == []

    wrapper.armed = False
    path.write_text("GAMMA_TOKEN newest facts stay after repair.", encoding="utf-8")
    again = kb.ingest(path)
    assert again.ok
    assert again.added == [doc_id]
    newest = kb.get_document(doc_id)
    assert newest is not None
    assert newest.content_hash not in {original_hash, ""}

    repaired = kb.repair()
    assert repaired["errors"] == []
    assert doc_id in repaired["superseded"]
    assert doc_id not in repaired["restored"]
    assert doc_id not in repaired["rolled_back"]

    kept = kb.get_document(doc_id)
    assert kept is not None
    assert kept.content_hash == newest.content_hash
    rows = _vectors(kb, doc_id)
    blob = "\n".join(row.document for row in rows)
    assert "GAMMA_TOKEN" in blob
    assert "ALPHA_TOKEN" not in blob
    assert "BETA_TOKEN" not in blob
    hits = kb.search("GAMMA_TOKEN", top_k=5)
    assert any("GAMMA_TOKEN" in hit.text for hit in hits)
    assert all("ALPHA_TOKEN" not in hit.text for hit in hits)
    assert all("BETA_TOKEN" not in hit.text for hit in hits)

    follow = kb.repair()
    assert follow["superseded"] == []
    assert follow["restored"] == []
    assert follow["rolled_back"] == []
    rows = _vectors(kb, doc_id)
    assert "GAMMA_TOKEN" in "\n".join(row.document for row in rows)


def test_quarantine_hides_when_governance_update_fails(kb):
    doc_id = kb.ingest_text("旧内容 UNIQUE_OLD", title="笔记")
    assert doc_id
    original = kb.governance.upsert_meta

    def boom(meta):
        if meta.doc_id == doc_id and meta.status == "hidden":
            raise RuntimeError("injected meta failure")
        return original(meta)

    kb.governance.upsert_meta = boom
    kb._indexer.doc_store = _FailDocDelete(kb._indexer.doc_store, times=5)
    with pytest.raises(IngestError, match="待清理"):
        kb.update_document(doc_id, "新内容 UNIQUE_NEW")

    meta = kb.governance.get_meta(doc_id)
    assert meta is not None
    assert meta.status == "active"
    hidden = kb.get_document(doc_id, include_hidden=True)
    assert hidden is not None
    assert hidden.status == "hidden"
    assert kb.get_document(doc_id) is None
    assert all("UNIQUE_OLD" not in hit.text for hit in kb.search("UNIQUE_OLD", top_k=5))
    assert any("UNIQUE_NEW" in hit.text for hit in kb.search("UNIQUE_NEW", top_k=5))


def test_quarantine_fails_loudly_when_hide_does_not_land(kb):
    doc_id = kb.ingest_text("旧内容 STILL_OLD", title="笔记")
    assert doc_id
    original_meta = kb.governance.upsert_meta

    def boom_meta(meta):
        if meta.doc_id == doc_id and meta.status == "hidden":
            raise RuntimeError("injected meta failure")
        return original_meta(meta)

    class _FailHide:
        def __init__(self, inner) -> None:
            self._inner = inner

        def upsert(self, doc):
            if doc.doc_id == doc_id and doc.status == "hidden":
                raise RuntimeError("injected row hide failure")
            return self._inner.upsert(doc)

        def delete(self, target: str) -> bool:
            raise RuntimeError("injected doc delete failure")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    kb.governance.upsert_meta = boom_meta
    kb._indexer.doc_store = _FailHide(kb._indexer.doc_store)
    with pytest.raises(IngestError, match="未能从默认检索隐藏"):
        kb.update_document(doc_id, "新内容 STILL_NEW")

    assert kb.governance.get_meta(doc_id).status == "active"
    visible = kb.get_document(doc_id, include_hidden=True)
    assert visible is not None
    assert visible.status == "active"
    assert any("STILL_OLD" in hit.text for hit in kb.search("STILL_OLD", top_k=5))

    kb.governance.upsert_meta = original_meta
    kb._indexer.doc_store = kb._doc_store
    repaired = kb.repair()
    assert doc_id in repaired["cleanups"]
    assert kb.get_document(doc_id, include_hidden=True) is None
    assert all("STILL_OLD" not in hit.text for hit in kb.search("STILL_OLD", top_k=5))
    assert any("STILL_NEW" in hit.text for hit in kb.search("STILL_NEW", top_k=5))


def test_update_old_delete_failure_hides_duplicate_until_repair(kb):
    doc_id = kb.ingest_text("旧内容 UNIQUE_OLD", title="笔记")
    assert doc_id
    kb._indexer.doc_store = _FailDocDelete(kb._indexer.doc_store, times=5)
    with pytest.raises(IngestError, match="待清理"):
        kb.update_document(doc_id, "新内容 UNIQUE_NEW")

    visible = kb.list_documents()
    assert len(visible) == 1
    new_id = visible[0].doc_id
    assert new_id != doc_id
    assert "UNIQUE_NEW" in visible[0].full_text
    hidden = kb.get_document(doc_id, include_hidden=True)
    assert hidden is not None
    assert "UNIQUE_OLD" in hidden.full_text
    assert kb.get_document(doc_id) is None

    old_hits = kb.search("UNIQUE_OLD", top_k=5)
    assert all("UNIQUE_OLD" not in hit.text for hit in old_hits)
    new_hits = kb.search("UNIQUE_NEW", top_k=5)
    assert any("UNIQUE_NEW" in hit.text for hit in new_hits)
    assert all(hit.doc_id != doc_id for hit in new_hits)

    kb._indexer.doc_store.remaining = 0
    repaired = kb.repair()
    assert repaired["errors"] == []
    assert doc_id in repaired["cleanups"]
    assert kb.get_document(doc_id, include_hidden=True) is None
    assert _vectors(kb, doc_id) == []
    got = kb.get_document(new_id)
    assert got is not None
    assert "UNIQUE_NEW" in got.full_text
    hits = kb.search("UNIQUE_NEW", top_k=5)
    assert any("UNIQUE_NEW" in hit.text for hit in hits)
    assert all("UNIQUE_OLD" not in hit.text for hit in hits)
    assert all(hit.doc_id != doc_id for hit in hits)
    manifest = kb.get_embedding_manifest()
    assert manifest is not None
    assert manifest["doc_count"] == 1


def test_repair_removes_parentless_vectors(kb):
    doc_id = kb.ingest_text("真实文档 KEEP_DOC", title="真")
    assert doc_id
    kb.repair()
    coll = kb.settings.default_collection
    kb._vector_store.add(
        coll,
        ids=["orphan-chunk"],
        embeddings=[FakeEmbedder().embed_query("ORPHAN_TOKEN")],
        documents=["ORPHAN_TOKEN 没有父文档"],
        metadatas=[{"doc_id": "missing-parent", "title": "orphan"}],
    )
    assert kb._vector_store.get(coll, ids=["orphan-chunk"])
    report = kb.repair()
    assert report["orphans_removed"] == 1
    assert kb._vector_store.get(coll, ids=["orphan-chunk"]) == []
    assert kb.get_document(doc_id) is not None
    assert _vectors(kb, doc_id)
    again = kb.repair()
    assert again["orphans_removed"] == 0


def test_repair_reports_vectorless_doc_without_snapshot(kb):
    doc_id = kb.ingest_text("没有快照的正文 LONE_DOC", title="孤")
    assert doc_id
    assert kb.repair()["vectorless"] == []
    kb._vector_store.delete(
        kb.settings.default_collection, where=eq("doc_id", doc_id)
    )
    report = kb.repair()
    assert doc_id in report["vectorless"]
    doc = kb.get_document(doc_id)
    assert doc is not None
    assert "LONE_DOC" in doc.full_text
    assert report["errors"] == []
