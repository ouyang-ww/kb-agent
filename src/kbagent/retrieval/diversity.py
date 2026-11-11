"""检索后去重：先按父文档 / 内容簇折叠，再 MMR，然后才轮到重排。

折叠保留每簇里分数最高的一条。已确认的 ``contradicts`` 不会被折进同一簇，
冲突的两边都留下来。MMR 的 λ 默认 0.7，和余弦阈值一样，换嵌入后要重标。
"""

from __future__ import annotations

import logging

from ..governance.near_dup import DEFAULT_COSINE, DEFAULT_COSINE_HAMMING, DEFAULT_MMR_LAMBDA, cosine, hamming, simhash64
from ..models import RetrievalHit

logger = logging.getLogger(__name__)


def _protected(doc_a: str, doc_b: str, pairs: set[frozenset[str]]) -> bool:
    if not doc_a or not doc_b or doc_a == doc_b:
        return False
    return frozenset((doc_a, doc_b)) in pairs


def select_diverse(
    hits: list[RetrievalHit],
    embedder,
    *,
    k: int,
    cosine_threshold: float = DEFAULT_COSINE,
    hamming_guard: int = DEFAULT_COSINE_HAMMING,
    mmr_lambda: float = DEFAULT_MMR_LAMBDA,
    protected_pairs: set[frozenset[str]] | None = None,
) -> list[RetrievalHit]:
    """折叠近重复，再用 MMR 选出最多 ``k`` 条。``k`` 小于 1 时返回空。"""
    if k <= 0 or not hits:
        return []
    pairs = protected_pairs or set()
    vectors = _embed(embedder, hits)
    folded, folded_vecs = _fold(hits, vectors, cosine_threshold, hamming_guard, pairs)
    return _mmr(folded, folded_vecs, k, mmr_lambda)


def ensure_conflict_sources(
    selected: list[RetrievalHit],
    pool: list[RetrievalHit],
    protected_pairs: set[frozenset[str]],
) -> list[RetrievalHit]:
    """确认过的矛盾两边都要出现。MMR 或 top-k 丢掉一边时从候选里补回来。"""
    if not protected_pairs:
        return selected
    present = {hit.doc_id for hit in selected if hit.doc_id}
    extra: list[RetrievalHit] = []
    seen = {hit.chunk_id for hit in selected}
    for pair in protected_pairs:
        docs = [doc_id for doc_id in pair if doc_id]
        if len(docs) < 2:
            continue
        if not (set(docs) & present):
            continue
        for doc_id in docs:
            if doc_id in present:
                continue
            match = _best_in_pool(pool, doc_id)
            if match is None or match.chunk_id in seen:
                continue
            extra.append(match)
            seen.add(match.chunk_id)
            present.add(doc_id)
    return [*selected, *extra]


def _best_in_pool(pool: list[RetrievalHit], doc_id: str) -> RetrievalHit | None:
    found = [hit for hit in pool if hit.doc_id == doc_id]
    if not found:
        return None
    return max(found, key=lambda hit: (hit.score, hit.authority))


def _embed(embedder, hits: list[RetrievalHit]) -> list[list[float] | None]:
    if embedder is None:
        return [None] * len(hits)
    try:
        vectors = embedder.embed_texts([hit.text or "" for hit in hits])
    except Exception:
        logger.warning("多样性去重无法嵌入候选，只按父文档折叠", exc_info=True)
        return [None] * len(hits)
    if len(vectors) != len(hits):
        return [None] * len(hits)
    return [list(row) if row else None for row in vectors]


def _fold(
    hits: list[RetrievalHit],
    vectors: list[list[float] | None],
    threshold: float,
    hamming_guard: int,
    pairs: set[frozenset[str]],
) -> tuple[list[RetrievalHit], list[list[float] | None]]:
    order = sorted(
        range(len(hits)),
        key=lambda i: (
            -hits[i].score,
            -int(hits[i].authority or 0),
            _recency_key(hits[i].last_verified),
        ),
    )
    kept: list[RetrievalHit] = []
    kept_vecs: list[list[float] | None] = []
    for index in order:
        hit = hits[index]
        vec = vectors[index]
        if _absorbed(hit, vec, kept, kept_vecs, threshold, hamming_guard, pairs):
            continue
        kept.append(hit)
        kept_vecs.append(vec)
    return kept, kept_vecs


def _recency_key(value: str) -> tuple[int, str]:
    """空的 last_verified 排在后面。有日期时，越新越靠前。

    升序排序里用「是否缺失」加逐位取反的日期，让 2024 排在 2020 前面，
    空串排在所有日期后面。
    """
    text = (value or "").strip()
    if not text:
        return (1, "")
    inverted = []
    for ch in text:
        if ch.isdigit():
            inverted.append(str(9 - int(ch)))
        else:
            inverted.append(ch)
    return (0, "".join(inverted))


def _absorbed(
    hit: RetrievalHit,
    vec: list[float] | None,
    kept: list[RetrievalHit],
    kept_vecs: list[list[float] | None],
    threshold: float,
    hamming_guard: int,
    pairs: set[frozenset[str]],
) -> bool:
    for prev, prev_vec in zip(kept, kept_vecs):
        if _protected(hit.doc_id, prev.doc_id, pairs):
            continue
        if _near_copy(hit.text, vec, prev.text, prev_vec, threshold, hamming_guard):
            return True
    return False


def _near_copy(
    text: str,
    vec: list[float] | None,
    other_text: str,
    other_vec: list[float] | None,
    threshold: float,
    hamming_guard: int,
) -> bool:
    """余弦高，并且 3-gram 也接近，才是会被折叠的副本。"""
    if vec is None or other_vec is None or cosine(vec, other_vec) < threshold:
        return False
    return hamming(simhash64(text or ""), simhash64(other_text or "")) <= hamming_guard


def _mmr(
    hits: list[RetrievalHit],
    vectors: list[list[float] | None],
    k: int,
    lam: float,
) -> list[RetrievalHit]:
    if len(hits) <= 1:
        return hits[:k]
    lam = min(1.0, max(0.0, float(lam)))
    selected: list[int] = []
    remaining = set(range(len(hits)))
    while remaining and len(selected) < k:
        best_index = None
        best_value = None
        for index in remaining:
            redundancy = 0.0
            current = vectors[index]
            if selected and current is not None:
                sims = [
                    cosine(current, vectors[prev])
                    for prev in selected
                    if vectors[prev] is not None
                ]
                if sims:
                    redundancy = max(0.0, max(sims))
            value = lam * float(hits[index].score) - (1.0 - lam) * redundancy
            if best_value is None or value > best_value:
                best_value = value
                best_index = index
        if best_index is None:
            break
        selected.append(best_index)
        remaining.remove(best_index)
    return [hits[index] for index in selected]
