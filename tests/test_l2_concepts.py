"""L2：概念、出处、消歧、概念页、参照系。不做出题。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kbagent.cli import app
from kbagent.config import Settings
from kbagent.errors import StudyDisabledError
from kbagent.kb import KnowledgeBase
from kbagent.models import SOURCE_BOUND
from kbagent.study.extract import (
    ExtractedConcept,
    Extraction,
    duplicate_rate,
    is_meaningful,
    resolve_extract_target,
)
from kbagent.study.schema import load_ontology

from helpers import FakeEmbedder, FakeLLM

NOTES = Path(__file__).resolve().parents[1] / "examples" / "sample_notes"
MARKER = "MARKER_FULLTEXT_SHOULD_NOT_ENTER_STUDY_DB_9f3a"


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _enable(kb: KnowledgeBase) -> None:
    kb.set_flag("study.concepts", True)
    kb.set_flag("study.enabled", True)


def _mentions(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM mentions"))
    finally:
        conn.close()


def test_extract_target_does_not_call_cloud_without_a_key(tmp_path):
    settings = Settings(storage_dir=tmp_path / "kb")
    target = resolve_extract_target(settings)
    assert target.kind == "heuristic"
    assert target.reason == "no_llm_key"
    assert target.provider == "deepseek"
    assert target.api_key == ""

    local = Settings(storage_dir=tmp_path / "kb", study_local_model="qwen2.5", study_extract_api_key="sk-cloud")
    chosen = resolve_extract_target(local)
    assert chosen.kind == "llm"
    assert chosen.reason == "local_model"
    assert chosen.provider == "local"
    assert chosen.model == "qwen2.5"
    assert "127.0.0.1" in chosen.base_url
    assert chosen.api_key != "sk-cloud"

    cloud = Settings(storage_dir=tmp_path / "kb", study_extract_api_key="sk-test", study_extract_provider="deepseek")
    remote = resolve_extract_target(cloud)
    assert remote.kind == "llm"
    assert remote.provider == "deepseek"
    assert remote.base_url == "https://api.deepseek.com/v1"
    assert remote.model == "deepseek-chat"

    closed = Settings(
        storage_dir=tmp_path / "kb",
        study_extract="heuristic",
        study_extract_api_key="sk-test",
    )
    assert resolve_extract_target(closed).kind == "heuristic"
    assert resolve_extract_target(
        Settings(storage_dir=tmp_path / "kb", study_extract="off", study_extract_api_key="sk-test")
    ).kind == "off"


def test_llm_extractor_keeps_grounded_spans_and_falls_back():
    ontology = load_ontology(None)
    chunk = "重排是对初检结果再排序的步骤。向量检索是用向量找近邻的方法。重排依赖向量检索。"

    class Scripted:
        def chat_json(self, messages, **kwargs):
            return {
                "concepts": [
                    {
                        "name": "重排",
                        "definition": "对初检结果再排序的步骤",
                        "aliases": [],
                        "entity_type": "Concept",
                        "evidence": "重排是对初检结果再排序的步骤",
                    },
                    {
                        "name": "库外概念",
                        "definition": "片段里没有的解释所以必须丢掉",
                        "aliases": [],
                        "entity_type": "Concept",
                        "evidence": "这段不在片段里",
                    },
                ],
                "relations": [
                    {
                        "src": "重排",
                        "dst": "向量检索",
                        "type": "prerequisite",
                        "evidence": "重排依赖向量检索",
                    }
                ],
            }

    from kbagent.study.extract import HeuristicExtractor, LLMExtractor

    extracted = LLMExtractor(
        Scripted(),
        ontology,
        fallback=HeuristicExtractor(ontology),
    ).extract(chunk)
    assert extracted.extractor == "llm"
    assert [item.name for item in extracted.concepts] == ["重排"]
    assert extracted.concepts[0].evidence in chunk
    # 关系的另一端没有被保留，不能留下悬空边。
    assert extracted.relations == []

    class Boom:
        def chat_json(self, messages, **kwargs):
            raise RuntimeError("down")

    fallen = LLMExtractor(Boom(), ontology, fallback=HeuristicExtractor(ontology)).extract(chunk)
    assert fallen.extractor == "heuristic"
    assert fallen.reason == "llm_failed"
    assert {item.name for item in fallen.concepts} >= {"重排", "向量检索"}
    assert any(item.type == "prerequisite" and item.src == "重排" and item.dst == "向量检索" for item in fallen.relations)
    assert all(item.evidence in chunk for item in fallen.relations)


def test_meaningful_rule_and_duplicate_rate_are_explicit():
    assert is_meaningful("重排", "对初检结果再排序的步骤", ["重排是对初检结果再排序的步骤"])
    assert not is_meaningful("检索", "检索是一个过于宽泛的说法所以不能单独出题", ["检索是一个过于宽泛的说法所以不能单独出题"])
    assert not is_meaningful("重排", "太短", ["重排太短"])
    rows = [
        {"id": "a", "name": "重排", "aliases": [], "domain": "general", "status": "active", "definition": "对初检结果再排序的步骤"},
        {"id": "b", "name": "嵌入", "aliases": ["Embedding"], "domain": "general", "status": "active", "definition": "把文本映射为稠密向量的技术"},
    ]
    assert duplicate_rate(rows) == 0.0
    rows.append(
        {
            "id": "c",
            "name": "向量嵌入",
            "aliases": ["Embedding"],
            "domain": "general",
            "status": "active",
            "definition": "另一种叫法但别名已经撞上",
        }
    )
    assert duplicate_rate(rows) == pytest.approx(1 / 3)
    assert duplicate_rate(rows) >= 0.10


def test_core_ingest_search_ask_ignore_concepts_when_study_is_off(kb):
    class Boom:
        name = "boom"
        reason = "test"

        def extract(self, text):
            raise AssertionError("关闭时不应该抽取")

    kb._extractor_override = Boom()
    doc_id = kb.ingest_text("向量检索通过 embedding 把文本映射为向量后进行相似度搜索。", title="向量检索")
    hits = kb.search("向量检索", rewrite=False, rerank=False)
    answer = kb.ask("向量检索怎么做？", top_k=3)
    assert hits and hits[0].doc_id == doc_id
    assert answer.sources and answer.sources[0].doc_id == doc_id
    assert all("概念抽取器" not in json.dumps(call, ensure_ascii=False) for call in kb._llm.calls)
    assert not kb.settings.study_db.exists()
    with pytest.raises(StudyDisabledError):
        kb.study.list_concepts()
    assert not kb.settings.study_db.exists()
    kb.close()


def test_enabling_does_not_backfill_and_concepts_off_skips_extraction(kb):
    kb.set_flag("study.concepts", False)
    kb.set_flag("study.enabled", True)
    assert not kb.settings.study_db.exists()
    first = kb.ingest_text("旧笔记是打开概念开关之前写好的一段定义。", title="旧笔记")
    second = kb.ingest_text("重排是对初检结果再排序的步骤。", title="重排")
    kb.flush_derived()
    assert kb.study.list_concepts() == []
    calls = len(kb._llm.calls)
    before = [(hit.doc_id, hit.score) for hit in kb.search("重排", rewrite=False, rerank=False)]

    kb.set_flag("study.concepts", True)
    kb.flush_derived()
    assert kb.study.list_concepts() == []
    assert [(hit.doc_id, hit.score) for hit in kb.search("重排", rewrite=False, rerank=False)] == before
    assert len(kb._llm.calls) == calls

    asked = kb.ask("什么是重排？", top_k=2)
    assert asked.sources[0].doc_id == second
    preview = kb.backfill_concepts(dry_run=True)
    assert preview["applied"] is False
    assert all(row["name"] != "旧笔记" for row in kb.study.list_concepts())
    applied = kb.backfill_concepts(dry_run=False)
    assert applied["applied"] is True
    names = [row["name"] for row in kb.study.list_concepts()]
    assert "重排" in names
    assert any(first == mention["doc_id"] for row in kb.study.list_concepts() for mention in row["mentions"])
    kb.set_flag("study.enabled", False)
    assert kb.settings.study_db.exists()
    with pytest.raises(StudyDisabledError):
        kb.study.list_concepts()
    kept = _mentions(kb.settings.study_db)
    assert kept
    kb.set_flag("study.enabled", True)
    assert any(row["name"] == "重排" for row in kb.study.list_concepts())
    kb.close()


def test_ingest_returns_before_slow_extraction(kb):
    started = threading.Event()
    release = threading.Event()

    class Slow:
        name = "slow"
        reason = "test"

        def extract(self, text):
            started.set()
            assert release.wait(3)
            return Extraction()

    _enable(kb)
    kb._extractor_override = Slow()
    doc_id = kb.ingest_text("重排是对初检结果再排序的步骤。", title="重排")
    assert doc_id
    assert started.wait(2)
    assert kb.search("重排", rewrite=False, rerank=False)
    release.set()
    kb.flush_derived()
    kb.close()


def test_concepts_have_provenance_and_disambiguate(kb):
    _enable(kb)
    first = kb.ingest_text(
        "RAG（Retrieval-Augmented Generation，检索增强生成）是一种把检索接到生成上的架构。",
        title="RAG 基础",
    )
    second = kb.ingest_text(
        "检索增强生成是把检索和生成接在一起的做法。重排是对初检结果再排序的步骤。重排依赖检索增强生成。",
        title="重排笔记",
    )
    kb.flush_derived()
    rows = kb.study.list_concepts()
    by_name = {row["name"]: row for row in rows}
    assert "RAG" in by_name
    rag = by_name["RAG"]
    assert any("检索增强生成" in alias or "Retrieval-Augmented Generation" in alias for alias in rag["aliases"])
    doc_ids = {mention["doc_id"] for mention in rag["mentions"]}
    assert first in doc_ids and second in doc_ids
    for mention in rag["mentions"]:
        assert mention["snippet"]
        assert len(mention["snippet"]) <= 180
        assert mention["chunk_id"]
        assert "source_path" not in mention
    audit = kb.study.audit_concepts()
    assert audit["duplicate_rate"] < 0.10
    assert audit["meaningful_ratio"] is not None and audit["meaningful_ratio"] >= 0.90
    page = kb.study.concept_page("RAG")
    assert page["mastery"] is None
    assert "尚无学习记录" in page["page"]
    assert first in page["page"] and second in page["page"]
    assert "定义" in page["page"] and "出处" in page["page"] and "相关笔记" in page["page"]
    assert page["page_ref"] == f"concept_pages/{page['id']}.md"
    assert not page["page_ref"].startswith("/")
    stored = kb._open_study().store
    assert "learner_state" not in stored.table_names()
    edges = stored.list_edges()
    assert any(edge["type"] == "prerequisite" and edge["evidence"] in "重排依赖检索增强生成" for edge in edges)
    assert all("/" not in edge["provenance"] or edge["provenance"].count("/") == 1 for edge in edges)
    assert all(not edge["provenance"].startswith("/") for edge in edges)

    again = len(_mentions(kb.settings.study_db))
    kb.flush_derived()
    kb.drain_consumers()
    assert len(_mentions(kb.settings.study_db)) == again
    kb.close()


def test_embedding_suggestion_does_not_auto_merge(kb):
    class SameVectors:
        def embed_texts(self, texts):
            return [[1.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0]

    _enable(kb)
    kb._embedder = SameVectors()

    class Fixed:
        name = "fixed"
        reason = "test"

        def extract(self, text):
            concepts = []
            for name, sentence in (
                ("重排", "重排是对初检结果再排序的步骤"),
                ("嵌入", "嵌入是把文本映射为稠密向量的技术"),
            ):
                if sentence in text:
                    concepts.append(
                        ExtractedConcept(name, sentence, [], "Concept", sentence)
                    )
            return Extraction(concepts=concepts, extractor="fixed", reason="test")

    kb._extractor_override = Fixed()
    kb.ingest_text(
        "重排是对初检结果再排序的步骤。嵌入是把文本映射为稠密向量的技术。",
        title="两个概念",
    )
    kb.flush_derived()
    names = {row["name"] for row in kb.study.list_concepts()}
    assert names == {"重排", "嵌入"}
    assert kb.study.audit_concepts()["suggestions"] >= 1
    assert kb.study.audit_concepts()["duplicate_rate"] < 0.10
    kb.close()


def test_hidden_private_secret_expired_and_scope_stay_out_of_default_output(kb):
    _enable(kb)
    kb.ingest_text("重排是对初检结果再排序的步骤。", title="可见重排")
    hidden = kb.ingest_text("隐藏概念是只写在隐藏笔记里的定义。", title="隐藏笔记")
    kb.ingest_text("私有概念是只写在私有笔记里的定义。", title="私有笔记", scope="private")
    kb.ingest_text("机密概念是只写在机密笔记里的定义。", title="机密笔记", scope="secret")
    kb.ingest_text("过期概念是已经过了有效期的一段定义。", title="过期笔记", metadata={"valid_until": "2020-01-01"})
    kb.ingest_text("范围外概念是不在默认允许列表里的定义。", title="范围外", scope="lab")
    kb.ingest_text(
        "排除概念是明确不进入学习的一段定义。",
        title="排除学习",
        metadata={"exclude_from_learning": True},
    )
    kb.flush_derived()
    kb.hide_document(hidden)
    kb.flush_derived()
    listed = kb.study.list_concepts()
    blob = json.dumps(listed, ensure_ascii=False)
    assert "重排" in blob
    for banned in ("隐藏概念", "私有概念", "机密概念", "过期概念", "范围外概念", "排除概念"):
        assert banned not in blob
    page = kb.study.concept_page("重排")
    assert "隐藏概念" not in page["page"]
    assert "私有概念" not in page["page"]
    from kbagent.errors import NotFoundError

    with pytest.raises(NotFoundError):
        kb.study.concept_page("隐藏概念")
    # 关掉展示不等于删掉派生行。隐藏笔记的出处还在库里。
    snippets = [row["snippet"] for row in _mentions(kb.settings.study_db)]
    assert any("隐藏概念" in snippet for snippet in snippets)
    kb.unhide_document(hidden)
    kb.flush_derived()
    assert any(row["name"] == "隐藏概念" for row in kb.study.list_concepts())
    kb.close()


def test_bound_source_is_not_copied_and_hash_stays(kb, tmp_path):
    body = (
        "重排是对初检结果再排序的步骤。\n\n"
        + ("这段说明不构成概念。" * 40)
        + f"\n\n{MARKER}\n"
    )
    path = tmp_path / "vault" / "rerank.md"
    path.parent.mkdir()
    path.write_text(body, encoding="utf-8")
    before = _file_hash(path)
    sample_before = {item.name: _file_hash(item) for item in NOTES.glob("*.md")}
    _enable(kb)
    kb.ingest(path)
    kb.flush_derived()
    assert _file_hash(path) == before
    assert path.read_text(encoding="utf-8") == body
    doc = next(doc for doc in kb.list_documents(include_hidden=True) if doc.title == "rerank")
    assert doc.source_kind == SOURCE_BOUND
    assert doc.full_text == ""
    mentions = _mentions(kb.settings.study_db)
    assert mentions
    assert all(len(row["snippet"]) <= 180 for row in mentions)
    assert all(row["snippet"] != body for row in mentions)
    assert all(MARKER not in row["snippet"] for row in mentions)
    for db in (kb.settings.documents_db, kb.settings.study_db):
        assert MARKER.encode() not in db.read_bytes()
    page = kb.study.concept_page("重排")
    assert MARKER not in page["page"]
    assert str(path) not in page["page"]
    assert str(tmp_path) not in page["page"]
    kb.ingest(NOTES)
    kb.flush_derived()
    assert {item.name: _file_hash(item) for item in NOTES.glob("*.md")} == sample_before
    rows = kb.study.list_concepts()
    assert 0 < len(rows) <= 15
    audit = kb.study.audit_concepts()
    assert audit["duplicate_rate"] < 0.10
    assert audit["meaningful_ratio"] >= 0.90
    kb.close()


def test_outline_coverage_and_gaps_leave_mastery_empty(kb, tmp_path):
    _enable(kb)
    kb.ingest_text("向量检索是用向量找近邻的方法。", title="向量检索")
    kb.flush_derived()
    outline = tmp_path / "rag.yaml"
    outline.write_text(
        "\n".join(
            [
                "domain: rag",
                'source: "《RAG 实践》目录"',
                "nodes:",
                "  - name: 向量检索",
                "    aliases: [语义检索]",
                "    children:",
                "      - name: 嵌入模型",
                "  - name: 重排",
                "  - name: 混合检索",
            ]
        ),
        encoding="utf-8",
    )
    imported = kb.study.import_outline(outline)
    assert imported == {"domain": "rag", "nodes": 4}
    report = kb.study.outline_coverage("rag")
    assert report["coverage"] == pytest.approx(0.25)
    assert report["mastery_coverage"] is None
    assert {gap["name"] for gap in report["gaps"]} == {"嵌入模型", "重排", "混合检索"}
    assert report["covered"][0]["name"] == "向量检索"
    assert str(tmp_path) not in json.dumps(report, ensure_ascii=False)
    kb.close()


def test_failed_extractor_does_not_fail_ingest(kb):
    class Boom:
        name = "boom"
        reason = "test"

        def extract(self, text):
            raise RuntimeError("抽取失败")

    _enable(kb)
    kb._extractor_override = Boom()
    doc_id = kb.ingest_text("重排是对初检结果再排序的步骤。", title="重排")
    report = kb.flush_derived()
    assert doc_id
    assert kb.search("重排", rewrite=False, rerank=False)
    letters = kb.governance.list_dead_letters("mentions")
    assert letters
    assert "抽取失败" in letters[0]["error"]
    assert int((report.get("mentions") or {}).get("dead_letter") or 0) >= 1
    kb.close()


def test_cli_concept_path_has_no_absolute_path(kb, tmp_path, monkeypatch):
    _enable(kb)
    note = tmp_path / "vault" / "note.md"
    note.parent.mkdir()
    note.write_text("重排是对初检结果再排序的步骤。向量检索是用向量找近邻的方法。\n", encoding="utf-8")
    before = _file_hash(note)
    monkeypatch.setattr("kbagent.cli._kb", lambda settings=None: kb)
    runner = CliRunner()
    ingested = runner.invoke(app, ["ingest", str(note)])
    assert ingested.exit_code == 0, ingested.stdout + ingested.stderr
    assert str(tmp_path) not in ingested.stdout
    assert str(note) not in ingested.stdout
    listed = runner.invoke(app, ["study", "concepts", "--audit", "--json"])
    assert listed.exit_code == 0, listed.stdout
    assert str(tmp_path) not in listed.stdout
    payload = json.loads(listed.stdout)
    assert payload["audit"]["duplicate_rate"] < 0.10
    assert payload["audit"]["meaningful_ratio"] >= 0.90
    assert any(row["name"] == "重排" for row in payload["concepts"])
    shown = runner.invoke(app, ["study", "concept", "重排"])
    assert shown.exit_code == 0, shown.stdout
    assert "尚无学习记录" in shown.stdout
    assert str(tmp_path) not in shown.stdout
    outline = tmp_path / "rag.yaml"
    outline.write_text(
        "domain: rag\nsource: 目录\nnodes:\n  - name: 重排\n  - name: 混合检索\n",
        encoding="utf-8",
    )
    imported = runner.invoke(app, ["study", "outline", "import", str(outline)])
    assert imported.exit_code == 0, imported.stdout
    assert str(tmp_path) not in imported.stdout
    covered = runner.invoke(app, ["study", "outline", "coverage", "--domain", "rag"])
    assert covered.exit_code == 0, covered.stdout
    assert "混合检索" in covered.stdout
    assert "尚无学习记录" in covered.stdout
    assert str(tmp_path) not in covered.stdout
    assert _file_hash(note) == before
    blocked = runner.invoke(app, ["study", "disable"])
    assert blocked.exit_code == 0
    denied = runner.invoke(app, ["study", "concepts"])
    assert denied.exit_code == 2
    assert kb.settings.study_db.exists()
    kb.close()


def test_new_concept_cap_per_document(kb):
    class Many:
        name = "many"
        reason = "test"

        def extract(self, text):
            concepts = []
            for index in range(10):
                name = f"概念{index:02d}"
                sentence = f"{name}是用于测试每篇上限的完整定义"
                if sentence in text:
                    concepts.append(ExtractedConcept(name, sentence, [], "Concept", sentence))
            return Extraction(concepts=concepts, extractor="many", reason="test")

    _enable(kb)
    kb._extractor_override = Many()
    text = "\n".join(f"概念{index:02d}是用于测试每篇上限的完整定义。" for index in range(10))
    kb.ingest_text(text, title="上限")
    kb.flush_derived()
    assert len(kb.study.list_concepts()) == 7
    kb.close()


def test_manual_merge_and_source_files_unchanged(kb, tmp_path):
    _enable(kb)

    class Fixed:
        name = "fixed"
        reason = "test"

        def extract(self, text):
            concepts = []
            for name, sentence in (
                ("甲概念", "甲概念是第一段用于合并测试的定义"),
                ("乙概念", "乙概念是第二段用于合并测试的定义"),
            ):
                if sentence in text:
                    concepts.append(ExtractedConcept(name, sentence, [], "Concept", sentence))
            return Extraction(concepts=concepts, extractor="fixed", reason="test")

    kb._extractor_override = Fixed()
    kb.ingest_text(
        "甲概念是第一段用于合并测试的定义。乙概念是第二段用于合并测试的定义。",
        title="合并",
    )
    kb.flush_derived()
    dry = kb.study.merge_concepts("甲概念", "乙概念", dry_run=True)
    assert dry["applied"] is False
    assert len(kb.study.list_concepts()) == 2
    done = kb.study.merge_concepts("甲概念", "乙概念")
    assert done["applied"] is True
    names = [row["name"] for row in kb.study.list_concepts()]
    assert names == ["甲概念"]
    assert any("乙概念" in alias for alias in kb.study.list_concepts()[0]["aliases"])
    kb.close()
