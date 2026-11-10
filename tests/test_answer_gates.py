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
