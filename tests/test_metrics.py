"""检索指标纯函数测试：用已知数据验证 recall@k / precision / hit_rate / MRR / NDCG。"""

from kbagent.eval.metrics import (
    aggregate,
    hit_rate_at_k,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


def test_recall_at_k():
    rel = {"A", "B"}
    assert recall_at_k(rel, ["A", "C", "D", "B"], 2) == 0.5  # top2 只含 A
    assert recall_at_k(rel, ["A", "B"], 2) == 1.0
    assert recall_at_k(rel, ["C", "D"], 2) == 0.0
    assert recall_at_k(set(), ["A"], 2) == 0.0


def test_precision_at_k():
    rel = {"A", "B"}
    assert precision_at_k(rel, ["A", "C"], 2) == 0.5
    assert precision_at_k(rel, ["C"], 1) == 0.0


def test_hit_rate_at_k():
    rel = {"B"}
    assert hit_rate_at_k(rel, ["A", "B"], 2) == 1.0
    assert hit_rate_at_k(rel, ["A", "C"], 2) == 0.0


def test_mrr():
    rel = {"B"}
    assert mrr(rel, ["A", "B", "C"]) == 0.5
    assert mrr(rel, ["A", "C"]) == 0.0
    assert mrr(rel, ["B"]) == 1.0


def test_ndcg_at_k():
    rel = {"A", "B"}
    # 完美排序 NDCG = 1
    assert ndcg_at_k(rel, ["A", "B"], 2) == 1.0
    # 只命中一个且位置靠后 < 1
    assert ndcg_at_k(rel, ["C", "A"], 2) < 1.0


def test_aggregate():
    items = [
        {"relevant_doc_ids": ["A"], "retrieved_doc_ids": ["A", "B"]},
        {"relevant_doc_ids": ["C"], "retrieved_doc_ids": ["D", "E"]},
    ]
    agg = aggregate(items, k=2)
    assert agg["recall@2"] == 0.5  # (1 + 0) / 2
    assert agg["hit_rate@2"] == 0.5
    assert agg["mrr"] == 0.5  # (1 + 0) / 2


def test_refusal_recall():
    from kbagent.eval.metrics import refusal_recall

    assert refusal_recall([]) == 0.0
    assert refusal_recall([True, True, False, True]) == 0.75


def test_aggregate_empty():
    agg = aggregate([], k=3)
    assert agg["recall@3"] == 0.0
