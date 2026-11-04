"""源文件正文：清洗后的文本、摘要，以及切块在其中的字符定位。

绑定文档的整篇正文不进 SQLite。这里的结果只在摄入、迁移和扩写时现算。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Chunk, ChunkLocation
from .cleaner import Cleaner
from .loaders import content_hash, load_file

# 摘要是派生摘录，不是整篇原件。短笔记可以整段落入摘要。
SUMMARY_MAX_CHARS = 240


def summarize(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """压缩空白后截取摘要。不超过 ``limit`` 个字符。"""
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def read_cleaned_source(path: str | Path, cleaner: Cleaner | None = None) -> str:
    """按与摄入相同的加载器和清洗器读源文件，返回清洗后文本。"""
    raws = load_file(Path(path))
    if not raws:
        return ""
    text = raws[0].get("content") or ""
    return (cleaner or Cleaner()).clean(text).text


def locate_chunks(text: str, chunks: list[Chunk]) -> list[ChunkLocation]:
    """在清洗后文本中为每个切块记录字符区间。找不到时 start/end 为 -1。"""
    cursor = 0
    locations: list[ChunkLocation] = []
    for chunk in chunks:
        piece = chunk.text or ""
        start = -1
        end = -1
        if piece:
            idx = text.find(piece, cursor)
            if idx < 0:
                idx = text.find(piece)
            if idx >= 0:
                start = idx
                end = idx + len(piece)
                # 重叠切块的下一段起点可能落在本段内部
                cursor = idx + 1
        locations.append(
            ChunkLocation(
                chunk_id=chunk.chunk_id,
                start=start,
                end=end,
                section=chunk.section,
            )
        )
    return locations


def read_verified_source(path: str | Path, expected_hash: str) -> str | None:
    """回读并清洗源文件。只有内容哈希与 ``expected_hash`` 一致时才返回文本。

    空哈希不算通过：无法证明磁盘上的文件仍是入库时那一版，调用方应退回切块文本。
    """
    if not (expected_hash or "").strip():
        return None
    cleaned = read_cleaned_source(path)
    if content_hash(cleaned) != expected_hash:
        return None
    return cleaned


def _record_id(record: object) -> str:
    return str(getattr(record, "id", None) or getattr(record, "chunk_id", "") or "")


def _record_text(record: object) -> str:
    document = getattr(record, "document", None)
    if document is None:
        document = getattr(record, "text", "") or ""
    return str(document or "")


def _record_section(record: object) -> str:
    meta = getattr(record, "metadata", None) or {}
    if isinstance(meta, dict) and meta.get("section"):
        return str(meta["section"])
    return str(getattr(record, "section", "") or "")


def _stored_span(record: object, text: str, piece: str) -> tuple[int, int] | None:
    """已写入向量元数据的偏移只有在能切回同一段文本时才采用。"""
    meta = getattr(record, "metadata", None) or {}
    if not isinstance(meta, dict) or not piece:
        return None
    try:
        start = int(meta["char_start"])
        end = int(meta["char_end"])
    except (KeyError, TypeError, ValueError):
        return None
    if 0 <= start < end <= len(text) and text[start:end] == piece:
        return start, end
    return None


def locations_from_records(text: str, records: list) -> list[ChunkLocation]:
    """用已经入库的切块文本（及其存储偏移）在正文里定位。

    不重新切块，避免和现有向量错位。
    """
    cursor = 0
    locations: list[ChunkLocation] = []
    for record in sorted(records, key=_record_id):
        piece = _record_text(record)
        section = _record_section(record)
        span = _stored_span(record, text, piece)
        if span is None and piece:
            idx = text.find(piece, cursor)
            if idx < 0:
                idx = text.find(piece)
            if idx >= 0:
                span = (idx, idx + len(piece))
        if span is None:
            locations.append(ChunkLocation(_record_id(record), -1, -1, section))
            continue
        start, end = span
        cursor = start + 1
        locations.append(ChunkLocation(_record_id(record), start, end, section))
    return locations


def align_locations(
    text: str,
    records: list | None,
    existing: list[ChunkLocation],
) -> list[ChunkLocation]:
    """优先采用能对上已存切块的定位；对不上时保留原定位，不另切一版。"""
    derived = locations_from_records(text, records) if records else []
    if any(loc.start >= 0 for loc in derived):
        return derived
    return existing
