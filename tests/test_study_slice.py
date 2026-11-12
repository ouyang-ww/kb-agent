"""学习切片：硬编码 3 题，today → answer → 下次到期。关闭时核心检索照常。"""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kbagent.cli import app
from kbagent.config import Settings
from kbagent.errors import KBError, NotFoundError, StudyDisabledError
from kbagent.kb import KnowledgeBase
from kbagent.governance.validity import today_utc
from kbagent.study.calendar import shanghai_today
from kbagent.study.cards import HARDCODED_CARDS, normalize_answer
from kbagent.study.scheduler import ReviewState, grade_for_outcome, schedule
from kbagent.study.service import StudyService

from helpers import FakeEmbedder, FakeLLM

DAY = date(2026, 11, 12)
NOTE = Path(__file__).resolve().parents[1] / "examples" / "sample_notes" / "01_rag基础.md"
_PUBLIC_KEYS = {"id", "type", "stem", "source_label", "next_due", "reps", "lapses"}


def _review(path: Path, item_id: str) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM reviews WHERE item_id = ?", (item_id,)
    ).fetchone()
    assert row is not None
    data = dict(row)
    conn.close()
    return data


def _attempt_count(path: Path, item_id: str | None = None) -> int:
    conn = sqlite3.connect(path)
    if item_id is None:
        count = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    else:
        count = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
    conn.close()
    return int(count)


def _open(path: Path) -> StudyService:
    return StudyService(path)


def test_cards_quote_the_sample_note_and_hide_the_answer():
    text = NOTE.read_text(encoding="utf-8")
    assert len(HARDCODED_CARDS) == 3
    assert len({card.id for card in HARDCODED_CARDS}) == 3
    for card in HARDCODED_CARDS:
        assert card.source_span in text
        assert card.answer in card.source_span
        assert normalize_answer(card.answer) not in normalize_answer(card.stem)


def test_sm2_success_then_lapse_restarts():
    first = schedule(ReviewState(), 4, DAY)
    assert (first.interval_d, first.ease, first.reps, first.lapses) == (1, 2.5, 1, 0)
    assert first.next_due == DAY + timedelta(days=1)

    second = schedule(
        ReviewState(ease=first.ease, interval_d=first.interval_d, reps=first.reps),
        4,
        first.next_due,
    )
    assert second.interval_d == 3
    assert second.reps == 2
    assert second.next_due == first.next_due + timedelta(days=3)

    third = schedule(
        ReviewState(ease=second.ease, interval_d=second.interval_d, reps=second.reps),
        4,
        second.next_due,
    )
    # round(3 * 2.5) == 8。Python round 对 .5 向偶数取整，7.5 落到 8。
    assert third.interval_d == 8
    assert third.next_due == second.next_due + timedelta(days=8)

    failed = schedule(
        ReviewState(ease=2.5, interval_d=3, reps=2, lapses=0),
        1,
        DAY,
    )
    assert (failed.interval_d, failed.ease, failed.reps, failed.lapses) == (1, 2.3, 0, 1)
    restarted = schedule(
        ReviewState(
            ease=failed.ease,
            interval_d=failed.interval_d,
            reps=failed.reps,
            lapses=failed.lapses,
        ),
        4,
        failed.next_due,
    )
    assert restarted.reps == 1
    assert restarted.interval_d == 1
    assert restarted.ease == 2.3


def test_sm2_partial_and_ease_floor():
    partial = schedule(ReviewState(), 3, DAY)
    assert partial.grade == 3
    assert partial.ease == 2.36
    assert partial.interval_d == 1
    assert partial.reps == 1

    state = ReviewState()
    day = DAY
    for _ in range(7):
        nxt = schedule(state, 1, day)
        state = ReviewState(
            ease=nxt.ease,
            interval_d=nxt.interval_d,
            reps=nxt.reps,
            lapses=nxt.lapses,
        )
        day = nxt.next_due
    assert state.ease == 1.3
    assert state.lapses == 7
    assert grade_for_outcome(1) == 4
    assert grade_for_outcome(0.5) == 3
    assert grade_for_outcome(0) == 1
    with pytest.raises(ValueError):
        schedule(ReviewState(), 5, DAY)


