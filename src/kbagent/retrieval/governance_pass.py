"""检索结果上的过期替换与冲突补齐。向量 where 是第一道，这里是第二道。

已确认的 supersedes 会把命中换成链上最新的文档。pending 关系不进这里。
已确认的 contradicts 把另一边补进候选，后面的折叠不会把它们合成一条。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..governance.near_dup import DEFAULT_COSINE, DEFAULT_COSINE_HAMMING, DEFAULT_MMR_LAMBDA
from ..governance.scope import visibility_filter
from ..governance.validity import (
    chunk_has_valid_until,
    display_day,
    expiry_note,
    is_expired,
    today_utc,
    validity_filter,
)
from ..models import MediaRef, RetrievalHit
from ..storage.filters import and_where, eq
from .diversity import ensure_conflict_sources, select_diverse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievalPolicy:
    """这次检索允许看见什么。和 retriever.search 的开关同一套。"""

    include_hidden: bool = False
    include_expired: bool = False
    as_of: str | None = None
    default_scope: str = "default"
    doc_visible: Callable[[str], bool] | None = None
    doc_dates: Callable[[str], tuple[str | None, str | None]] | None = None


def shape_hits(
    hits: list[RetrievalHit],
    *,
    governance,
    vector_store,
    collection: str,
    embedder,
    top_k: int,
    policy: RetrievalPolicy | None = None,
    cosine_threshold: float = DEFAULT_COSINE,
    hamming_guard: int = DEFAULT_COSINE_HAMMING,
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
        policy=policy or RetrievalPolicy(),
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
    policy: RetrievalPolicy | None = None,
) -> tuple[list[RetrievalHit], set[frozenset[str]]]:
    """沿已确认的取代链替换，并补上已确认的矛盾来源。

    补进来的切块必须满足这次调用的可见性和时效。补不到合法切块时保留原命中。
    """
    if governance is None or not hits:
        return hits, set()
    rules = policy or RetrievalPolicy()
    replaced = follow_supersedes(hits, governance, vector_store, collection, rules)
    pairs = conflict_pairs(replaced, governance)
    attached = attach_contradictions(
        replaced, pairs, vector_store, collection, rules
    )
    return attached, pairs


def follow_supersedes(
    hits: list[RetrievalHit],
    governance,
    vector_store,
    collection: str,
    policy: RetrievalPolicy | None = None,
) -> list[RetrievalHit]:
    rules = policy or RetrievalPolicy()
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
            replacement = representative_hit(
                vector_store, collection, latest, hit.score, rules
            )
            if replacement is None:
                logger.warning(
                    "取代链最新文档 %s 不在本次检索范围内，保留原命中 %s",
                    latest,
                    hit.doc_id,
                )
            else:
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
    policy: RetrievalPolicy | None = None,
) -> list[RetrievalHit]:
    rules = policy or RetrievalPolicy()
    present = {hit.doc_id for hit in hits if hit.doc_id}
    extra: list[RetrievalHit] = []
    seen = {hit.chunk_id for hit in hits}
    for pair in pairs:
        for doc_id in pair:
            if not doc_id or doc_id in present:
                continue
            match = representative_hit(vector_store, collection, doc_id, 0.0, rules)
            if match is None:
                logger.warning(
                    "冲突文档 %s 不在本次检索范围内，不补进结果",
                    doc_id,
                )
                continue
            if match.chunk_id in seen:
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
    policy: RetrievalPolicy | None = None,
) -> RetrievalHit | None:
    """按这次检索的可见性和时效读取一条切块。读不到合法切块就返回 None。"""
    rules = policy or RetrievalPolicy()
    if not _doc_allowed(doc_id, rules):
        return None
    where = eq("doc_id", doc_id)
    if not rules.include_hidden:
        where = and_where(where, visibility_filter(rules.default_scope))
    if not rules.include_expired:
        where = and_where(where, validity_filter(rules.as_of))
    try:
        rows = vector_store.get(collection, where=where)
    except Exception:
        logger.warning("读取文档 %s 的切块失败", doc_id, exc_info=True)
        return None
    for row in rows:
        meta = row.metadata or {}
        if not _chunk_allowed(meta, rules):
            continue
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
    return None


def _doc_allowed(doc_id: str, policy: RetrievalPolicy) -> bool:
    if not policy.include_hidden and policy.doc_visible is not None:
        try:
            if not policy.doc_visible(doc_id):
                return False
        except Exception:
            logger.warning("读取文档 %s 的可见性失败，不补进结果", doc_id, exc_info=True)
            return False
    if not policy.include_expired and policy.doc_dates is not None:
        try:
            until, _verified = policy.doc_dates(doc_id)
        except Exception:
            logger.warning("读取文档 %s 的时效失败，不补进结果", doc_id, exc_info=True)
            return False
        if is_expired(until, policy.as_of):
            return False
    return True


def _chunk_allowed(meta: dict, policy: RetrievalPolicy) -> bool:
    if not policy.include_expired and not chunk_has_valid_until(meta.get("valid_until")):
        return False
    if not policy.include_expired and is_expired(display_day(meta.get("valid_until")), policy.as_of):
        return False
    if policy.include_hidden:
        return True
    status = str(meta.get("status") or "")
    scope = str(meta.get("scope") or "").strip()
    from ..governance.scope import ACTIVE_STATUS, visibility_scopes

    return status == ACTIVE_STATUS and scope in visibility_scopes(policy.default_scope)


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
