"""内容哈希：完整 SHA256、旧 12 hex 迁移，以及截断碰撞不能跳过入库。"""

from kbagent.document_store.sqlite_store import SQLiteDocumentStore
from kbagent.ingestion.cleaner import Cleaner
from kbagent.ingestion.loaders import (
    HASH_HEX_LEN,
    content_hash,
    doc_id_for,
    hash_display,
    hashes_equal,
    is_legacy_hash,
)
from kbagent.ingestion.source_text import read_cleaned_source, read_verified_source
from kbagent.kb import KnowledgeBase
from kbagent.models import SOURCE_BOUND, Document

from helpers import FakeEmbedder, FakeLLM


def test_full_hash_and_display_truncation():
    left = content_hash("内容甲")
    right = content_hash("内容乙")
    assert len(left) == HASH_HEX_LEN
    assert left != right
    assert hash_display(left) == left[:12]
    assert hash_display(left) != left
    assert is_legacy_hash(left[:12])
    assert hashes_equal(left[:12], left)
    assert not hashes_equal(left[:12], right)


def test_distinct_contents_both_ingest(kb):
    id_a = kb.ingest_text("内容甲，和乙不同。", title="甲")
    id_b = kb.ingest_text("内容乙，和甲不同。", title="乙")
    assert id_a and id_b and id_a != id_b
    assert len(id_a) == HASH_HEX_LEN
    doc_a = kb.get_document(id_a)
    doc_b = kb.get_document(id_b)
    assert doc_a is not None and doc_b is not None
    assert len(doc_a.content_hash) == HASH_HEX_LEN
    assert doc_a.content_hash != doc_b.content_hash
    assert len(kb.list_documents()) == 2


def test_verified_source_accepts_legacy_prefix(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("hello\n", encoding="utf-8")
    cleaned = read_cleaned_source(path)
    full = content_hash(cleaned)
    assert read_verified_source(path, full) == cleaned
    assert read_verified_source(path, full[:12]) == cleaned
    assert read_verified_source(path, "0" * 12) is None
    assert read_verified_source(path, "") is None


def test_legacy_inline_reingest_upgrades_hash_without_duplicate(kb):
    text = "旧库里的同一篇正文"
    cleaned = Cleaner().clean(text).text
    full = content_hash(cleaned)
    collection = kb.settings.default_collection
    title = "旧笔记"
    legacy_id = doc_id_for(f"{collection}:{title}:{full[:12]}")[:12]
    kb._doc_store.upsert(
        Document(
            doc_id=legacy_id,
            title=title,
            full_text=cleaned,
            content_hash=full[:12],
            collection=collection,
        )
    )
    new_id = kb.ingest_text(text, title=title)
    assert new_id == legacy_id
    docs = kb._doc_store.list()
    assert len(docs) == 1
    assert docs[0].content_hash == full
    assert kb.ingest_text(text, title=title) is None


def test_legacy_path_doc_id_is_reused(kb, tmp_path):
    path = tmp_path / "note.md"
    path.write_text("路径稳定。\n", encoding="utf-8")
    cleaned = read_cleaned_source(path)
    full = content_hash(cleaned)
    legacy_id = doc_id_for(str(path))[:12]
    kb._doc_store.upsert(
        Document(
            doc_id=legacy_id,
            title="note",
            full_text="",
            source_path=str(path),
            content_hash=full[:12],
            source_kind=SOURCE_BOUND,
        )
    )
    report = kb.ingest(path)
    assert report.added == [legacy_id]
    assert kb._doc_store.get(doc_id_for(str(path))) is None
    stored = kb.get_document(legacy_id)
    assert stored is not None
    assert stored.content_hash == full
    assert len(kb._doc_store.list()) == 1
    again = kb.ingest(path)
    assert again.added == []
    assert again.skipped


def test_legacy_doc_id_collision_does_not_overwrite(kb, tmp_path):
    path = tmp_path / "new.md"
    path.write_text("新文件。\n", encoding="utf-8")
    full_id = doc_id_for(str(path))
    legacy_id = full_id[:12]
    kb._doc_store.upsert(
        Document(
            doc_id=legacy_id,
            title="other",
            full_text="别的文档",
            source_path=str(tmp_path / "other.md"),
            content_hash="0" * 12,
        )
    )
    report = kb.ingest(path)
    assert report.added == [full_id]
    kept = kb._doc_store.get(legacy_id)
    assert kept is not None
    assert kept.full_text == "别的文档"
    assert kb._doc_store.get(full_id) is not None


def test_truncated_hash_collision_does_not_skip_other_text(kb):
    """库里已有一行 12 hex，不能挡住全文不同、只是前缀碰巧相同的新文档。"""
    fresh = "全新的正文，不应被旧哈希挡住。"
    cleaned = Cleaner().clean(fresh).text
    full = content_hash(cleaned)
    kb._doc_store.upsert(
        Document(
            doc_id="legacycollide",
            title="旧",
            full_text="完全不同的旧正文",
            content_hash=full[:12],
            collection=kb.settings.default_collection,
        )
    )
    new_id = kb.ingest_text(fresh, title="新")
    assert new_id
    assert new_id != "legacycollide"
    ids = {doc.doc_id for doc in kb._doc_store.list()}
    assert ids == {"legacycollide", new_id}


def test_migrate_upgrades_legacy_twelve_hex(tmp_path):
    from kbagent.config import Settings

    path = tmp_path / "keep.md"
    path.write_text("迁移前的父文档。\n", encoding="utf-8")
    cleaned = read_cleaned_source(path)
    full = content_hash(cleaned)
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
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        kb._doc_store.upsert(
            Document(
                doc_id="old-keep",
                title="keep",
                full_text=cleaned,
                source_path=str(path),
                content_hash=full[:12],
                source_kind="legacy",
            )
        )
        kb._doc_store.upsert(
            Document(
                doc_id="old-inline",
                title="inline",
                full_text="没有文件的旧正文",
                content_hash=content_hash("没有文件的旧正文")[:12],
                source_kind="inline",
            )
        )
        report = kb.migrate_parent_bodies()
        assert report["bound"] == 1
        bound = kb.get_document("old-keep")
        assert bound is not None
        assert bound.full_text == ""
        assert bound.content_hash == full
        inline = kb._doc_store.get("old-inline")
        assert inline is not None
        assert inline.content_hash == content_hash("没有文件的旧正文")
        assert report["hash_upgraded"] == 1

        # 已经绑定、只剩 12 hex 的记录，再跑一次只升级哈希，不把正文写回去。
        store = kb._doc_store
        assert isinstance(store, SQLiteDocumentStore)
        bound.content_hash = full[:12]
        store.upsert(bound)
        again = kb.migrate_parent_bodies()
        assert again["hash_upgraded"] == 1
        assert again["bound"] == 0
        upgraded = kb.get_document("old-keep")
        assert upgraded is not None
        assert upgraded.full_text == ""
        assert upgraded.content_hash == full
    finally:
        kb.close()
