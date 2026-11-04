"""源文件正文：清洗后的文本、摘要，以及切块在其中的字符定位。

绑定文档的整篇正文不进 SQLite。这里的结果只在摄入、迁移和扩写时现算。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Chunk, ChunkLocation
from .chunker import Chunker
from .cleaner import Cleaner
from .loaders import load_file

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


def locations_for_text(doc_id: str, text: str, chunker: Chunker) -> list[ChunkLocation]:
    """用当前切块器在文本上重算定位（迁移时向量不必重嵌）。"""
    if not text.strip():
        return []
    return locate_chunks(text, chunker.chunk(doc_id, text))
