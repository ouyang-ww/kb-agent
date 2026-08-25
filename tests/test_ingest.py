"""入库链路测试：去重、delete 级联、update、stats、图片单元（fake vision）。"""

from kbagent.kb import KnowledgeBase
from kbagent.vision.indexer import VisionIndexer

from helpers import FakeCaptioner, FakeEmbedder, FakeLLM


def test_ingest_text_and_dedup(kb):
    doc_id = kb.ingest_text("向量检索的核心是 embedding。", title="向量笔记")
    assert doc_id
    assert kb.get_document(doc_id) is not None
    # 相同内容再次入库 → 去重跳过
    assert kb.ingest_text("向量检索的核心是 embedding。", title="向量笔记") is None
    assert len(kb.list_documents()) == 1


def test_force_reingest(kb):
    doc_id = kb.ingest_text("第一版内容")
    assert doc_id
    doc_id2 = kb.ingest_text("第一版内容", force=True)
    assert doc_id2 is not None


def test_delete_cascades(kb):
    doc_id = kb.ingest_text("要删除的文档内容")
    assert kb.get_document(doc_id) is not None
    assert kb.delete_document(doc_id) is True
    assert kb.get_document(doc_id) is None
    assert kb.delete_document(doc_id) is False
    # 向量库也应清空
    assert kb._vector_store.count(kb.settings.default_collection) == 0


def test_update_document(kb):
    doc_id = kb.ingest_text("旧内容 A", title="t")
    new_id = kb.update_document(doc_id, "新内容 B 更详细")
    assert new_id and new_id != doc_id
    assert kb.get_document(doc_id) is None
    assert kb.get_document(new_id) is not None


def test_stats(kb):
    kb.ingest_text("文档一的内容", title="a")
    kb.ingest_text("文档二的内容", title="b")
    s = kb.stats()
    assert s["documents"] == 2
    assert s["collections"] == ["personal"]


def test_ingest_markdown_file(tmp_path, settings):
    f = tmp_path / "note.md"
    f.write_text("# 标题\n\n正文内容。", encoding="utf-8")
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    report = kb.ingest(f)
    assert report.ok
    assert len(report.added) == 1


def test_vision_media_chunk(settings, tmp_path):
    """含图 markdown：图文单元入库，media 元数据可回查。"""
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    img = tmp_path / "chart.png"
    img.write_bytes(png)
    md = tmp_path / "doc.md"
    md.write_text("# 报告\n\n![销量图](chart.png)\n\n文本内容。", encoding="utf-8")

    kb = KnowledgeBase(
        settings,
        embedder=FakeEmbedder(),
        llm=FakeLLM(),
        vision_indexer=VisionIndexer(settings, FakeLLM(), captioner=FakeCaptioner()),
    )
    report = kb.ingest(md)
    assert report.ok
    doc = kb.get_document(report.added[0])
    assert doc.media_count == 1
    media = doc.media[0]
    assert media.image_path.endswith(".png")
    # 图文单元可检索（fake embedder 按字符袋，命中含“图片描述”的单元）
    hits = kb.search("图片描述 测试", top_k=5)
    assert any(h.media is not None for h in hits)
    # 级联删除：media 文件被清理
    kb.delete_document(doc.doc_id)
    import pathlib

    assert not pathlib.Path(media.image_path).exists()
