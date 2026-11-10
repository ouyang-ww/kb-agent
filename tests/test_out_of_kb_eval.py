"""库外拒答集：数据集本身可跑，不依赖裁判模型。"""

from pathlib import Path

from kbagent.eval.dataset import EvalItem, load_dataset
from kbagent.eval.runner import Evaluator

_ROOT = Path(__file__).resolve().parents[1]
_DATASET = _ROOT / "eval/datasets/out_of_kb.jsonl"
_NOTES = _ROOT / "examples/sample_notes"

# 基线笔记里的专有说法。库外问题不能靠这些词“碰巧”被资料覆盖。
_CORPUS_MARKERS = (
    "RAG",
    "检索增强",
    "嵌入",
    "余弦",
    "BM25",
    "向量",
    "async",
    "协程",
    "食盐",
    "1500",
    "全谷物",
    "Chroma",
    "FAISS",
    "Qdrant",
    "Milvus",
)


class _JudgeMustNotRun:
    def correctness(self, *args, **kwargs):
        raise AssertionError("refusal 模式不应调用裁判")

    def faithfulness(self, *args, **kwargs):
        raise AssertionError("refusal 模式不应调用裁判")

    def relevance(self, *args, **kwargs):
        raise AssertionError("refusal 模式不应调用裁判")

    def citation_accuracy(self, *args, **kwargs):
        raise AssertionError("refusal 模式不应调用裁判")


def test_out_of_kb_dataset_is_outside_the_sample_corpus():
    items = load_dataset(_DATASET)
    assert 20 <= len(items) <= 50
    notes = "\n".join(path.read_text(encoding="utf-8") for path in _NOTES.glob("*.md"))
    for item in items:
        assert item.should_refuse is True
        assert item.category == "out_of_kb"
        assert item.relevant_doc_ids == []
        assert item.query not in notes
        lowered = item.query.lower()
        for marker in _CORPUS_MARKERS:
            assert marker.lower() not in lowered


def test_refusal_harness_counts_gate_refusals_without_a_judge(kb):
    kb.ingest_text("RAG 是检索增强生成，先检索后生成。", title="RAG")
    kb.settings.answer_tau_low = 10
    kb.settings.answer_tau_high = 11
    items = load_dataset(_DATASET)[:5]
    result = Evaluator(kb, kb.settings, judge=_JudgeMustNotRun()).run(
        items, mode="refusal", rerank=False
    )
    assert result.metrics["refusal_recall"] == 1.0
    assert result.metrics["total"] == 5.0
    assert result.warnings == []
    assert all(row["refused"] for row in result.per_item)
    assert "accuracy" not in result.metrics


def test_refusal_harness_does_not_treat_a_cited_answer_as_refusal(kb):
    kb.ingest_text("一些与问题无关的笔记。", title="笔记")
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    items = [
        EvalItem(
            query="拿破仑是在哪一年兵败滑铁卢的？",
            should_refuse=True,
            category="out_of_kb",
        )
    ]
    result = Evaluator(kb, kb.settings).run(items, mode="refusal", rerank=False)
    assert result.metrics["refusal_recall"] == 0.0
    assert result.warnings and "refusal_recall" in result.warnings[0]
