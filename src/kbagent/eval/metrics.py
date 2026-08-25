"""检索指标：Recall@k、Precision@k、HitRate@k、MRR、NDCG@k（纯函数，可单测）。"""

from __future__ import annotations

import math


def recall_at_k(relevant: set[str], retrieved: list[str], k: int) -> float:
    """Recall@k = 命中的相关 doc 数 / 相关 doc 总数。"""
    if not relevant:
        return 0.0
    hit = len(relevant & set(retrieved[:k]))
    return hit / len(relevant)


def precision_at_k(relevant: set[str], retrieved: list[str], k: int) -> float:
    if not retrieved[:k]:
        return 0.0
    hit = len(relevant & set(retrieved[:k]))
    return hit / len(retrieved[:k])


def hit_rate_at_k(relevant: set[str], retrieved: list[str], k: int) -> float:
    """至少召回一个相关文档即命中。"""
    return 1.0 if relevant & set(retrieved[:k]) else 0.0


def mrr(relevant: set[str], retrieved: list[str]) -> float:
    """第一个相关文档的倒数排名。"""
    for i, doc_id in enumerate(retrieved):
        if doc_id in relevant:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(relevant: set[str], retrieved: list[str], k: int) -> float:
    """NDCG@k：相关文档在 top-k 中按位置折损的增益。"""
    top = retrieved[:k]
    if not top:
        return 0.0
    dcg = 0.0
    for i, doc_id in enumerate(top):
        rel = 1.0 if doc_id in relevant else 0.0
        dcg += rel / math.log2(i + 2)
    # IDCG：理想排序（相关文档全部排最前）
    n = min(len(relevant), len(top))
    idcg = sum(1.0 / math.log2(i + 2) for i in range(n))
    return dcg / idcg if idcg > 0 else 0.0


def aggregate(items: list[dict], k: int) -> dict:
    """对每条 {relevant_doc_ids, retrieved_doc_ids} 聚合平均指标。"""
    n = len(items)
    if n == 0:
        return {f"recall@{k}": 0.0, f"precision@{k}": 0.0, f"hit_rate@{k}": 0.0, "mrr": 0.0, f"ndcg@{k}": 0.0}
    sums = {"recall": 0.0, "precision": 0.0, "hit": 0.0, "mrr": 0.0, "ndcg": 0.0}
    for it in items:
        rel = set(it["relevant_doc_ids"])
        ret = list(it["retrieved_doc_ids"])
        sums["recall"] += recall_at_k(rel, ret, k)
        sums["precision"] += precision_at_k(rel, ret, k)
        sums["hit"] += hit_rate_at_k(rel, ret, k)
        sums["mrr"] += mrr(rel, ret)
        sums["ndcg"] += ndcg_at_k(rel, ret, k)
    return {
        f"recall@{k}": round(sums["recall"] / n, 4),
        f"precision@{k}": round(sums["precision"] / n, 4),
        f"hit_rate@{k}": round(sums["hit"] / n, 4),
        "mrr": round(sums["mrr"] / n, 4),
        f"ndcg@{k}": round(sums["ndcg"] / n, 4),
    }
