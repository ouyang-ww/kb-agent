"""库外拒答集：数据集本身可跑，不依赖裁判模型。"""

from pathlib import Path

from kbagent.config import Settings
from kbagent.eval.dataset import EvalItem, load_dataset
from kbagent.eval.metrics import refusal_recall
from kbagent.eval.runner import Evaluator
from kbagent.generation.answerability import assess

# 2026-11-10，examples/sample_notes + bge-small-zh-v1.5 / bge-reranker-base 的 top1。
# 单测只喂这些数，不下载模型。默认 τ 必须让库外 recall ≥ 0.9，且不拒答基线。
_MEASURED_OUT_VECTOR = (
    0.4049, 0.4556, 0.4426, 0.4905, 0.4460, 0.4535, 0.4543, 0.4223,
    0.4007, 0.4607, 0.4336, 0.4959, 0.4187, 0.4366, 0.4527, 0.4561,
    0.4525, 0.4408, 0.4529, 0.4436, 0.4305, 0.4373, 0.4118, 0.4571,
    0.4493, 0.4474, 0.4304, 0.4780, 0.4633, 0.4852, 0.4111, 0.4592,
)
_MEASURED_OUT_RERANK = (
    0.0, 0.0, 0.0, 0.0008, 0.0771, 0.0, 0.0006, 0.0,
    0.0, 0.0029, 0.0008, 0.0004, 0.0, 0.0001, 0.0, 0.0016,
    0.0004, 0.0, 0.0, 0.0295, 0.0, 0.0, 0.0, 0.0,
    0.0002, 0.0, 0.0, 0.0101, 0.0005, 0.0011, 0.0, 0.3319,
)
_MEASURED_BASE_VECTOR = (0.6228, 0.6055, 0.5440, 0.5975, 0.7086)
_MEASURED_BASE_RERANK = (0.9999, 0.9824, 0.9819, 0.9406, 0.9919)

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


def test_default_tau_refusal_recall_on_measured_scores():
    """默认 τ，不改成 0 或 10。分数是上次实跑的 top1，不是现场下载的模型。"""
    settings = Settings(
        storage_dir="/tmp/kb-measured-tau",
        embedding_model="unused",
    )
    assert settings.answer_tau_low == 0.52
    assert settings.answer_rerank_tau_low == 0.50
    vector_refused = [
        assess(score, None, settings).action == "refuse" for score in _MEASURED_OUT_VECTOR
    ]
    rerank_refused = [
        assess(0.4, score, settings).action == "refuse" for score in _MEASURED_OUT_RERANK
    ]
    assert refusal_recall(vector_refused) >= 0.9
    assert refusal_recall(rerank_refused) >= 0.9
    assert all(assess(score, None, settings).score_kind == "vector" for score in _MEASURED_OUT_VECTOR)
    assert all(
        assess(0.4, score, settings).score_kind == "rerank" for score in _MEASURED_OUT_RERANK
    )
    # 概率 0 必须仍是 0。再 sigmoid 会变成 0.5，默认 τ_low 就拒不掉。
    zero = assess(0.4, 0.0, settings)
    assert zero.top1_score == 0.0
    assert zero.action == "refuse"
    assert all(assess(score, None, settings).action != "refuse" for score in _MEASURED_BASE_VECTOR)
    assert all(assess(0.4, score, settings).action != "refuse" for score in _MEASURED_BASE_RERANK)


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