def test_today_answer_persists_next_due(tmp_path):
    path = tmp_path / "study.sqlite3"
    svc = _open(path)
    try:
        due = svc.today(on=DAY)
        assert [row["id"] for row in due] == [
            "rag-fullname",
            "rag-index-sink",
            "rag-quality-cap",
        ]
        for row in due:
            assert set(row) == _PUBLIC_KEYS
        blob = json.dumps(due, ensure_ascii=False)
        for card in HARDCODED_CARDS:
            assert card.answer not in blob
            assert card.source_span not in blob

        result = svc.answer(
            "rag-fullname",
            "retrieval augmented generation",
            confidence=2,
            on=DAY,
        )
    finally:
        svc.close()

    assert result["correct"] == 1.0
    assert result["schedule_updated"] is True
    assert result["grade"] == 4
    assert result["grader"] == "exact"
    assert result["confidence"] == 2
    assert result["next_due"] == (DAY + timedelta(days=1)).isoformat()
    assert result["interval_d"] == 1
    assert result["ease"] == 2.5
    assert result["reps"] == 1
    assert result["lapses"] == 0

    stored = _review(path, "rag-fullname")
    assert stored["next_due"] == result["next_due"]
    assert stored["ease"] == 2.5
    assert stored["reps"] == 1
    assert stored["interval_d"] == 1
    assert _attempt_count(path, "rag-fullname") == 1

    again = _open(path)
    try:
        same_day = [row["id"] for row in again.today(on=DAY)]
        assert "rag-fullname" not in same_day
        assert len(same_day) == 2
        nxt = again.today(on=DAY + timedelta(days=1))
        assert "rag-fullname" in [row["id"] for row in nxt]
        second = again.answer(
            "rag-fullname",
            "Retrieval-Augmented Generation",
            on=DAY + timedelta(days=1),
        )
    finally:
        again.close()

    assert second["schedule_updated"] is True
    assert second["interval_d"] == 3
    assert second["reps"] == 2
    assert second["next_due"] == (DAY + timedelta(days=4)).isoformat()
    assert _review(path, "rag-fullname")["next_due"] == second["next_due"]
    assert _attempt_count(path, "rag-fullname") == 2

    conn = sqlite3.connect(path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    assert version == 1
    assert {"items", "attempts", "reviews", "schema_version"} <= tables
    assert "concepts" not in tables
    assert "mentions" not in tables
    assert "learner_state" not in tables


def test_wrong_answer_is_due_again_tomorrow_with_a_lapse(tmp_path):
    path = tmp_path / "study.sqlite3"
    svc = _open(path)
    try:
        result = svc.answer("rag-quality-cap", "向量库", on=DAY)
        punct = svc.answer("rag-index-sink", "写入向量库。", on=DAY)
    finally:
        svc.close()

    assert result["correct"] == 0.0
    assert result["grade"] == 1
    assert result["lapses"] == 1
    assert result["reps"] == 0
    assert result["ease"] == 2.3
    assert result["next_due"] == (DAY + timedelta(days=1)).isoformat()
    stored = _review(path, "rag-quality-cap")
    assert stored["lapses"] == 1
    assert stored["reps"] == 0
    assert stored["next_due"] == result["next_due"]
    assert punct["correct"] == 1.0
    assert punct["next_due"] == (DAY + timedelta(days=1)).isoformat()

    # 错题第二天用笔记里的整句也能判对，并重新从 1 天间隔开始。
    svc = _open(path)
    try:
        longer = svc.answer(
            "rag-quality-cap", "检索质量决定上限", on=DAY + timedelta(days=1)
        )
    finally:
        svc.close()
    assert longer["correct"] == 1.0
    assert longer["reps"] == 1
    assert longer["lapses"] == 1
    assert longer["ease"] == 2.3
    assert longer["interval_d"] == 1
    assert _review(path, "rag-quality-cap")["next_due"] == longer["next_due"]


def test_bad_answer_does_not_write_an_attempt(tmp_path):
    path = tmp_path / "study.sqlite3"
    svc = _open(path)
    try:
        with pytest.raises(NotFoundError):
            svc.answer("not-a-card", "随便", on=DAY)
        with pytest.raises(KBError):
            svc.answer("rag-fullname", "x", confidence=4, on=DAY)
        with pytest.raises(KBError):
            svc.answer("  ", "x", on=DAY)
        with pytest.raises(KBError):
            svc.answer("rag-fullname", None, on=DAY)  # type: ignore[arg-type]
        due = svc.today(on=DAY)
    finally:
        svc.close()
    assert _attempt_count(path) == 0
    assert len(due) == 3
    assert _review(path, "rag-fullname")["reps"] == 0


def test_core_search_works_while_study_is_off(kb):
    assert kb.flag("study.enabled") is False
    doc_id = kb.ingest_text(
        "向量检索通过 embedding 把文本映射为向量后进行相似度搜索。",
        title="向量检索",
    )
    hits = kb.search("向量检索", rewrite=False, rerank=False)
    answer = kb.ask("向量检索怎么做？", top_k=3)
    assert hits and hits[0].doc_id == doc_id
    assert answer.sources and answer.sources[0].doc_id == doc_id
    assert not kb.settings.study_db.exists()
    with pytest.raises(StudyDisabledError):
        kb.study.today()
    with pytest.raises(StudyDisabledError):
        kb.study.answer("rag-fullname", response="Retrieval-Augmented Generation")
    assert not kb.settings.study_db.exists()
    again = kb.search("向量检索", rewrite=False, rerank=False)
    assert [hit.doc_id for hit in again] == [hit.doc_id for hit in hits]
    kb.close()


def test_enabling_study_does_not_change_search(kb):
    doc_id = kb.ingest_text("RAG 是检索增强生成。", title="RAG")
    before = [
        (hit.doc_id, hit.score)
        for hit in kb.search("RAG", rewrite=False, rerank=False)
    ]
    assert before and before[0][0] == doc_id
    calls = len(kb._llm.calls)
    kb.set_flag("study.enabled", True)
    due = kb.study.today(on=DAY)
    assert len(due) == 3
    result = kb.study.answer("rag-fullname", "Retrieval-Augmented Generation", on=DAY)
    assert result["next_due"] == (DAY + timedelta(days=1)).isoformat()
    assert len(kb._llm.calls) == calls
    after = [
        (hit.doc_id, hit.score)
        for hit in kb.search("RAG", rewrite=False, rerank=False)
    ]
    assert after == before

    kb.set_flag("study.enabled", False)
    with pytest.raises(StudyDisabledError):
        kb.study.today(on=DAY)
    assert kb.settings.study_db.exists()
    still = [
        (hit.doc_id, hit.score)
        for hit in kb.search("RAG", rewrite=False, rerank=False)
    ]
    assert still == before
    asked = kb.ask("什么是 RAG？", top_k=2)
    assert asked.sources[0].doc_id == doc_id

    kb.set_flag("study.enabled", True)
    back = [row["id"] for row in kb.study.today(on=DAY)]
    assert "rag-fullname" not in back
    kb.close()

    reopened = KnowledgeBase(kb.settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        assert reopened.flag("study.enabled") is True
        ids = [row["id"] for row in reopened.study.today(on=DAY)]
        assert "rag-fullname" not in ids
        assert _review(kb.settings.study_db, "rag-fullname")["next_due"] == result["next_due"]
    finally:
        reopened.close()


def test_same_day_reanswer_keeps_the_schedule(tmp_path):
    path = tmp_path / "study.sqlite3"
    svc = _open(path)
    try:
        first = svc.answer("rag-fullname", "Retrieval-Augmented Generation", on=DAY)
        stored = _review(path, "rag-fullname")
        second = svc.answer("rag-fullname", "不对", on=DAY)
    finally:
        svc.close()

    assert first["schedule_updated"] is True
    assert first["next_due"] == (DAY + timedelta(days=1)).isoformat()
    assert second["correct"] == 0.0
    assert second["schedule_updated"] is False
    assert "下次到期不变" in second["schedule_note"]
    assert second["next_due"] == first["next_due"]
    assert second["ease"] == first["ease"]
    assert second["interval_d"] == first["interval_d"]
    assert second["reps"] == first["reps"]
    assert second["lapses"] == first["lapses"]
    kept = _review(path, "rag-fullname")
    for key in ("next_due", "ease", "interval_d", "reps", "lapses", "updated_at"):
        assert kept[key] == stored[key]
    assert _attempt_count(path, "rag-fullname") == 2
    conn = sqlite3.connect(path)
    dues = [
        row[0]
        for row in conn.execute(
            "SELECT next_due FROM attempts WHERE item_id = ?", ("rag-fullname",)
        )
    ]
    conn.close()
    assert dues == [first["next_due"], first["next_due"]]


def test_answer_before_due_does_not_advance_sm2(tmp_path):
    path = tmp_path / "study.sqlite3"
    svc = _open(path)
    try:
        first = svc.answer("rag-index-sink", "写入向量库", on=DAY)
        second = svc.answer(
            "rag-index-sink", "写入向量库", on=DAY + timedelta(days=1)
        )
        stored = _review(path, "rag-index-sink")
        early = svc.answer(
            "rag-index-sink", "写入向量库", on=DAY + timedelta(days=2)
        )
        hidden = [row["id"] for row in svc.today(on=DAY + timedelta(days=3))]
        due = [row["id"] for row in svc.today(on=DAY + timedelta(days=4))]
    finally:
        svc.close()

    assert first["next_due"] == (DAY + timedelta(days=1)).isoformat()
    assert second["schedule_updated"] is True
    assert second["interval_d"] == 3
    assert second["reps"] == 2
    assert second["next_due"] == (DAY + timedelta(days=4)).isoformat()
    assert early["correct"] == 1.0
    assert early["schedule_updated"] is False
    assert early["reps"] == 2
    assert early["lapses"] == 0
    assert early["ease"] == 2.5
    assert early["interval_d"] == 3
    assert early["next_due"] == second["next_due"]
    # 若误推进，第三次答对会从第 2 天再加 round(3 × 2.5) = 8 天。
    assert early["next_due"] != (DAY + timedelta(days=10)).isoformat()
    kept = _review(path, "rag-index-sink")
    for key in ("next_due", "ease", "interval_d", "reps", "lapses", "updated_at"):
        assert kept[key] == stored[key]
    assert _attempt_count(path, "rag-index-sink") == 3
    assert "rag-index-sink" not in hidden
    assert "rag-index-sink" in due


def test_study_day_is_shanghai_when_utc_still_yesterday(tmp_path, monkeypatch):
    # 上海 2026-11-12 00:30。进程若在 UTC，date.today() 仍是 11-11。
    morning = datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)
    assert shanghai_today(morning) == date(2026, 11, 12)
    assert morning.astimezone(timezone.utc).date() == date(2026, 11, 11)

    path = tmp_path / "study.sqlite3"
    svc = _open(path)
    try:
        seeded = svc.today(on=date(2026, 11, 12))
        assert {row["next_due"] for row in seeded} == {"2026-11-12"}
        monkeypatch.setattr("kbagent.study.calendar.clock", lambda: morning)
        assert [row["id"] for row in svc.today()] == [row["id"] for row in seeded]
        # 用 UTC 日去比，这三题都还没到期。
        assert svc.today(on=morning.astimezone(timezone.utc).date()) == []

        # 上海 11-12 答完，下次是 11-13。UTC 11-12 20:00 时上海已是 11-13。
        # 若队列误用 UTC，这次作答会当成未到期，间隔停在 1 天。
        first = svc.answer(
            "rag-fullname", "Retrieval-Augmented Generation", on=date(2026, 11, 12)
        )
        assert first["schedule_updated"] is True
        assert first["next_due"] == "2026-11-13"
        evening = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)
        assert shanghai_today(evening) == date(2026, 11, 13)
        assert evening.astimezone(timezone.utc).date() == date(2026, 11, 12)
        monkeypatch.setattr("kbagent.study.calendar.clock", lambda: evening)
        assert "rag-fullname" in [row["id"] for row in svc.today()]
        utc_view = [row["id"] for row in svc.today(on=evening.astimezone(timezone.utc).date())]
        assert "rag-fullname" not in utc_view
        moved = svc.answer("rag-fullname", "Retrieval-Augmented Generation")
    finally:
        svc.close()

    assert moved["schedule_updated"] is True
    assert moved["interval_d"] == 3
    assert moved["reps"] == 2
    assert moved["next_due"] == "2026-11-16"
    assert _review(path, "rag-fullname")["next_due"] == "2026-11-16"


