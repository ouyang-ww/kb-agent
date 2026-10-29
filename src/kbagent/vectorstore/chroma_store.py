"""向量存储：ChromaDB 适配器（实现 VectorStore Protocol）。

业务层只依赖 :mod:`kbagent.vectorstore.base`，本模块负责：
- 把后端中立 Filter 编译为 Chroma where
- 把距离统一换算成 0–1 相关度分数
- 暴露原始 distance / metric，避免上层绑死某一种度量
"""

from __future__ import annotations

from typing import Any

from ..errors import KBError
from ..storage.filters import Where, compile_chroma
from .base import StoredVector, VectorHit, VectorStore, distance_to_score

__all__ = ["ChromaStore", "StoredVector", "VectorHit", "VectorStore"]


class ChromaStore:
    """ChromaDB embedded 持久化实现。"""

    def __init__(self, persist_dir: str | Any, metric: str = "l2") -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover
            raise KBError("缺少 chromadb，请先安装: pip install chromadb") from exc
        metric = metric.strip().lower()
        if metric not in {"l2", "cosine", "ip"}:
            raise KBError(f"不支持的向量度量: {metric}")
        self._metric = metric
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collections: dict[str, Any] = {}

    def _coll(self, name: str):
        coll = self._collections.get(name)
        if coll is None:
            existing = {c.name for c in self._client.list_collections()}
            if name in existing:
                # 已有集合：以集合自身记录的度量方式为准，避免误改索引
                coll = self._client.get_collection(name)
            elif self._metric == "l2":
                coll = self._client.create_collection(name)
            else:
                coll = self._client.create_collection(
                    name, metadata={"hnsw:space": self._metric}
                )
            self._collections[name] = coll
        return coll

    @staticmethod
    def _metric_of(coll) -> str:
        # 旧集合没有 hnsw:space 元数据，Chroma 默认使用 l2
        meta = getattr(coll, "metadata", None) or {}
        return str(meta.get("hnsw:space", "l2"))

    def add(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        if not ids:
            return
        self._coll(collection).add(
            ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas
        )

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        top_k: int,
        where: Where = None,
    ) -> list[VectorHit]:
        coll = self._coll(collection)
        metric = self._metric_of(coll)
        res = coll.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=compile_chroma(where),
            include=["documents", "metadatas", "distances"],
        )
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[VectorHit] = []
        for i in range(len(ids)):
            distance = float(dists[i]) if i < len(dists) else 1.0
            out.append(
                VectorHit(
                    id=ids[i],
                    document=docs[i] if i < len(docs) else "",
                    metadata=(metas[i] or {}) if i < len(metas) else {},
                    distance=distance,
                    score=distance_to_score(distance, metric),
                    metric=metric,
                )
            )
        return out

    def delete(
        self,
        collection: str,
        ids: list[str] | None = None,
        where: Where = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if ids:
            kwargs["ids"] = ids
        chroma_where = compile_chroma(where)
        if chroma_where:
            kwargs["where"] = chroma_where
        if kwargs:
            self._coll(collection).delete(**kwargs)

    def get(
        self,
        collection: str,
        ids: list[str] | None = None,
        where: Where = None,
        limit: int | None = None,
    ) -> list[StoredVector]:
        kwargs: dict[str, Any] = {}
        if ids:
            kwargs["ids"] = ids
        chroma_where = compile_chroma(where)
        if chroma_where:
            kwargs["where"] = chroma_where
        if limit is not None:
            kwargs["limit"] = limit
        res = self._coll(collection).get(**kwargs)
        ids_l = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        return [
            StoredVector(
                id=ids_l[i],
                document=docs[i] if i < len(docs) else "",
                metadata=(metas[i] or {}) if i < len(metas) else {},
            )
            for i in range(len(ids_l))
        ]

    def count(self, collection: str) -> int:
        return self._coll(collection).count()

    def collections(self) -> list[str]:
        return [c.name for c in self._client.list_collections()]

    def close(self) -> None:
        # Chroma PersistentClient 没有显式 close；清掉缓存引用即可。
        self._collections.clear()
