"""检索器：查询嵌入 → Chroma top-N（支持 metadata 过滤），产出 RetrievalHit。"""

from __future__ import annotations

from typing import Callable

from ..config import Settings
from ..models import MediaRef, RetrievalHit
from ..storage.filters import Where
from ..vectorstore.base import VectorStore


def _meta_offset(meta: dict, key: str) -> int | None:
    if key not in meta or meta[key] is None:
        return None
    try:
        value = int(meta[key])
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class Retriever:
    def __init__(
        self,
        embedder,
        vector_store: VectorStore,
        settings: Settings,
        doc_visibility: Callable[[str], bool] | None = None,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.settings = settings
        self.doc_visibility = doc_visibility

    def search(
        self,
        query: str,
        top_k: int,
        where: Where = None,
        collection: str | None = None,
        include_hidden: bool = False,
    ) -> list[RetrievalHit]:
        collection = collection or self.settings.default_collection
        qv = self.embedder.embed_query(query)
        fetch_k = top_k
        if not include_hidden and self.doc_visibility is not None:
            # 多取一些候选，过滤隐藏文档后再截断，降低召回损失
            fetch_k = max(top_k * 3, top_k)
        results = self.vector_store.query(collection, qv, fetch_k, where)
        if not include_hidden and self.doc_visibility is not None:
            results = [
                r for r in results
                if self.doc_visibility((r.metadata or {}).get("doc_id", ""))
            ][:top_k]
        hits: list[RetrievalHit] = []
        for r in results:
            meta: dict = r.metadata or {}
            media = MediaRef.from_metadata(meta)
            hits.append(
                RetrievalHit(
                    chunk_id=r.id,
                    doc_id=meta.get("doc_id", ""),
                    text=r.document,
                    score=float(r.score),
                    title=meta.get("title", ""),
                    section=meta.get("section", ""),
                    source_path=meta.get("source_path", ""),
                    media=media,
                    collection=collection,
                    char_start=_meta_offset(meta, "char_start"),
                    char_end=_meta_offset(meta, "char_end"),
                )
            )
        return hits
