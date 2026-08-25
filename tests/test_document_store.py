"""父文档库测试：子母文档关联、回查原文、级联删除。"""

from kbagent.document_store.sqlite_store import SQLiteDocumentStore
from kbagent.models import Document, MediaRef


def test_upsert_and_get(tmp_path):
    store = SQLiteDocumentStore(tmp_path / "docs.sqlite3")
    doc = Document(
        doc_id="abc123", title="测试", full_text="原文内容", source_path="/x.md",
        content_hash="hash1", collection="personal", chunk_ids=["abc123#0000"],
    )
    store.upsert(doc)
    got = store.get("abc123")
    assert got is not None
    assert got.full_text == "原文内容"
    assert got.chunk_ids == ["abc123#0000"]


def test_get_by_hash_dedup(tmp_path):
    store = SQLiteDocumentStore(tmp_path / "docs.sqlite3")
    store.upsert(Document(doc_id="a", title="a", full_text="x", content_hash="h1"))
    assert store.get_by_hash("h1") is not None
    assert store.get_by_hash("nope") is None


def test_list_and_stats(tmp_path):
    store = SQLiteDocumentStore(tmp_path / "docs.sqlite3")
    store.upsert(Document(doc_id="a", title="a", full_text="x", collection="c1",
                          chunk_ids=["a#0", "a#1"]))
    store.upsert(Document(doc_id="b", title="b", full_text="y", collection="c2",
                          chunk_ids=["b#0"]))
    assert len(store.list()) == 2
    assert len(store.list("c1")) == 1
    s = store.stats()
    assert s["documents"] == 2
    assert s["chunks"] == 3
    assert s["by_collection"] == {"c1": 1, "c2": 1}


def test_media_roundtrip(tmp_path):
    store = SQLiteDocumentStore(tmp_path / "docs.sqlite3")
    doc = Document(
        doc_id="m1", title="含图", full_text="图见下",
        media=[MediaRef(image_path="/tmp/img.png", caption="描述")],
    )
    store.upsert(doc)
    got = store.get("m1")
    assert got.media[0].image_path == "/tmp/img.png"
    assert got.media[0].caption == "描述"


def test_delete(tmp_path):
    store = SQLiteDocumentStore(tmp_path / "docs.sqlite3")
    store.upsert(Document(doc_id="a", title="a", full_text="x"))
    assert store.delete("a") is True
    assert store.delete("a") is False
    assert store.get("a") is None
