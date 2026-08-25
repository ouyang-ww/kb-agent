"""向量存储：协议 + ChromaDB 实现（存子 chunk 与向量）。"""

from __future__ import annotations

from typing import Any, Protocol

from ..errors import KBError


class VectorStore(Protocol):
    def add(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None: ...

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def delete(
        self,
        collection: str,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> None: ...

    def get(
        self,
        collection: str,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def count(self, collection: str) -> int: ...

    def collections(self) -> list[str]: ...


class ChromaStore:
    """ChromaDB embedded 持久化实现。"""

    def __init__(self, persist_dir: str | Any) -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover
            raise KBError("缺少 chromadb，请先安装: pip install chromadb") from exc
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collections: dict[str, Any] = {}

    def _coll(self, name: str):
        if name not in self._collections:
            self._collections[name] = self._client.get_or_create_collection(name)
        return self._collections[name]

    def add(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        self._coll(collection).add(
            ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas
        )

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        res = self._coll(collection).query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[dict[str, Any]] = []
        for i in range(len(ids)):
            d = dists[i] if i < len(dists) else 1.0
            out.append(
                {
                    "id": ids[i],
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] or {} if i < len(metas) else {},
                    "distance": d,
                    "score": 1.0 / (1.0 + float(d)),  # L2 距离 → 相似度
                }
            )
        return out

    def delete(
        self,
        collection: str,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if ids:
            kwargs["ids"] = ids
        if where:
            kwargs["where"] = where
        if kwargs:
            self._coll(collection).delete(**kwargs)

    def get(
        self,
        collection: str,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if ids:
            kwargs["ids"] = ids
        if where:
            kwargs["where"] = where
        if limit is not None:
            kwargs["limit"] = limit
        res = self._coll(collection).get(**kwargs)
        ids_l = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        out: list[dict[str, Any]] = []
        for i in range(len(ids_l)):
            out.append(
                {
                    "id": ids_l[i],
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] or {} if i < len(metas) else {},
                }
            )
        return out

    def count(self, collection: str) -> int:
        return self._coll(collection).count()

    def collections(self) -> list[str]:
        return [c.name for c in self._client.list_collections()]
