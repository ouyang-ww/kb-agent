"""父文档扩写：子 chunk 命中后取父文档上下文。

绑定 Vault / 文件夹的文档按路径回读源文件，再按切块定位截取。
不从 SQLite 读取这类文档的整篇正文。``max_parent_chars``（``KB_MAX_PARENT_CHARS``）
只限制返回窗口；回读本身会重新清洗整篇源文件。

回读前核对 ``content_hash``。哈希为空或不一致时，不把磁盘上的新文本送进提示词，
退回本次命中的切块文本（与文件缺失时相同）。``diverged`` / ``orphaned_copy`` /
``inline`` 仍使用库内正文，因为那份正文才是向量对应的内容。

回读源文件是 ADR 0002 的有意变更。Recall / Accuracy / Faithfulness 继续衡量
检索与回答，不把「父全文不再来自 SQLite」记成 L1 回归失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import IngestError
from ..ingestion.source_text import read_verified_source
from ..models import SOURCE_BOUND, ChunkLocation, MediaRef, RetrievalHit
from .source_window import slice_parent_window

if TYPE_CHECKING:
    from ..document_store.base import DocumentStore


@dataclass
class ContextItem:
    """送入提示词的上下文单元。"""

    doc_id: str
    text: str
    title: str = ""
    source_path: str = ""
    section: str = ""
    score: float = 0.0
    chunk_id: str = ""
    media: MediaRef | None = field(default=None)


class ParentExpander:
    def __init__(
        self,
        doc_store: "DocumentStore",
        max_parent_chars: int = 4000,
        mode: str = "full",
    ) -> None:
        self.doc_store = doc_store
        self.max_parent_chars = max_parent_chars
        self.mode = mode  # full | section

    def expand(self, hits: list[RetrievalHit]) -> list[ContextItem]:
        items: list[ContextItem] = []
        seen: set[str] = set()
        for hit in sorted(hits, key=lambda h: -h.score):
            if hit.doc_id in seen:
                continue
            seen.add(hit.doc_id)
            doc = self.doc_store.get(hit.doc_id)
            text = self._parent_text(doc, hit)
            items.append(
                ContextItem(
                    doc_id=hit.doc_id,
                    text=text,
                    title=doc.title if doc else hit.title,
                    source_path=(doc.source_path if doc else hit.source_path),
                    section=hit.section,
                    score=hit.score,
                    chunk_id=hit.chunk_id,
                    media=hit.media,
                )
            )
        return items

    def _parent_text(self, doc, hit: RetrievalHit) -> str:
        if doc is None:
            return hit.text
        start, end = self._span(doc, hit)
        if doc.source_kind == SOURCE_BOUND:
            return self._from_source(doc, hit, start, end)
        if not doc.full_text:
            return hit.text
        return slice_parent_window(
            doc.full_text,
            start=start,
            end=end,
            max_chars=self.max_parent_chars,
            probe=hit.text,
        )

    def _from_source(self, doc, hit: RetrievalHit, start: int | None, end: int | None) -> str:
        path = doc.source_path or ""
        if not path or not Path(path).is_file():
            return hit.text
        try:
            cleaned = read_verified_source(path, doc.content_hash or "")
        except (OSError, IngestError):
            return hit.text
        if cleaned is None:
            return hit.text
        return slice_parent_window(
            cleaned,
            start=start,
            end=end,
            max_chars=self.max_parent_chars,
            probe=hit.text,
        )

    @staticmethod
    def _span(doc, hit: RetrievalHit) -> tuple[int | None, int | None]:
        if (
            hit.char_start is not None
            and hit.char_end is not None
            and hit.char_start >= 0
            and hit.char_end > hit.char_start
        ):
            return hit.char_start, hit.char_end
        locations: list[ChunkLocation] = getattr(doc, "chunk_locations", []) or []
        for loc in locations:
            if (
                loc.chunk_id == hit.chunk_id
                and loc.start >= 0
                and loc.end > loc.start
            ):
                return loc.start, loc.end
        return None, None
