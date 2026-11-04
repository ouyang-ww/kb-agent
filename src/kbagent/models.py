"""核心数据模型：父文档（Document）、子 chunk（Chunk）、检索命中、答案等。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MediaRef:
    """图片等媒体引用。"""

    media_type: str = "image"
    image_path: str = ""
    page: int | None = None
    caption: str = ""
    original_path: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return {
            "media_type": self.media_type,
            "image_path": self.image_path,
            "page": self.page if self.page is not None else -1,
            "caption": self.caption,
            "original_path": self.original_path,
        }

    @classmethod
    def from_metadata(cls, meta: dict[str, Any]) -> MediaRef | None:
        if meta.get("media_type") != "image":
            return None
        return cls(
            media_type=meta.get("media_type", "image"),
            image_path=meta.get("image_path", ""),
            page=meta.get("page") if meta.get("page") not in (None, -1) else None,
            caption=meta.get("caption", ""),
            original_path=meta.get("original_path", ""),
        )


# 原文放在哪。见 ADR 0002。
SOURCE_BOUND = "bound"  # Vault / 文件夹；正文只在源文件
SOURCE_INLINE = "inline"  # 无外部原件，正文留在应用库
SOURCE_ORPHANED = "orphaned_copy"  # 源文件已不在，库内副本保留待用户处理
SOURCE_LEGACY = "legacy"  # 迁移前的旧行，仍可能带父全文
SOURCE_KINDS = frozenset(
    {SOURCE_BOUND, SOURCE_INLINE, SOURCE_ORPHANED, SOURCE_LEGACY}
)


@dataclass
class ChunkLocation:
    """切块在清洗后源文本中的字符区间（含 start，不含 end）。"""

    chunk_id: str
    start: int
    end: int
    section: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "start": self.start,
            "end": self.end,
            "section": self.section,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChunkLocation:
        return cls(
            chunk_id=str(data.get("chunk_id", "")),
            start=int(data.get("start", -1)),
            end=int(data.get("end", -1)),
            section=str(data.get("section", "")),
        )


@dataclass
class Document:
    """父文档记录。

    ``bound``：不保存整篇正文，扩写时按路径回读源文件。
    ``inline``：无外部原件，正文留在应用库。
    ``orphaned_copy``：源文件已丢失，库内副本保留到用户处理。
    """

    doc_id: str
    title: str
    full_text: str
    source_path: str = ""
    doc_type: str = "text"
    content_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    collection: str = "personal"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    chunk_ids: list[str] = field(default_factory=list)
    media: list[MediaRef] = field(default_factory=list)
    scope: str = "default"
    status: str = "active"
    summary: str = ""
    source_kind: str = SOURCE_INLINE
    chunk_locations: list[ChunkLocation] = field(default_factory=list)

    @property
    def is_hidden(self) -> bool:
        from .governance.scope import HIDDEN_SCOPES

        return self.status != "active" or self.scope in HIDDEN_SCOPES

    @property
    def chunk_count(self) -> int:
        return len(self.chunk_ids)

    @property
    def media_count(self) -> int:
        return len(self.media)


@dataclass
class Chunk:
    """子 chunk：存 Chroma 向量库，携带父文档 doc_id 与 section。"""

    chunk_id: str
    doc_id: str
    text: str
    section: str = ""
    index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    media: MediaRef | None = None


@dataclass
class RetrievalHit:
    """检索命中项（子 chunk 级）。"""

    chunk_id: str
    doc_id: str
    text: str
    score: float
    title: str = ""
    section: str = ""
    source_path: str = ""
    media: MediaRef | None = None
    collection: str = ""
    char_start: int | None = None
    char_end: int | None = None


@dataclass
class SourceRef:
    """答案引用来源（文档级）。"""

    doc_id: str
    title: str = ""
    source_path: str = ""
    score: float = 0.0
    snippet: str = ""
    section: str = ""
    media: MediaRef | None = None


@dataclass
class Answer:
    """RAG 答案：正文 + 来源 + 过程元信息。"""

    text: str
    sources: list[SourceRef] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryPlan:
    """意图识别 + 查询改写的结果。"""

    intent: str  # kb_question | smalltalk | management | out_of_scope
    rewritten_query: str
    original_query: str = ""
