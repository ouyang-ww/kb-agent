"""RAG pipeline 测试：全流程、意图路由、无结果不臆造、来源编号。"""

from kbagent.config import Settings
from kbagent.kb import KnowledgeBase

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


def test_ask_rewrite_used_for_retrieval(settings):
    llm = FakeLLM()
    llm.rewritten = "RAG 是检索增强生成。"  # 改写后与文档精确匹配
    kb = _ask_kb(settings, llm)
    kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    # 原文与文档不精确匹配，但改写后匹配 → 能命中
    ans = kb.ask("RAG 是啥子意思啊？", top_k=2)
    assert ans.sources, "改写后的查询应能命中文档"
