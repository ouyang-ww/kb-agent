"""切块器：markdown 标题感知 + 中英文递归切块（子 chunk 挂 doc_id 与 section）。"""

from __future__ import annotations

import re

from ..models import Chunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 递归切块分隔符优先级（中文标点优先）
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", " ", ""]


class Chunker:
    def __init__(self, chunk_size: int = 800, overlap: int = 100) -> None:
        self.chunk_size = max(64, chunk_size)
        self.overlap = max(0, min(overlap, self.chunk_size // 2))

    def chunk(self, doc_id: str, text: str, base_meta: dict | None = None) -> list[Chunk]:
        sections = self._split_sections(text)
        chunks: list[Chunk] = []
        idx = 0
        for section_path, body in sections:
            for piece in self._recursive_split(body):
                if not piece.strip():
                    continue
                meta = dict(base_meta or {})
                meta.update({"section": section_path, "chunk_index": idx})
                chunks.append(
                    Chunk(
                        chunk_id=f"{doc_id}#{idx:04d}",
                        doc_id=doc_id,
                        text=piece,
                        section=section_path,
                        index=idx,
                        metadata=meta,
                    )
                )
                idx += 1
        return chunks

    def _split_sections(self, text: str) -> list[tuple[str, str]]:
        """按 markdown 标题切分，返回 [(section_path, body)]。"""
        sections: list[tuple[str, str]] = []
        path: list[str] = []
        body: list[str] = []
        for ln in text.split("\n"):
            m = _HEADING_RE.match(ln)
            if m:
                if body or sections:
                    sections.append((" / ".join(path), "\n".join(body).strip()))
                level = len(m.group(1))
                path = path[: level - 1] + [m.group(2).strip()]
                body = []
            else:
                body.append(ln)
        if body or not sections:
            sections.append((" / ".join(path), "\n".join(body).strip()))
        return sections

    def _recursive_split(self, text: str, depth: int = 0) -> list[str]:
        text = text.strip("\n")
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]
        if depth > 8:
            return [
                text[i : i + self.chunk_size]
                for i in range(0, len(text), self.chunk_size)
            ]
        for sep in _SEPARATORS:
            if sep and sep not in text:
                continue
            if not sep:
                break
            idx = text.rfind(sep, 0, self.chunk_size)
            if idx == -1:
                continue
            left, right = text[: idx + len(sep)], text[idx + len(sep) :]
            if self.overlap and right:
                right = left[-self.overlap :] + right
            return self._recursive_split(left, depth + 1) + self._recursive_split(
                right, depth + 1
            )
        return [text[: self.chunk_size]] + self._recursive_split(
            text[self.chunk_size :], depth + 1
        )
