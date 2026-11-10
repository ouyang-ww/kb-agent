"""向量存储：ChromaDB 适配器（实现 VectorStore Protocol）。

业务层只依赖 :mod:`kbagent.vectorstore.base`，本模块负责：
- 把后端中立 Filter 编译为 Chroma where
- 把距离统一换算成 0–1 相关度分数
- 暴露原始 distance / metric，避免上层绑死某一种度量
- 其他进程改写同一目录后，丢掉过期的内存索引并重新打开客户端
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from ..errors import KBError
from ..storage.filters import Where, compile_chroma
from .base import StoredVector, VectorHit, VectorStore, distance_to_score

__all__ = ["ChromaStore", "StoredVector", "VectorHit", "VectorStore"]

logger = logging.getLogger(__name__)

# 放在 Chroma 目录旁边，避免被当成集合数据。repair 写入它，其他进程据此重开客户端。
_GENERATION_SUFFIX = ".generation"

STALE_CHROMA_RELOAD_WARNING = (
    "Chroma 目录 %s 已在本进程之外被改写，内存中的向量索引已过期，正在重新打开客户端。"
    "若这次检索仍然为空，请重启本进程。"
)

SHARED_CHROMA_REPAIR_NOTICE = (
    "kb repair 已更新共享向量库 {path}。"
    "其他仍在运行的进程会在下一次检索时重新打开 Chroma；"
    "如果那些进程检索仍然为空，请重启它们。"
    "Chroma 不保证多个进程同时写入同一目录是安全的。"
)


def _embedding_list(raw: Any) -> list[float] | None:
    """把 Chroma 返回的 ndarray / 序列收成普通 float 列表。"""
    if raw is None:
        return None
    try:
        values = [float(x) for x in raw]
    except TypeError:
        return None
    return values or None


def _fingerprint(root: Path, generation: Path) -> tuple:
    """目录签名。其他进程的写入或 repair 标记都会让它变化；纯读取不会。"""
    parts: list[tuple] = []
    try:
        if generation.is_file():
            parts.append(("generation", generation.read_text(encoding="utf-8").strip()))
    except OSError:
        parts.append(("generation", "<unreadable>"))
    if not root.exists():
        return tuple(parts)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append((str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size))
    return tuple(parts)


class ChromaStore:
    """ChromaDB embedded 持久化实现。"""

    def __init__(self, persist_dir: str | Any, metric: str = "l2") -> None:
        try:
            import chromadb  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise KBError("缺少 chromadb，请先安装: pip install chromadb") from exc
        metric = metric.strip().lower()
        if metric not in {"l2", "cosine", "ip"}:
            raise KBError(f"不支持的向量度量: {metric}")
        self._metric = metric
        self._path = str(persist_dir)
        self._generation = Path(self._path).parent / (
            Path(self._path).name + _GENERATION_SUFFIX
        )
        self._lock = threading.RLock()
        self._client: Any = None
        self._collections: dict[str, Any] = {}
        self._stamp: tuple | None = None
        self._open_client()

    def _open_client(self) -> None:
        import chromadb

        Path(self._path).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=self._path)
        self._collections = {}
        # 打开客户端本身可能落盘，签名要在打开之后取。
        self._stamp = _fingerprint(Path(self._path), self._generation)

    def _release_client(self, *, force: bool = False) -> None:
        client = self._client
        self._client = None
        self._collections = {}
        if client is None:
            return
        closer = getattr(client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.warning("关闭 Chroma 客户端失败，尝试丢弃缓存的系统", exc_info=True)
        # 重新打开时必须把共享系统停掉，否则新的 PersistentClient 会复用过期索引。
        # 普通 close 则留给同进程里仍持有该目录的其他客户端。
        _drop_shared_system(client, force=force or not callable(closer))

    def reload(self) -> None:
        """丢掉内存中的 HNSW 索引，按磁盘上的最新数据重新打开。"""
        logger.warning(STALE_CHROMA_RELOAD_WARNING, self._path)
        self._release_client(force=True)
        self._open_client()

    def mark_shared_revision(self) -> str:
        """repair 写完后留下标记，让已经打开的其他进程在下次检索时重开客户端。"""
        with self._lock:
            self._generation.parent.mkdir(parents=True, exist_ok=True)
            token = str(_now_ns())
            self._generation.write_text(token + "\n", encoding="utf-8")
            # 本进程刚写完，内存索引就是新的，只更新签名，不要再重开一次。
            self._stamp = _fingerprint(Path(self._path), self._generation)
            return SHARED_CHROMA_REPAIR_NOTICE.format(path=self._path)

    def _ensure_current(self) -> None:
        if self._client is None:
            self._open_client()
            return
        current = _fingerprint(Path(self._path), self._generation)
        if current != self._stamp:
            self.reload()

    def _remember(self) -> None:
        self._stamp = _fingerprint(Path(self._path), self._generation)

    def _coll(self, name: str):
        self._ensure_current()
        client = self._client
        if client is None:
            raise KBError("Chroma 客户端未打开")
        coll = self._collections.get(name)
        if coll is None:
            existing = {c.name for c in client.list_collections()}
            if name in existing:
                # 已有集合：以集合自身记录的度量方式为准，避免误改索引
                coll = client.get_collection(name)
            elif self._metric == "l2":
                coll = client.create_collection(name)
            else:
                coll = client.create_collection(
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
        with self._lock:
            self._coll(collection).add(
                ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas
            )
            self._remember()

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        top_k: int,
        where: Where = None,
    ) -> list[VectorHit]:
        with self._lock:
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
            document = docs[i] if i < len(docs) and docs[i] is not None else ""
            out.append(
                VectorHit(
                    id=ids[i],
                    document=document,
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
            with self._lock:
                self._coll(collection).delete(**kwargs)
                self._remember()

    def get(
        self,
        collection: str,
        ids: list[str] | None = None,
        where: Where = None,
        limit: int | None = None,
        *,
        include_embeddings: bool = False,
    ) -> list[StoredVector]:
        kwargs: dict[str, Any] = {}
        if ids:
            kwargs["ids"] = ids
        chroma_where = compile_chroma(where)
        if chroma_where:
            kwargs["where"] = chroma_where
        if limit is not None:
            kwargs["limit"] = limit
        if include_embeddings:
            # 默认 get 不带向量；只有替换回滚才需要把嵌入读回来。
            kwargs["include"] = ["documents", "metadatas", "embeddings"]
        with self._lock:
            res = self._coll(collection).get(**kwargs)
        ids_l = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        raw_embs = res.get("embeddings") if include_embeddings else None
        return [
            StoredVector(
                id=ids_l[i],
                document=docs[i] if i < len(docs) and docs[i] is not None else "",
                metadata=(metas[i] or {}) if i < len(metas) else {},
                embedding=_embedding_list(raw_embs[i])
                if raw_embs is not None and i < len(raw_embs)
                else None,
            )
            for i in range(len(ids_l))
        ]

    def count(self, collection: str) -> int:
        with self._lock:
            return self._coll(collection).count()

    def collections(self) -> list[str]:
        with self._lock:
            self._ensure_current()
            client = self._client
            assert client is not None
            return [c.name for c in client.list_collections()]

    def close(self) -> None:
        with self._lock:
            self._release_client()
            self._stamp = None


def _now_ns() -> int:
    from time import time_ns

    return time_ns()


def _drop_shared_system(client: Any, *, force: bool) -> None:
    """close() 没丢掉共享系统时，按路径把它停掉。

    同一进程里后创建的 PersistentClient 会复用这个系统，于是继续拿着
    另一个进程写完之前加载的 HNSW。查询会返回已经删除的 id，默认检索因此变空。
    """
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
    except Exception:
        return
    ident = getattr(client, "_identifier", None)
    if not ident:
        return
    if not force and SharedSystemClient._identifier_to_refcount.get(ident, 0) > 0:
        return
    system = SharedSystemClient._identifier_to_system.pop(ident, None)
    SharedSystemClient._identifier_to_refcount.pop(ident, None)
    if system is None:
        return
    try:
        system.stop()
    except Exception:
        logger.warning("停止过期的 Chroma 系统失败", exc_info=True)