def test_p2_expiry_day_stays_utc(monkeypatch):
    instant = datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr("kbagent.governance.validity.datetime", Frozen)
    assert today_utc() == "2026-11-11"
    assert shanghai_today(instant).isoformat() == "2026-11-12"


def test_core_paths_do_not_reference_study():
    for name in ("search", "ask", "ingest", "ingest_text"):
        source = inspect.getsource(getattr(KnowledgeBase, name))
        assert "study" not in source.lower()
    root = Path(__file__).resolve().parents[1] / "src" / "kbagent"
    for package in (
        "generation",
        "retrieval",
        "ingestion",
        "governance",
        "document_store",
        "vectorstore",
    ):
        for path in (root / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "kbagent.study" not in text
            assert "from .study" not in text


def test_cli_today_answer_loop(tmp_path, monkeypatch):
    settings = Settings(
        storage_dir=tmp_path / "kb",
        embedding_model="unused",
        rerank_enabled=False,
        intent_enabled=False,
        query_rewrite_enabled=False,
        multimodal_enabled=False,
        llm_api_key="test",
        llm_base_url="http://localhost:9999/v1",
        llm_model="fake-model",
    )
    kb = KnowledgeBase(settings, embedder=FakeEmbedder(), llm=FakeLLM())
    monkeypatch.setattr("kbagent.cli._kb", lambda settings=None: kb)
    runner = CliRunner()
    try:
        blocked = runner.invoke(app, ["study", "today"])
        assert blocked.exit_code == 2
        assert "study.enabled" in (blocked.stdout + (blocked.stderr or ""))
        assert not settings.study_db.exists()

        enabled = runner.invoke(app, ["study", "enable"])
        assert enabled.exit_code == 0
        assert kb.flag("study.enabled") is True

        listed = runner.invoke(app, ["study", "today", "--json"])
        assert listed.exit_code == 0, listed.stdout
        rows = json.loads(listed.stdout)
        assert [row["id"] for row in rows] == [
            "rag-fullname",
            "rag-index-sink",
            "rag-quality-cap",
        ]
        assert "Retrieval-Augmented Generation" not in listed.stdout

        answered = runner.invoke(
            app,
            [
                "study",
                "answer",
                "rag-fullname",
                "--response",
                "Retrieval-Augmented Generation",
                "--confidence",
                "3",
                "--json",
            ],
        )
        assert answered.exit_code == 0, answered.stdout
        body = json.loads(answered.stdout)
        assert body["correct"] == 1.0
        assert body["confidence"] == 3
        assert body["schedule_updated"] is True
        assert body["next_due"] == (shanghai_today() + timedelta(days=1)).isoformat()

        again = runner.invoke(
            app,
            ["study", "answer", "rag-fullname", "--response", "不对", "--json"],
        )
        assert again.exit_code == 0, again.stdout
        held = json.loads(again.stdout)
        assert held["schedule_updated"] is False
        assert held["next_due"] == body["next_due"]
        assert "下次到期不变" in held["schedule_note"]

        remaining = json.loads(runner.invoke(app, ["study", "today", "--json"]).stdout)
        assert "rag-fullname" not in [row["id"] for row in remaining]

        missing = runner.invoke(
            app, ["study", "answer", "missing", "--response", "nope"]
        )
        assert missing.exit_code == 1

        runner.invoke(app, ["study", "disable"])
        blocked_again = runner.invoke(app, ["study", "answer", "rag-fullname", "--response", "x"])
        assert blocked_again.exit_code == 2
        assert settings.study_db.exists()
        assert _review(settings.study_db, "rag-fullname")["next_due"] == body["next_due"]
    finally:
        kb.close()
