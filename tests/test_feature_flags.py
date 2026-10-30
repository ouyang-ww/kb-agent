"""运行时开关：默认值、持久化、白名单、KnowledgeBase 热切换。"""

from __future__ import annotations

import pytest

from kbagent.config import Settings
from kbagent.errors import KBError
from kbagent.flags import FeatureFlags
from kbagent.kb import KnowledgeBase

from helpers import FakeEmbedder, FakeLLM


def test_defaults_and_set(tmp_path):
    flags = FeatureFlags(tmp_path / "flags.sqlite3")
    assert flags.get("study.enabled") is False
    assert flags.get("study.queue") is True
    assert flags.set("study.enabled", True) is True
    assert flags.get("study.enabled") is True
    flags.close()


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "flags.sqlite3"
    first = FeatureFlags(path)
    first.set("study.enabled", True)
    first.close()

    second = FeatureFlags(path)
    assert second.get("study.enabled") is True
    second.close()


def test_unknown_flag_rejected(tmp_path):
    flags = FeatureFlags(tmp_path / "flags.sqlite3")
    with pytest.raises(KBError):
        flags.set("study.unknown", True)
    with pytest.raises(KBError):
        flags.get("study.unknown")
    flags.close()


def test_settings_defaults_override(tmp_path):
    flags = FeatureFlags(
        tmp_path / "flags.sqlite3",
        defaults={"study.enabled": True},
    )
    assert flags.get("study.enabled") is True
    flags.close()


def test_kb_flags_hot_toggle(tmp_path):
    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        study_enabled=True,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
        llm_model="fake-model",
    )
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        assert kb.flag("study.enabled") is True
        kb.set_flag("study.enabled", False)
        assert kb.flag("study.enabled") is False
        assert kb.get_flags()["study.enabled"] is False
    finally:
        kb.close()
