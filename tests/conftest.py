"""共享 fixtures：临时目录、确定性 FakeEmbedder、FakeLLM、最小 KnowledgeBase。"""

from __future__ import annotations

import pytest

from kbagent.config import Settings
from kbagent.kb import KnowledgeBase

from helpers import FailingLLM, FakeEmbedder, FakeLLM


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",  # 注入 FakeEmbedder
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
        llm_model="fake-model",
    )


@pytest.fixture
def kb(settings) -> KnowledgeBase:
    return KnowledgeBase(
        settings, embedder=FakeEmbedder(), llm=FakeLLM()
    )


@pytest.fixture
def failing_llm() -> FailingLLM:
    return FailingLLM()
