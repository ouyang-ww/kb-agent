"""闸 1 可答性与闸 2 引用契约。拒答是正常答案，不走异常。"""

from kbagent.generation.citations import CITATION_SYSTEM_PROMPT, apply_citation_contract


def test_prompt_requires_numbered_sources_and_fixed_refusal():
    assert "不得" in CITATION_SYSTEM_PROMPT
    assert "[n]" in CITATION_SYSTEM_PROMPT
    assert "知识库中没有直接依据回答这个问题。" in CITATION_SYSTEM_PROMPT
    assert "SELF_CHECK" in CITATION_SYSTEM_PROMPT


def test_self_check_is_kept_out_of_the_answer():
    text = (
        "事实来自资料 [1]。\n"
        'SELF_CHECK {"used_sources": [1], "confidence": "high", "gaps": [], "unsupported": []}'
    )
    checked = apply_citation_contract(text, 1)
    assert "SELF_CHECK" not in checked.text
    assert checked.self_check["used_sources"] == [1]
    assert checked.used_sources == [1]
    assert checked.forced_refusal is False


def test_invented_only_citation_becomes_refusal():
    checked = apply_citation_contract("编造的结论 [9]。", 1)
    assert checked.forced_refusal is True
    assert checked.text == "知识库中没有直接依据回答这个问题。"
    assert 9 in checked.stripped


def test_mixed_citation_strips_the_invented_number():
    checked = apply_citation_contract("资料里的事实 [1]，以及编造 [9]。", 1)
    assert checked.forced_refusal is False
    assert "[1]" in checked.text
    assert "[9]" not in checked.text
    assert checked.confidence_steps == 1


def test_default_tau_uses_measured_score_scales():
    """默认 τ 按真实 bge 的分数尺度拒绝库外、接受库内，不再把概率又 sigmoid 一次。"""
    from kbagent.config import Settings
    from kbagent.generation.answerability import assess
    from kbagent.retrieval.reranker import stable_sigmoid

    settings = Settings(
        storage_dir="/tmp/kb-tau-defaults",
        embedding_model="unused",
        rerank_enabled=True,
    )
    # 库外向量分最高 0.4959，必须直接拒答。基线最低 0.5440 必须作答。
    low_vector = assess(0.4959, None, settings)
    assert low_vector.action == "refuse"
    assert low_vector.score_kind == "vector"
    assert low_vector.calibrated is True
    assert low_vector.reason == "below_tau_low"
    assert settings.answer_tau_low == 0.52
    assert settings.answer_tau_high == 0.54

    in_kb = assess(0.5440, None, settings)
    assert in_kb.action == "answer"
    assert in_kb.score_kind == "vector"
    assert assess(0.53, None, settings).action == "weak"

    # predict 已是概率。0 再 sigmoid 会变成 0.5，旧闸门因此不拒答。
    # 库外重排最高 0.3319，基线最低 0.9406。
    rerank_out = assess(0.40, 0.3319, settings)
    assert rerank_out.action == "refuse"
    assert rerank_out.top1_score == 0.3319
    assert rerank_out.score_kind == "rerank"
    assert rerank_out.calibrated is True
    assert stable_sigmoid(0.0) == 0.5
    assert settings.answer_rerank_tau_low == 0.50
    assert settings.answer_rerank_tau_high == 0.90

    rerank_in = assess(0.40, 0.9406, settings)
    assert rerank_in.action == "answer"
    assert rerank_in.score_kind == "rerank"

    assert stable_sigmoid(-1000.0) == 0.0
    assert stable_sigmoid(1000.0) == 1.0
    from kbagent.retrieval.reranker import to_gate_score

    assert to_gate_score(0.0, probability=True) == 0.0
    assert to_gate_score(-1000.0, probability=False) == 0.0
    assert to_gate_score(0.0, probability=False) == 0.5


def test_rerank_load_failure_is_marked(kb, monkeypatch):
    from kbagent.retrieval.reranker import Reranker

    kb.ingest_text("向量检索的核心是 embedding。", title="向量")

    def boom(self):
        raise RuntimeError("model load failed")

    kb._reranker.enabled = True
    monkeypatch.setattr(Reranker, "_lazy", boom)
    ans = kb.ask("向量检索的核心是什么？", rerank=True)
    assert ans.meta["rerank_fallback"] is True
    assert "model load failed" in ans.meta["rerank_error"]
    assert ans.meta["score_kind"] == "vector"
    assert ans.meta["tau_calibrated"] is True
    assert ans.meta.get("score_kind") != "rerank"


def test_below_tau_refuses_without_calling_llm(kb):
    kb.settings.answer_tau_low = 10
    kb.settings.answer_tau_high = 11
    kb.ingest_text("向量检索的核心是 embedding。", title="向量")
    before = len(kb._llm.calls)
    ans = kb.ask("向量检索的核心是什么？")
    assert ans.meta["refusal_reason"] == "below_tau_low"
    assert ans.meta["confidence"] == "low"
    assert ans.meta["answerability"] == "refuse"
    assert "知识库中没有直接依据" in ans.text
    assert "依据较弱" not in ans.text
    assert ans.sources
    assert len(kb._llm.calls) == before
    row = kb.list_answers()[-1]
    assert row["refusal_reason"] == "below_tau_low"
    assert row["confidence"] == "low"
    assert row["query"] == "向量检索的核心是什么？"


def test_weak_band_answers_with_hint(kb):
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 10
    kb.ingest_text("向量检索的核心是 embedding。", title="向量")
    ans = kb.ask("向量检索的核心是什么？")
    assert ans.meta["answerability"] == "weak"
    assert ans.meta["confidence"] == "low"
    assert ans.meta["refusal_reason"] is None
    assert "依据较弱" in ans.text
    assert "[1]" in ans.text
    assert ans.sources


def test_high_score_answers_normally(kb):
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    kb.ingest_text("向量检索的核心是 embedding。", title="向量")
    ans = kb.ask("向量检索的核心是什么？")
    assert ans.meta["answerability"] == "answer"
    assert ans.meta["confidence"] == "high"
    assert "依据较弱" not in ans.text
    assert ans.meta["score_kind"] == "vector"


def test_invented_citation_on_the_answer_path(kb):
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    kb._llm.chat_text = "完全编造的事实 [9]。"
    ans = kb.ask("什么是 RAG？")
    assert ans.meta["refusal_reason"] == "invalid_citations"
    assert "[9]" not in ans.text
    assert "知识库中没有直接依据" in ans.text
    row = kb.list_answers()[-1]
    assert row["refusal_reason"] == "invalid_citations"


def test_partial_invented_citation_downgrades_confidence(kb):
    kb.settings.answer_tau_low = 0
    kb.settings.answer_tau_high = 0
    kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    kb._llm.chat_text = "RAG 是检索增强生成 [1]，另外还有编造 [9]。"
    ans = kb.ask("什么是 RAG？")
    assert "[1]" in ans.text
    assert "[9]" not in ans.text
    assert ans.meta["confidence"] == "medium"
    assert ans.meta["refusal_reason"] is None
    assert ans.meta["citation_check"]["stripped"] == [9]
