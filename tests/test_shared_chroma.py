"""两个进程共用一个 Chroma 目录时，repair 之后旧进程仍要能检索。

Chroma 把 HNSW 留在本进程内存里。另一个进程删掉旧 id 再写入新 id 之后，
旧进程的 query 仍可能返回已经删除的 id（文档和元数据是 None），
默认检索把这些命中滤掉，结果就是空的，直到重新打开客户端。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from kbagent.config import Settings
from kbagent.kb import KnowledgeBase

from helpers import FakeEmbedder, FakeLLM

_ROOT = Path(__file__).resolve().parents[1]


def _settings(storage: Path) -> Settings:
    return Settings(
        storage_dir=storage,
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
        llm_model="fake-model",
    )


def test_search_after_other_process_repair(tmp_path, caplog):
    storage = tmp_path / "kb"
    kb = KnowledgeBase(_settings(storage), embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        doc_id = kb.ingest_text("旧向量 OLD_TOKEN 先被本进程检索过", title="笔记")
        assert doc_id
        warmed = kb.search("OLD_TOKEN", top_k=3)
        assert any("OLD_TOKEN" in hit.text for hit in warmed)

        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_ROOT / "src"), str(_ROOT / "tests"), env.get("PYTHONPATH", "")]
        )
        script = """
import sys
sys.path[:0] = [__SRC__, __TESTS__]
from kbagent.config import Settings
from kbagent.kb import KnowledgeBase
from kbagent.storage.filters import eq
from helpers import FakeEmbedder, FakeLLM
from pathlib import Path

storage = Path(__STORAGE__)
kb = KnowledgeBase(
    Settings(
        storage_dir=storage,
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
        llm_model="fake-model",
    ),
    embedder=FakeEmbedder(),
    llm=FakeLLM(),
)
doc_id = __DOC__
coll = kb.settings.default_collection
text = "NEW_TOKEN 跨进程修复后应能检索"
emb = FakeEmbedder().embed_texts([text])[0]
kb._vector_store.delete(coll, where=eq("doc_id", doc_id))
kb._vector_store.add(
    coll,
    [doc_id + "#replaced"],
    [emb],
    [text],
    [{
        "doc_id": doc_id,
        "title": "笔记",
        "scope": "default",
        "status": "active",
        "source_path": "",
        "section": "",
    }],
)
report = kb.repair()
print(report.get("notice") or "")
kb.close()
"""
        script = (
            script.replace("__SRC__", repr(str(_ROOT / "src")))
            .replace("__TESTS__", repr(str(_ROOT / "tests")))
            .replace("__STORAGE__", repr(str(storage)))
            .replace("__DOC__", repr(doc_id))
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert "重新打开" in result.stdout
        assert "重启" in result.stdout

        with caplog.at_level(logging.WARNING, logger="kbagent.vectorstore.chroma_store"):
            hits = kb.search("NEW_TOKEN 跨进程修复后应能检索", top_k=3)
        assert any("NEW_TOKEN" in (hit.text or "") for hit in hits)
        assert all(hit.text for hit in hits)
        assert any("重新打开" in record.getMessage() for record in caplog.records)
    finally:
        kb.close()
