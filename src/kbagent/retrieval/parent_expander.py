"""父文档扩写：子 chunk 命中 → 按 doc_id 聚合取父文档全文（子母文档回查）。

超长父文档按命中片段取窗口；同一 doc 只扩写一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import MediaRef, RetrievalHit


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
        doc_store,
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
            if doc and len(doc.full_text) <= self.max_parent_chars:
                text = doc.full_text
            elif doc:
                text = self._window(doc.full_text, hit.text)
            else:
                text = hit.text
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

    def _window(self, full_text: str, chunk_text: str) -> str:
        """在父文档中定位命中片段，取 max_parent_chars 窗口。"""
        probe = chunk_text[:50].strip()
        idx = full_text.find(probe)
        if idx == -1:
            return full_text[: self.max_parent_chars]
        half = self.max_parent_chars // 2
        start = max(0, idx - half)
        return full_text[start : start + self.max_parent_chars]
