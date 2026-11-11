"""检索器：查询嵌入 → Chroma top-N（支持 metadata 过滤），产出 RetrievalHit。"""

from __future__ import annotations

import logging
from typing import Callable

from ..config import Settings
from ..errors import KBError
from ..governance.scope import visibility_filter, visibility_is_enforced
from ..governance.validity import (
    chunk_has_valid_until,
    display_day,
    earlier_deadline,
    expiry_is_enforced,
    expiry_note,
    is_expired,
    resolve_as_of,
    today_utc,
    validity_filter,
)
from ..models import MediaRef, RetrievalHit
from ..storage.filters import Where, and_where
from ..vectorstore.base import VectorHit, VectorStore

logger = logging.getLogger(__name__)

# 第一窗沿用原来的 top_k*3。隐藏近邻占满这窗时再取到集合大小。
_HIDDEN_WINDOW_FACTOR = 3


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
        doc_dates: Callable[[str], tuple[str | None, str | None]] | None = None,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.settings = settings
        self.doc_visibility = doc_visibility
        # (valid_until, last_verified)。文档行是向量 where 之外的第二道。
        self.doc_dates = doc_dates

    def search(
        self,
        query: str,
        top_k: int,
        where: Where = None,
        collection: str | None = None,
        include_hidden: bool = False,
        include_expired: bool = False,
        as_of: str | None = None,
    ) -> list[RetrievalHit]:
        collection = collection or self.settings.default_collection
        as_of_day = resolve_as_of(as_of)
        # 不带过滤器时补上 scope/status，补不上就拒绝，不能静默搜全库。
        # include_hidden=True 是 scope/status 的显式旁路，不过期过滤仍然默认开着。
        where = self._scoped_where(
            where,
            include_hidden,
            include_expired=include_expired,
            as_of=as_of_day,
        )
        qv = self.embedder.embed_query(query)
        # 向量 where 先排除 hidden/private，避免它们占满近邻。
        # 文档行和治理元数据是第二道：切块状态过期时仍然丢掉不可见命中。
        # 近邻里还有过期隐藏块时，窗口会继续扩大，直到凑满可见结果。
        if not include_hidden and self.doc_visibility is not None and top_k > 0:
            results = self._query_visible(collection, qv, top_k, where)
        else:
            results = self.vector_store.query(collection, qv, top_k, where)
        hits: list[RetrievalHit] = []
        today = today_utc()
        for r in results:
            meta: dict = r.metadata or {}
            media = MediaRef.from_metadata(meta)
            doc_id = meta.get("doc_id", "") or ""
            if not isinstance(doc_id, str):
                doc_id = str(doc_id)
            doc_until, doc_verified = self._doc_dates(doc_id)
            if not include_expired and not chunk_has_valid_until(meta.get("valid_until")):
                # 缺字段不能当成「永不过期」。include_expired 才是显式旁路。
                continue
            until = earlier_deadline(display_day(meta.get("valid_until")), doc_until)
            verified = str(meta.get("last_verified") or doc_verified or "")
            if not include_expired and is_expired(until or doc_until, as_of_day):
                continue
            note = ""
            if is_expired(until or doc_until, today):
                note = expiry_note(until or doc_until, verified)
            try:
                authority = int(meta.get("authority", 3))
            except (TypeError, ValueError):
                authority = 3
            hits.append(
                RetrievalHit(
                    chunk_id=r.id,
                    doc_id=doc_id,
                    text=r.document,
                    score=float(r.score),
                    title=meta.get("title", ""),
                    section=meta.get("section", ""),
                    source_path=meta.get("source_path", ""),
                    media=media,
                    collection=collection,
                    char_start=_meta_offset(meta, "char_start"),
                    char_end=_meta_offset(meta, "char_end"),
                    valid_until=until or display_day(meta.get("valid_until")),
                    last_verified=verified,
                    authority=authority,
                    note=note,
                )
            )
        return hits

    def _doc_dates(self, doc_id: str) -> tuple[str | None, str | None]:
        if self.doc_dates is None or not doc_id:
            return None, None
        try:
            until, verified = self.doc_dates(doc_id)
        except Exception:
            logger.warning("读取文档 %s 的时效失败", doc_id, exc_info=True)
            return None, None
        return until, verified

    def _scoped_where(
        self,
        where: Where,
        include_hidden: bool,
        *,
        include_expired: bool = False,
        as_of: str | None = None,
    ) -> Where:
        if include_hidden:
            # scope/status 的显式旁路不把可见性写进 where。
            # 时效仍然要在，除非调用方同时给了 include_expired。
            merged = where
        else:
            merged = and_where(where, visibility_filter(self.settings.default_scope))
            if not visibility_is_enforced(merged):
                raise KBError(
                    "检索缺少 scope/status 过滤，已拒绝执行。"
                    "默认检索只接受 status=active 且 scope 在允许列表里的切块；"
                    "缺 scope 或空 scope 不会返回。只有显式 include_hidden=True 可以关闭这道过滤。"
                )
        if include_expired:
            return merged
        merged = and_where(merged, validity_filter(as_of))
        if not expiry_is_enforced(merged):
            raise KBError(
                "检索缺少 valid_until 过滤，已拒绝执行。"
                "默认检索要求切块带 valid_until，并且不早于比较日。"
                "缺这个字段不会被当成永不过期，旧切块要先跑 kb governance backfill。"
                "只有显式 include_expired=True 可以关闭这道过滤。"
            )
        return merged

    def _is_visible_hit(self, hit: VectorHit) -> bool:
        doc_id = (hit.metadata or {}).get("doc_id", "")
        if not isinstance(doc_id, str):
            doc_id = "" if doc_id is None else str(doc_id)
        checker = self.doc_visibility
        return checker is not None and checker(doc_id)

    def _visible_only(self, results: list[VectorHit], top_k: int) -> list[VectorHit]:
        kept: list[VectorHit] = []
        for hit in results:
            if self._is_visible_hit(hit):
                kept.append(hit)
                if len(kept) >= top_k:
                    break
        return kept

    def _collection_size(self, collection: str) -> int | None:
        counter = getattr(self.vector_store, "count", None)
        if counter is None:
            return None
        try:
            return max(0, int(counter(collection)))
        except Exception:
            logger.warning(
                "无法读取集合 %s 的向量数量，检索窗口改为按返回条数扩大",
                collection,
                exc_info=True,
            )
            return None

    def _query_visible(
        self,
        collection: str,
        query_embedding: list[float],
        top_k: int,
        where: Where,
    ) -> list[VectorHit]:
        total = self._collection_size(collection)
        if total == 0:
            return []
        window = max(top_k * _HIDDEN_WINDOW_FACTOR, top_k)
        if total is not None:
            window = min(window, total)
        results = self.vector_store.query(collection, query_embedding, window, where)
        visible = self._visible_only(results, top_k)
        if len(visible) >= top_k or len(results) < window:
            return visible
        if total is not None:
            if window >= total:
                return visible
            try:
                results = self.vector_store.query(
                    collection, query_embedding, total, where
                )
            except Exception:
                logger.warning(
                    "扩大检索窗口到集合大小失败，只返回当前可见结果",
                    exc_info=True,
                )
                return visible
            return self._visible_only(results, top_k)
        return self._query_visible_until_full(
            collection, query_embedding, top_k, where, window, results, visible
        )

    def _query_visible_until_full(
        self,
        collection: str,
        query_embedding: list[float],
        top_k: int,
        where: Where,
        window: int,
        results: list[VectorHit],
        visible: list[VectorHit],
    ) -> list[VectorHit]:
        """没有 count 时按 3 倍扩大，直到凑满、结果不再变多，或查询拒绝更大的 n。"""
        seen = len(results)
        while len(visible) < top_k:
            nxt = window * _HIDDEN_WINDOW_FACTOR
            if nxt <= window:
                break
            window = nxt
            try:
                results = self.vector_store.query(
                    collection, query_embedding, window, where
                )
            except Exception:
                logger.warning(
                    "扩大检索窗口失败，只返回当前可见结果", exc_info=True
                )
                break
            visible = self._visible_only(results, top_k)
            if len(results) < window or len(results) <= seen:
                break
            seen = len(results)
        return visible
