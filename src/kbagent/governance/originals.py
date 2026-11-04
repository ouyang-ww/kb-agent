"""把已入库的父全文迁出应用库。见 ADR 0002。

源文件仍在：写下路径与哈希，删除库内全文，标为 ``bound``。
源文件不在且仍有正文：保留正文，标为 ``orphaned_copy``。
没有外部路径：标为 ``inline``，正文留在库里。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..errors import IngestError
from ..ingestion.chunker import Chunker
from ..ingestion.loaders import content_hash
from ..ingestion.source_text import (
    locations_for_text,
    read_cleaned_source,
    summarize,
)
from ..models import SOURCE_BOUND, SOURCE_INLINE, SOURCE_ORPHANED, Document


def _touch(doc: Document) -> None:
    doc.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fill_from_body(doc: Document, chunker: Chunker) -> None:
    if doc.full_text and not doc.summary:
        doc.summary = summarize(doc.full_text)
    if doc.full_text and not doc.chunk_locations:
        doc.chunk_locations = locations_for_text(doc.doc_id, doc.full_text, chunker)


def migrate_document(doc: Document, chunker: Chunker) -> str:
    """就地更新 ``doc``。返回 ``bound`` / ``inline`` / ``orphaned_copy`` / ``unchanged``。"""
    path = (doc.source_path or "").strip()
    exists = bool(path) and Path(path).is_file()

    if doc.source_kind == SOURCE_BOUND and not doc.full_text:
        return "unchanged"
    if doc.source_kind == SOURCE_INLINE and not path:
        return "unchanged"
    if doc.source_kind == SOURCE_ORPHANED and doc.full_text and not exists:
        return "unchanged"

    if exists:
        try:
            cleaned = read_cleaned_source(path)
        except (OSError, IngestError):
            if not doc.full_text:
                return "unchanged"
            doc.source_kind = SOURCE_ORPHANED
            _fill_from_body(doc, chunker)
            _touch(doc)
            return "orphaned_copy"
        if not doc.content_hash:
            doc.content_hash = content_hash(cleaned)
        summary = summarize(cleaned)
        if summary:
            doc.summary = summary
        locations = locations_for_text(doc.doc_id, cleaned, chunker)
        if locations:
            doc.chunk_locations = locations
        doc.full_text = ""
        doc.source_kind = SOURCE_BOUND
        _touch(doc)
        return "bound"

    if path:
        doc.source_kind = SOURCE_ORPHANED
        _fill_from_body(doc, chunker)
        _touch(doc)
        return "orphaned_copy"

    doc.source_kind = SOURCE_INLINE
    _fill_from_body(doc, chunker)
    _touch(doc)
    return "inline"
