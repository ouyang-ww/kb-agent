"""评估执行器测试：retrieval/full 模式、对比实验、红线告警。"""

from kbagent.eval.dataset import EvalItem, load_dataset, save_dataset, validate
from kbagent.eval.runner import Evaluator


def _seed_kb(kb):
    return {
        "rag": kb.ingest_text("RAG 是检索增强生成，先检索后生成。", title="RAG"),
        "vector": kb.ingest_text("向量检索用 embedding 表示文本相似度。", title="向量检索"),
    }


def test_dataset_roundtrip(tmp_path):
    items = [
        EvalItem(query="q1", expected_answer="a1", relevant_doc_ids=["d1"], category="事实问答"),
        EvalItem(query="q2", image_required=True),
    ]
    p = tmp_path / "ds.jsonl"
    save_dataset(items, p)
    loaded = load_dataset(p)
    assert len(loaded) == 2
    assert loaded[0].relevant_doc_ids == ["d1"]
    assert loaded[1].image_required is True


def test_validate_missing_docs():
    items = [EvalItem(query="q", relevant_doc_ids=["d1", "missing"])]
    problems = validate(items, {"d1"})
    assert any("missing" in p for p in problems)


def test_eval_retrieval_mode(kb):
    ids = _seed_kb(kb)
    items = [
        EvalItem(query="RAG 是检索增强生成。", relevant_doc_ids=[ids["rag"]]),
        EvalItem(query="向量检索用 embedding 表示文本相似度。", relevant_doc_ids=[ids["vector"]]),
    ]
    result = Evaluator(kb, kb.settings).run(items, mode="retrieval", top_k=2)
    assert result.metrics["recall@2"] == 1.0
    assert result.metrics["hit_rate@2"] == 1.0
    assert result.metrics["mrr"] == 1.0


def test_eval_full_mode_with_fake_judge(kb):
    ids = _seed_kb(kb)
    items = [
        EvalItem(query="RAG 是检索增强生成。", expected_answer="是", relevant_doc_ids=[ids["rag"]]),
    ]
    result = Evaluator(kb, kb.settings).run(items, mode="full", top_k=2)
    assert "accuracy" in result.metrics
    assert "faithfulness" in result.metrics
    assert "composite" in result.metrics
    assert result.metrics["accuracy"] == 1.0  # FakeLLM 恒判 correct


def test_compare_experiments(kb):
    _seed_kb(kb)
    items = [EvalItem(query="RAG 是检索增强生成。", relevant_doc_ids=["x"])]
    results = Evaluator(kb, kb.settings).compare(
        items, mode="retrieval", param_sets=[{"top_k": 1}, {"top_k": 5}]
    )
    assert len(results) == 2
    assert results[0].config["top_k"] == 1
    assert results[1].config["top_k"] == 5


def test_redline_warning():
    # 机制测试：指标低于红线 → 告警
    warnings = Evaluator._redlines(
        {"recall@5": 0.5, "accuracy": 0.6, "faithfulness": 0.8}, "full"
    )
    assert len(warnings) == 3
    assert all("告警" in w for w in warnings)
    assert Evaluator._redlines(
        {"recall@5": 0.9, "accuracy": 0.8, "faithfulness": 0.95}, "full"
    ) == []
