"""意图识别 + 查询改写测试（LLM 路径 + 规则兜底）。"""

from kbagent.config import Settings
from kbagent.models import QueryPlan
from kbagent.retrieval.query_processor import QueryProcessor

from helpers import FailingLLM, FakeLLM


def _proc(llm, **overrides):
    s = Settings(
        llm_api_key="x",
        intent_enabled=overrides.get("intent_enabled", True),
        query_rewrite_enabled=overrides.get("query_rewrite_enabled", True),
    )
    return QueryProcessor(llm, s)


def test_llm_path_kb_question():
    llm = FakeLLM()
    llm.intent = "kb_question"
    llm.rewritten = "什么是混合检索"
    plan = _proc(llm).process("混合检索是什么呀？")
    assert plan.intent == "kb_question"
    assert plan.rewritten_query == "什么是混合检索"


def test_llm_path_smalltalk():
    llm = FakeLLM()
    llm.intent = "smalltalk"
    plan = _proc(llm).process("你好")
    assert plan.intent == "smalltalk"


def test_rules_fallback_on_llm_failure():
    proc = _proc(FailingLLM())
    assert proc.process("你好呀").intent == "smalltalk"
    assert proc.process("帮我列出所有文档").intent == "management"
    assert proc.process("什么是向量数据库").intent == "kb_question"


def test_disabled_intent_keeps_kb_question():
    llm = FakeLLM()
    llm.intent = "smalltalk"
    proc = _proc(llm, intent_enabled=False, query_rewrite_enabled=True)
    plan = proc.process("你好")
    assert plan.intent == "kb_question"


def test_disabled_rewrite_keeps_original():
    proc = _proc(FakeLLM(), intent_enabled=True, query_rewrite_enabled=False)
    plan = proc.process("混合检索原理")
    assert plan.rewritten_query == "混合检索原理"


def test_both_disabled():
    proc = _proc(FakeLLM(), intent_enabled=False, query_rewrite_enabled=False)
    plan = proc.process("问个问题")
    assert plan == QueryPlan("kb_question", "问个问题", "问个问题")
