"""多模态测试：markdown 图片提取、VisionIndexer 图文单元、vision 生成降级。"""

import base64
from pathlib import Path

from kbagent.config import Settings
from kbagent.vision.extractor import extract_images_from_markdown
from kbagent.vision.indexer import VisionIndexer
from kbagent.vision.ocr import OCREngine

from helpers import FakeCaptioner, FakeEmbedder, FakeLLM

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_markdown_extractor(tmp_path):
    img = tmp_path / "chart.png"
    img.write_bytes(PNG_1PX)
    md = "![销量图](chart.png)\n\n![远程](https://example.com/x.png)\n\n![缺失](nope.png)"
    imgs = extract_images_from_markdown(md, tmp_path)
    assert len(imgs) == 1
    assert imgs[0]["alt"] == "销量图"
    assert Path(imgs[0]["path"]).exists()


def test_vision_indexer_creates_media_chunk(tmp_path):
    img = tmp_path / "chart.png"
    img.write_bytes(PNG_1PX)
    md = tmp_path / "doc.md"
    md.write_text("# 报告\n\n![图](chart.png)\n\n正文。", encoding="utf-8")

    settings = Settings(storage_dir=tmp_path / "kb", multimodal_enabled=True)
    vi = VisionIndexer(settings, FakeLLM(), captioner=FakeCaptioner(), ocr=OCREngine(enabled=False))
    chunks, media = vi.process_document(
        doc_id="doc1", title="报告", doc_type="text",
        content=md.read_text(encoding="utf-8"),
        source_path=str(md), collection="personal",
    )
    assert len(media) == 1
    assert "图片描述" in chunks[0].text
    assert chunks[0].doc_id == "doc1"
    assert chunks[0].chunk_id.startswith("doc1#img")
    # 图片已复制到 media 目录
    assert Path(media[0].image_path).exists()


def test_vision_indexer_no_images(tmp_path):
    settings = Settings(storage_dir=tmp_path / "kb", multimodal_enabled=True)
    vi = VisionIndexer(settings, FakeLLM(), captioner=FakeCaptioner(), ocr=OCREngine(enabled=False))
    chunks, media = vi.process_document(
        doc_id="doc2", title="纯文本", doc_type="text",
        content="没有图片的文档内容", source_path="", collection="personal",
    )
    assert chunks == [] and media == []


def test_enabled_vision_reports_available(tmp_path):
    from kbagent.kb import KnowledgeBase

    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=True,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
    )
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        status = kb.vision_status()
        assert status["enabled"] is True
        assert status["available"] is True
        assert status["error"] is None
        assert kb.stats()["vision"] == status
    finally:
        kb.close()


def test_ocr_disabled_returns_empty(tmp_path):
    ocr = OCREngine(enabled=False)
    assert ocr.ocr_text("/nonexistent.png") == ""


def test_vision_generation_fallback(settings, kb):
    """配置 vision 模型但图片路径无效 → 降级纯文本，不报错。"""
    settings.vision_llm_model = "vision-model"
    settings.llm_base_url = "http://127.0.0.1:9/v1"  # 不可达 → vision 调用失败
    kb.ingest_text("含图片描述的内容", title="t")
    ans = kb.ask("有什么图片？", top_k=2)
    assert ans.text  # 正常返回（降级路径）
