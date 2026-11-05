"""把已入库的父全文迁出应用库。见 ADR 0002。

只有磁盘内容的哈希与已入库哈希一致时，才删除库内正文并标为 ``bound``。
没有存过哈希、但库内正文和磁盘清洗结果一致时，补上这份哈希再删除正文。
两边都没有可保护的正文时，才采用当前文件的哈希（库里没有另一份可丢的原文）。

哈希不一致时保留库内正文，标为 ``diverged`` 而不是 ``orphaned_copy``：
文件还在，只是已经不是入库时那一版。``orphaned_copy`` 只表示源文件不在。
``diverged`` 需要重新入库，让切块重新对齐源文件。

切块定位只从已有向量切块（或其存储偏移）对齐，不按当前切块器重切。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..errors import IngestError
from ..ingestion.loaders import content_hash, hashes_equal, is_legacy_hash
from ..ingestion.source_text import align_locations, read_cleaned_source, summarize
from ..models import (
    SOURCE_BOUND,
    SOURCE_DIVERGED,
    SOURCE_INLINE,
    SOURCE_ORPHANED,
    ChunkLocation,
    Document,
)

RE_INGEST_HINT = (
    "{n} 篇文档的源文件与已入库哈希不一致（source_kind=diverged）。"
    "库内正文已保留。请重新入库这些路径，使切块与源文件重新对齐。"
)


def _touch(doc: Document) -> None:
    doc.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _locations(
    text: str,
    chunks: list | None,
    existing: list[ChunkLocation],
) -> list[ChunkLocation]:
    if not text:
        return existing
    return align_locations(text, chunks, existing)


def _mark_bound(
    doc: Document,
    cleaned: str,
    digest: str,
    chunks: list | None,
) -> str:
    locations = _locations(cleaned, chunks, doc.chunk_locations)
    summary = summarize(cleaned) or doc.summary
    if (
        doc.source_kind == SOURCE_BOUND
        and not doc.full_text
        and doc.content_hash == digest
        and doc.summary == summary
        and doc.chunk_locations == locations
    ):
        return "unchanged"
    doc.content_hash = digest
    doc.summary = summary
    doc.chunk_locations = locations
    doc.full_text = ""
    doc.source_kind = SOURCE_BOUND
    _touch(doc)
    return "bound"


def _mark_diverged(
    doc: Document,
    body: str,
    digest: str,
    chunks: list | None,
) -> str:
    """保留与向量一致的库内正文。定位对着这篇正文，不对着已漂移的文件。"""
    locations = _locations(body, chunks, doc.chunk_locations) if body else list(
        doc.chunk_locations
    )
    summary = doc.summary or (summarize(body) if body else "")
    if (
        doc.source_kind == SOURCE_DIVERGED
        and doc.full_text == body
        and doc.content_hash == digest
        and doc.summary == summary
        and doc.chunk_locations == locations
    ):
        return "unchanged"
    doc.full_text = body
    doc.content_hash = digest
    doc.summary = summary
    doc.chunk_locations = locations
    doc.source_kind = SOURCE_DIVERGED
    _touch(doc)
    return "diverged"


def _mark_orphaned(doc: Document, chunks: list | None) -> str:
    if doc.source_kind == SOURCE_ORPHANED and doc.full_text:
        if _upgrade_legacy_hash(doc, doc.full_text):
            return "hash_upgraded"
        return "unchanged"
    summary = doc.summary or (summarize(doc.full_text) if doc.full_text else "")
    locations = (
        _locations(doc.full_text, chunks, doc.chunk_locations)
        if doc.full_text
        else list(doc.chunk_locations)
    )
    if (
        doc.source_kind == SOURCE_ORPHANED
        and doc.summary == summary
        and doc.chunk_locations == locations
    ):
        return "unchanged"
    doc.summary = summary
    doc.chunk_locations = locations
    doc.source_kind = SOURCE_ORPHANED
    _touch(doc)
    _upgrade_legacy_hash(doc, doc.full_text)
    return "orphaned_copy"


def _mark_inline(doc: Document, chunks: list | None) -> str:
    if doc.source_kind == SOURCE_INLINE and not (doc.source_path or "").strip():
        if _upgrade_legacy_hash(doc, doc.full_text):
            return "hash_upgraded"
        return "unchanged"
    summary = doc.summary or (summarize(doc.full_text) if doc.full_text else "")
    locations = (
        _locations(doc.full_text, chunks, doc.chunk_locations)
        if doc.full_text
        else list(doc.chunk_locations)
    )
    doc.source_kind = SOURCE_INLINE
    doc.summary = summary
    doc.chunk_locations = locations
    doc.source_path = ""
    _touch(doc)
    _upgrade_legacy_hash(doc, doc.full_text)
    return "inline"


def _upgrade_legacy_hash(doc: Document, text: str) -> bool:
    """旧 12 hex 若确是 ``text`` 的前缀，就地写成完整 SHA256。

    对不上不改：不能把另一篇正文的哈希安到这条记录上。
    """
    stored = (doc.content_hash or "").strip()
    if not is_legacy_hash(stored) or not (text or "").strip():
        return False
    full = content_hash(text)
    if not hashes_equal(stored, full):
        return False
    doc.content_hash = full
    _touch(doc)
    return True


def migrate_document(doc: Document, chunks: list | None = None) -> str:
    """就地更新 ``doc``。

    返回 ``bound`` / ``inline`` / ``orphaned_copy`` / ``diverged`` /
    ``hash_upgraded`` / ``unchanged``。
    ``chunks`` 是向量库里已经存在的切块，用来对齐定位，不会触发重新切块。
    """
    path = (doc.source_path or "").strip()
    exists = bool(path) and Path(path).is_file()
    body = doc.full_text or ""

    # 已经是绑定且没有库内正文：文件随后漂移由扩写拒绝，迁移补不回正文。
    # 仍可以把旧的 12 hex 升成完整哈希（对得上当前文件时）。
    if doc.source_kind == SOURCE_BOUND and not body:
        if exists and is_legacy_hash(doc.content_hash):
            try:
                cleaned = read_cleaned_source(path)
            except (OSError, IngestError):
                return "unchanged"
            if _upgrade_legacy_hash(doc, cleaned):
                return "hash_upgraded"
        return "unchanged"
    if doc.source_kind == SOURCE_INLINE and not path:
        if _upgrade_legacy_hash(doc, body):
            return "hash_upgraded"
        return "unchanged"

    if not exists:
        if not path:
            return _mark_inline(doc, chunks)
        if not body:
            return "unchanged"
        return _mark_orphaned(doc, chunks)

    try:
        cleaned = read_cleaned_source(path)
    except (OSError, IngestError):
        if not body:
            return "unchanged"
        return _mark_orphaned(doc, chunks)

    disk_hash = content_hash(cleaned)
    stored_hash = (doc.content_hash or "").strip()
    if not stored_hash and body:
        stored_hash = content_hash(body)

    if stored_hash and hashes_equal(stored_hash, disk_hash):
        # 旧 12 hex 对得上时，库里改存完整哈希，避免以后再按截断值去重。
        return _mark_bound(doc, cleaned, disk_hash, chunks)
    # 库里没有正文、也没有哈希：没有另一份可保护的原文，采用当前文件。
    if not stored_hash and not body:
        return _mark_bound(doc, cleaned, disk_hash, chunks)
    digest = stored_hash
    if body:
        body_hash = content_hash(body)
        # 保留的是库内正文。它的旧截断哈希能对上时，一并存完整值。
        if not stored_hash or hashes_equal(stored_hash, body_hash):
            digest = body_hash
    return _mark_diverged(doc, body, digest or content_hash(body or cleaned), chunks)
