"""检索默认必须带 scope/status。忘掉过滤器时拒绝执行，而不是搜全库。"""

import pytest

from kbagent.config import Settings
from kbagent.errors import KBError
from kbagent.governance import visibility_is_enforced
from kbagent.governance.consumers import RESIDUAL_CONSUMERS, drain_consumers
from kbagent.governance.store import GovernanceStore
from kbagent.retrieval.retriever import Retriever
from kbagent.storage.filters import all_of, and_where, any_of, compile_chroma, eq, nin
from kbagent.vectorstore.base import VectorHit

from helpers import FakeEmbedder


class _RecordingStore:
    """where 为空时返回全部近邻；带了过滤才丢掉 hidden/private。"""

    def __init__(self, hits) -> None:
        self.hits = hits
        self.wheres = []

    def query(self, collection, embedding, top_k, where=None):
        self.wheres.append(where)
        if where is None:
            return self.hits[:top_k]
        kept = []
        for hit in self.hits:
            meta = hit.metadata or {}
            if meta.get("status") != "active":
                continue
            if meta.get("scope") in {"private", "secret"}:
                continue
            kept.append(hit)
            if len(kept) >= top_k:
                break
        return kept

    def count(self, collection) -> int:
        return len(self.hits)


def _hit(chunk_id: str, doc_id: str, *, status: str, scope: str, score: float):
    return VectorHit(
        id=chunk_id,
        document=doc_id,
        metadata={"doc_id": doc_id, "title": doc_id, "status": status, "scope": scope},
        score=score,
    )


def test_search_without_caller_filter_stays_closed(tmp_path):
    hidden = _hit("h", "hid", status="hidden", scope="default", score=0.99)
    private = _hit("p", "priv", status="active", scope="private", score=0.9)
    visible = _hit("v", "vis", status="active", scope="default", score=0.2)
    store = _RecordingStore([hidden, private, visible])
    settings = Settings(storage_dir=tmp_path / "kb", embedding_model="unused")
    retriever = Retriever(FakeEmbedder(), store, settings, doc_visibility=None)
    hits = retriever.search("q", top_k=5)
    assert [hit.doc_id for hit in hits] == ["vis"]
    assert store.wheres[0] is not None
    assert visibility_is_enforced(store.wheres[0])

    revealed = retriever.search("q", top_k=3, include_hidden=True)
    assert store.wheres[-1] is None
    assert [hit.doc_id for hit in revealed] == ["hid", "priv", "vis"]


def test_forgotten_visibility_filter_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kbagent.retrieval.retriever.visibility_filter", lambda *_args, **_kwargs: None
    )
    store = _RecordingStore(
        [_hit("v", "vis", status="active", scope="default", score=0.5)]
    )
    settings = Settings(storage_dir=tmp_path / "kb", embedding_model="unused")
    retriever = Retriever(FakeEmbedder(), store, settings)
    with pytest.raises(KBError, match="scope/status"):
        retriever.search("q", top_k=1)
    assert store.wheres == []


def test_missing_or_empty_scope_is_not_searched(tmp_path):
    from kbagent.vectorstore.chroma_store import ChromaStore

    store = ChromaStore(tmp_path / "chroma")
    store.add(
        "personal",
        ["ok", "noscope", "empty", "hidden", "private"],
        [[1.0, 0.0]] * 5,
        ["可见", "缺范围", "空范围", "隐藏", "私有"],
        [
            {"doc_id": "ok", "status": "active", "scope": "default"},
            {"doc_id": "noscope", "status": "active"},
            {"doc_id": "empty", "status": "active", "scope": ""},
            {"doc_id": "hidden", "status": "hidden", "scope": "default"},
            {"doc_id": "private", "status": "active", "scope": "private"},
        ],
    )
    settings = Settings(storage_dir=tmp_path / "kb", embedding_model="unused")
    retriever = Retriever(
        _FixedEmbedder(),
        store,
        settings,
        doc_visibility=lambda _doc_id: True,
    )
    hits = retriever.search("q", top_k=10)
    assert {hit.doc_id for hit in hits} == {"ok"}
    store.close()


class _FixedEmbedder:
    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def test_nin_is_not_treated_as_closed(tmp_path):
    opened = all_of(eq("status", "active"), nin("scope", ["private", "secret"]))
    assert visibility_is_enforced(opened) is False
    settings = Settings(storage_dir=tmp_path / "kb", embedding_model="unused")
    retriever = Retriever(FakeEmbedder(), _RecordingStore([]), settings)
    where = retriever._scoped_where(None, include_hidden=False)
    blob = str(compile_chroma(where))
    assert "$in" in blob
    assert "default" in blob
    assert "$nin" not in blob


def test_and_where_keeps_visibility_beside_a_tag():
    merged = and_where({"tag": "food"}, eq("status", "active"))
    compiled = compile_chroma(merged)
    assert compiled["$and"][0]["tag"] == "food" or any(
        item.get("tag") == "food" for item in compiled["$and"]
    )
    # OR 不能把可见性条件摊掉。
    grouped = and_where(any_of(eq("tag", "a"), eq("tag", "b")), eq("status", "active"))
    compiled = compile_chroma(grouped)
    assert "$and" in compiled
    assert any("$or" in item for item in compiled["$and"])


def test_hide_and_unhide_sync_vector_metadata(kb):
    doc_id = kb.ingest_text("猫是常见的宠物动物。", title="猫")
    coll = kb.settings.default_collection
    before = kb._vector_store.get(coll, where=eq("doc_id", doc_id))
    assert before
    assert before[0].metadata["status"] == "active"

    assert kb.hide_document(doc_id) is True
    hidden = kb._vector_store.get(coll, where=eq("doc_id", doc_id))
    assert hidden[0].metadata["status"] == "hidden"
    assert kb.search("猫是常见的宠物动物。", top_k=3) == []
    assert kb.governance.get_cursor("vector_index") >= 1

    assert kb.unhide_document(doc_id) is True
    restored = kb._vector_store.get(coll, where=eq("doc_id", doc_id))
    assert restored[0].metadata["status"] == "active"
    assert kb.search("猫是常见的宠物动物。", top_k=3)

    report = kb.drain_consumers()
    names = {row["consumer"] for row in report["residual"]}
    assert set(RESIDUAL_CONSUMERS) <= names
    assert all(row["implemented"] is False and row["cursor"] == 0 for row in report["residual"])


def test_poison_event_goes_to_dead_letter(tmp_path):
    store = GovernanceStore(tmp_path / "gov.sqlite3")
    try:
        event_id = store.emit_event("d1", "hide", {"collection": "personal"})

        class Boom:
            name = "vector_index"

            def handle(self, event):
                raise RuntimeError("boom")

        report = drain_consumers(store, [Boom()])
        assert report["vector_index"]["dead_letter"] == 1
        assert store.get_cursor("vector_index") == event_id
        letters = store.list_dead_letters("vector_index")
        assert letters and "boom" in letters[0]["error"]
        assert any(row["consumer"] == "mentions" for row in report["residual"])
    finally:
        store.close()
