"""检索器：查询嵌入 → Chroma top-N（支持 metadata 过滤），产出 RetrievalHit。"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..models import MediaRef, RetrievalHit


class Retriever:
    def __init__(self, embedder, vector_store, settings: Settings) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.settings = settings

    def search(
        self,
        query: str,
        top_k: int,
        where: dict[str, Any] | None = None,
        collection: str | None = None,
    ) -> list[RetrievalHit]:
        collection = collection or self.settings.default_collection
        qv = self.embedder.embed_query(query)
        results = self.vector_store.query(collection, qv, top_k, where)
        hits: list[RetrievalHit] = []
        for r in results:
            meta: dict = r.get("metadata") or {}
            media = MediaRef.from_metadata(meta)
            hits.append(
                RetrievalHit(
                    chunk_id=r.get("id", ""),
                    doc_id=meta.get("doc_id", ""),
                    text=r.get("document", ""),
                    score=float(r.get("score", 0.0)),
                    title=meta.get("title", ""),
                    section=meta.get("section", ""),
                    source_path=meta.get("source_path", ""),
                    media=media,
                    collection=collection,
                )
            )
        return hits
