"""RAG pipeline 测试：全流程、意图路由、无结果不臆造、来源编号。"""

import threading

from kbagent.config import Settings
from kbagent.kb import KnowledgeBase
from kbagent.models import QueryPlan

from helpers import FakeEmbedder, FakeLLM


def _ask_kb(settings, llm, intent: bool = False, rewrite: bool = False):
    s = Settings(
        storage_dir=settings.storage_dir,
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=intent,
        query_rewrite_enabled=rewrite,
        multimodal_enabled=False,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
    )
    return KnowledgeBase(s, embedder=FakeEmbedder(), llm=llm)


def test_ask_full_flow(settings):
    kb = _ask_kb(settings, FakeLLM())
    kb.ingest_text("向量检索通过 embedding 把文本映射为向量后进行相似度搜索。", title="向量检索")
    ans = kb.ask("向量检索怎么做？", top_k=3)
    assert ans.text
    assert len(ans.sources) >= 1
    assert ans.sources[0].doc_id
    assert ans.meta["intent"] == "kb_question"


def test_ask_empty_kb_no_hallucination(settings):
    kb = _ask_kb(settings, FakeLLM())
    ans = kb.ask("随便问什么")
    assert "知识库中没有" in ans.text
    assert ans.sources == []


def test_ask_smalltalk_routing(settings):
    llm = FakeLLM()
    llm.intent = "smalltalk"
    kb = _ask_kb(settings, llm, intent=True)
    ans = kb.ask("你好")
    assert ans.meta["intent"] == "smalltalk"
    assert ans.sources == []


def test_ask_management_routing(settings):
    llm = FakeLLM()
    llm.intent = "management"
    kb = _ask_kb(settings, llm, intent=True)
    ans = kb.ask("列出所有文档")
    assert ans.meta["intent"] == "management"
    assert "kb list" in ans.text


def test_ask_sources_carried(settings):
    llm = FakeLLM()
    kb = _ask_kb(settings, llm)
    doc_id = kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    ans = kb.ask("什么是 RAG？", top_k=2)
    assert ans.sources[0].doc_id == doc_id
    assert ans.sources[0].title == "RAG"


def test_search_and_ask_do_not_clobber_shared_flags(kb):
    """search/ask 的本次开关不能写进共享 Settings，并发时也不能留脏值。"""
    kb.settings.query_rewrite_enabled = False
    kb.settings.intent_enabled = True
    search_entered = threading.Event()
    ask_entered = threading.Event()
    search_release = threading.Event()
    ask_release = threading.Event()
    seen: dict[str, tuple] = {}

    def fake_process(query, intent_enabled=None, rewrite_enabled=None):
        seen[query] = (rewrite_enabled, intent_enabled)
        if query == "SEARCH":
            search_entered.set()
            assert search_release.wait(timeout=3)
        elif query == "ASK":
            ask_entered.set()
            assert ask_release.wait(timeout=3)
        return QueryPlan("kb_question", query, query)

    kb._query_processor.process = fake_process
    t_search = threading.Thread(target=lambda: kb.search("SEARCH", rewrite=True))
    t_ask = threading.Thread(
        target=lambda: kb.ask("ASK", rewrite=False, intent=False)
    )
    ask_started = False
    try:
        t_search.start()
        assert search_entered.wait(timeout=3)
        during_search = (
            kb.settings.query_rewrite_enabled,
            kb.settings.intent_enabled,
        )
        t_ask.start()
        ask_started = True
        assert ask_entered.wait(timeout=3)
        during_both = (
            kb.settings.query_rewrite_enabled,
            kb.settings.intent_enabled,
        )
        # 先放行 search，让它的收尾发生在 ask 仍停在 process 里的时候。
        search_release.set()
        t_search.join(timeout=3)
        after_search = (
            kb.settings.query_rewrite_enabled,
            kb.settings.intent_enabled,
        )
        ask_release.set()
        t_ask.join(timeout=3)
    finally:
        search_release.set()
        ask_release.set()
        t_search.join(timeout=3)
        if ask_started:
            t_ask.join(timeout=3)

    assert during_search == (False, True)
    assert during_both == (False, True)
    assert after_search == (False, True)
    assert (kb.settings.query_rewrite_enabled, kb.settings.intent_enabled) == (
        False,
        True,
    )
    assert seen["SEARCH"] == (True, True)
    assert seen["ASK"] == (False, False)


def test_ask_rewrite_used_for_retrieval(settings):
    llm = FakeLLM()
    llm.rewritten = "RAG 是检索增强生成。"  # 改写后与文档精确匹配
    kb = _ask_kb(settings, llm)
    kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    # 原文与文档不精确匹配，但改写后匹配 → 能命中
    ans = kb.ask("RAG 是啥子意思啊？", top_k=2)
    assert ans.sources, "改写后的查询应能命中文档"
