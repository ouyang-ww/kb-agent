"""多模态索引：文档内图片 → 描述(+OCR) → 图文单元 chunk（挂父 doc_id）。

图片文件复制到 ~/.kbagent/media/{doc_id}/，图文单元与文本 chunk 同库检索。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import Settings
from ..models import Chunk, MediaRef
from .captioner import Captioner, LocalCaptioner, VisionAPICaptioner
from .extractor import extract_images_from_markdown, extract_images_from_pdf
from .ocr import OCREngine


class VisionIndexer:
    def __init__(
        self,
        settings: Settings,
        llm,
        captioner: Captioner | None = None,
        ocr: OCREngine | None = None,
    ) -> None:
        self.settings = settings
        self.ocr = ocr or OCREngine(enabled=settings.ocr_enabled)
        model = settings.image_caption_model or settings.llm_model
        self.captioner = captioner or VisionAPICaptioner(llm, model=model)

    def process_document(
        self,
        doc_id: str,
        title: str,
        doc_type: str,
        content: str,
        source_path: str,
        collection: str,
    ) -> tuple[list[Chunk], list[MediaRef]]:
        chunks: list[Chunk] = []
        media_refs: list[MediaRef] = []
        try:
            images = self._extract(doc_type, content, source_path, doc_id)
        except Exception:
            return chunks, media_refs  # 提取失败不影响文本入库

        if not images:
            return chunks, media_refs

        out_dir = self.settings.media_root / doc_id
        out_dir.mkdir(parents=True, exist_ok=True)

        for i, img in enumerate(images):
            src = Path(img["path"])
            if not src.exists():
                continue
            target = out_dir / f"img_{i:03d}{src.suffix.lower() or '.png'}"
            if not target.exists():
                shutil.copy2(src, target)

            caption = ""
            try:
                caption = self.captioner.caption(
                    str(target), hint=f"来自文档：{title}"
                )
            except Exception:
                caption = ""
            ocr_text = self.ocr.ocr_text(str(target))

            text_parts = []
            if caption:
                text_parts.append(f"【图片描述】{caption}")
            if ocr_text:
                text_parts.append(f"【图片文字】\n{ocr_text}")
            if not text_parts:
                text_parts.append(f"【图片】{title} 中第 {i + 1} 张图片")
            text = "\n".join(text_parts)

            media = MediaRef(
                media_type="image",
                image_path=str(target),
                page=img.get("page"),
                caption=caption,
                original_path=str(src),
            )
            meta = {
                "doc_id": doc_id,
                "title": title,
                "source_path": source_path,
                "section": "图片",
                "doc_type": "image",
                "chunk_index": 9000 + i,
            }
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}#img{i:03d}",
                    doc_id=doc_id,
                    text=text,
                    section="图片",
                    index=9000 + i,
                    metadata=meta,
                    media=media,
                )
            )
            media_refs.append(media)
        return chunks, media_refs

    def _extract(
        self, doc_type: str, content: str, source_path: str, doc_id: str
    ) -> list[dict]:
        if doc_type == "pdf" and source_path and Path(source_path).exists():
            out = self.settings.media_root / doc_id / "_pdf_tmp"
            return extract_images_from_pdf(source_path, out)
        # 文本/markdown：按图片引用提取（相对路径基于源文件目录）
        if source_path:
            base_dir = Path(source_path).parent
        else:
            base_dir = self.settings.media_root
        return extract_images_from_markdown(content, base_dir)
