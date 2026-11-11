"""检索结果上的过期替换与冲突补齐。向量 where 是第一道，这里是第二道。

已确认的 supersedes 会把命中换成链上最新的文档。pending 关系不进这里。
已确认的 contradicts 把另一边补进候选，后面的折叠不会把它们合成一条。
"""

from __future__ import annotations

import logging

from ..governance.near_dup import DEFAULT_COSINE, DEFAULT_MMR_LAMBDA
from ..governance.validity import display_day, expiry_note, is_expired, today_utc
from ..models import MediaRef, RetrievalHit
from ..storage.filters import eq
from .diversity import ensure_conflict_sources, select_diverse

logger = logging.getLogger(__name__)


def shape_hits(
    hits: list[RetrievalHit],
    *,
    governance,
    vector_store,
    collection: str,
    embedder,
    top_k: int,
    cosine_threshold: float = DEFAULT_COSINE,
    hamming_guard: int = 8,
    mmr_lambda: float = DEFAULT_MMR_LAMBDA,
) -> tuple[list[RetrievalHit], list[RetrievalHit], set[frozenset[str]]]:
    """取代链、冲突补齐、折叠、MMR。返回 (diverse, prepared, pairs)。

    重排仍由调用方在这之后做，然后再 ``finish_hits`` 把矛盾来源补回。
    """
    prepared, pairs = prepare_candidates(
        hits,
        governance=governance,
        vector_store=vector_store,
        collection=collection,
    )
    diverse = select_diverse(
        prepared,
        embedder,
        k=top_k,
        cosine_threshold=cosine_threshold,
        hamming_guard=hamming_guard,
        mmr_lambda=mmr_lambda,
        protected_pairs=pairs,
    )
    return diverse, prepared, pairs


def finish_hits(
    hits: list[RetrievalHit],
    prepared: list[RetrievalHit],
    pairs: set[frozenset[str]],
) -> list[RetrievalHit]:
    """重排之后把被截掉的矛盾来源补回，并给已经过期的命中标上时间。"""
    finished = ensure_conflict_sources(hits, prepared, pairs)
    label_expired(finished)
    return finished


def prepare_candidates(
    hits: list[RetrievalHit],
    *,
    governance,
    vector_store,
    collection: str,
) -> tuple[list[RetrievalHit], set[frozenset[str]]]:
    """沿已确认的取代链替换，并补上已确认的矛盾来源。"""
    if governance is None or not hits:
        return hits, set()
    replaced = follow_supersedes(hits, governance, vector_store, collection)
    pairs = conflict_pairs(replaced, governance)
    attached = attach_contradictions(replaced, pairs, vector_store, collection)
    return attached, pairs


def follow_supersedes(
    hits: list[RetrievalHit],
    governance,
    vector_store,
    collection: str,
) -> list[RetrievalHit]:
    out: list[RetrievalHit] = []
    seen: set[str] = set()
    for hit in hits:
        latest = hit.doc_id
        try:
            latest = governance.latest_doc(hit.doc_id) or hit.doc_id
        except Exception:
            logger.warning("读取取代链失败，保留原命中 %s", hit.doc_id, exc_info=True)
        chosen = hit
        if latest and latest != hit.doc_id:
            replacement = representative_hit(vector_store, collection, latest, hit.score)
            if replacement is None:
                continue
            chosen = replacement
        if chosen.chunk_id in seen:
            continue
        seen.add(chosen.chunk_id)
        out.append(chosen)
    return out


def conflict_pairs(hits: list[RetrievalHit], governance) -> set[frozenset[str]]:
    pairs: set[frozenset[str]] = set()
    if governance is None:
        return pairs
    seen: set[str] = set()
    for hit in hits:
        if not hit.doc_id or hit.doc_id in seen:
            continue
        seen.add(hit.doc_id)
        try:
            others = governance.confirmed_contradictions(hit.doc_id)
        except Exception:
            logger.warning("读取冲突关系失败 doc=%s", hit.doc_id, exc_info=True)
            continue
        for other in others:
            if other and other != hit.doc_id:
                pairs.add(frozenset((hit.doc_id, other)))
    return pairs


def attach_contradictions(
    hits: list[RetrievalHit],
    pairs: set[frozenset[str]],
    vector_store,
    collection: str,
) -> list[RetrievalHit]:
    present = {hit.doc_id for hit in hits if hit.doc_id}
    extra: list[RetrievalHit] = []
    seen = {hit.chunk_id for hit in hits}
    for pair in pairs:
        for doc_id in pair:
            if not doc_id or doc_id in present:
                continue
            match = representative_hit(vector_store, collection, doc_id, score=0.0)
            if match is None or match.chunk_id in seen:
                continue
            extra.append(match)
            seen.add(match.chunk_id)
            present.add(doc_id)
    return [*hits, *extra]


def representative_hit(
    vector_store,
    collection: str,
    doc_id: str,
    score: float,
) -> RetrievalHit | None:
    try:
        rows = vector_store.get(collection, where=eq("doc_id", doc_id))
    except Exception:
        logger.warning("读取文档 %s 的切块失败", doc_id, exc_info=True)
        return None
    if not rows:
        return None
    row = rows[0]
    meta = row.metadata or {}
    return RetrievalHit(
        chunk_id=row.id,
        doc_id=str(meta.get("doc_id") or doc_id),
        text=row.document or "",
        score=float(score),
        title=str(meta.get("title") or ""),
        section=str(meta.get("section") or ""),
        source_path=str(meta.get("source_path") or ""),
        media=MediaRef.from_metadata(meta),
        collection=collection,
        valid_until=display_day(meta.get("valid_until")),
        last_verified=str(meta.get("last_verified") or ""),
        authority=_authority(meta.get("authority")),
    )


def label_expired(hits: list[RetrievalHit], *, today: str | None = None) -> None:
    """相对真实今天已经过期的命中写上时间。比较日和标注日不是一回事。"""
    day = today or today_utc()
    for hit in hits:
        if hit.note:
            continue
        if is_expired(hit.valid_until, day):
            hit.note = expiry_note(hit.valid_until, hit.last_verified)


def _authority(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 3
    return number
