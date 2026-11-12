"""study.sqlite3：这一刀只建题目、作答和复习状态。

不建 concepts / mentions / learner_state / reports。
作答和当前复习状态不能从文档重建。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from ..errors import KBError, NotFoundError
from .cards import HARDCODED_CARDS, PROMPT_VERSION, is_correct
from .scheduler import DEFAULT_EASE, ReviewState, grade_for_outcome, schedule

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
  id             TEXT PRIMARY KEY,
  type           TEXT NOT NULL,
  stem           TEXT NOT NULL,
  answer         TEXT NOT NULL,
  accept_json    TEXT NOT NULL DEFAULT '[]',
  source_label   TEXT NOT NULL,
  source_span    TEXT NOT NULL,
  content_hash   TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'active',
  created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  id         TEXT PRIMARY KEY,
  item_id    TEXT NOT NULL REFERENCES items(id),
  ts         TEXT NOT NULL,
  response   TEXT,
  correct    REAL NOT NULL,
  confidence INTEGER,
  grader     TEXT NOT NULL,
  next_due   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_item ON attempts(item_id, ts);
CREATE TABLE IF NOT EXISTS reviews (
  item_id    TEXT PRIMARY KEY REFERENCES items(id),
  ease       REAL NOT NULL DEFAULT 2.5,
  interval_d REAL NOT NULL DEFAULT 0,
  reps       INTEGER NOT NULL DEFAULT 0,
  lapses     INTEGER NOT NULL DEFAULT 0,
  next_due   TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_due ON reviews(next_due);
"""

_DUE_SQL = """
SELECT i.id, i.type, i.stem, i.source_label, r.next_due, r.reps, r.lapses
FROM items i
JOIN reviews r ON r.item_id = i.id
WHERE i.status = 'active' AND r.next_due <= ?
ORDER BY r.next_due, i.id
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_day(on: date | None) -> date:
    if on is None:
        return date.today()
    if type(on) is not date:
        raise KBError("on 必须是 datetime.date")
    return on


class StudyStore:
    """学习库。调用方负责只在模块开启时创建它。"""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (1, ?)",
                (_now(),),
            )
            self._conn.commit()

    def _seed_locked(self, on: date) -> None:
        """缺的硬编码题补上，并把第一次到期放在 on。已有行不动。"""
        day = on.isoformat()
        stamp = _now()
        for card in HARDCODED_CARDS:
            self._conn.execute(
                """
                INSERT INTO items(
                  id, type, stem, answer, accept_json, source_label,
                  source_span, content_hash, prompt_version, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    card.id,
                    card.type,
                    card.stem,
                    card.answer,
                    json.dumps(list(card.accept), ensure_ascii=False),
                    card.source_label,
                    card.source_span,
                    card.content_hash(),
                    PROMPT_VERSION,
                    stamp,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO reviews(
                  item_id, ease, interval_d, reps, lapses, next_due, updated_at
                ) VALUES (?, ?, 0, 0, 0, ?, ?)
                ON CONFLICT(item_id) DO NOTHING
                """,
                (card.id, DEFAULT_EASE, day, stamp),
            )

    def due(self, on: date) -> list[dict]:
        with self._lock:
            self._seed_locked(on)
            rows = self._conn.execute(_DUE_SQL, (on.isoformat(),)).fetchall()
            self._conn.commit()
        return [_public_item(row) for row in rows]

    def answer(
        self,
        item_id: str,
        response: str,
        *,
        confidence: int | None,
        on: date,
    ) -> dict:
        if not isinstance(response, str):
            raise KBError("response 必须是字符串")
        if confidence is not None and confidence not in (1, 2, 3):
            raise KBError("confidence 只能是 1、2 或 3")
        key = item_id.strip()
        if not key:
            raise KBError("item_id 不能为空")
        with self._lock:
            self._seed_locked(on)
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT i.id, i.answer, i.accept_json, i.source_label, i.source_span,
                       i.status, r.ease, r.interval_d, r.reps, r.lapses
                FROM items i
                JOIN reviews r ON r.item_id = i.id
                WHERE i.id = ?
                """,
                (key,),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise NotFoundError(f"题目不存在: {key}")
            accept = tuple(json.loads(row["accept_json"] or "[]"))
            correct = 1.0 if is_correct(row["answer"], accept, response) else 0.0
            graded = schedule(
                ReviewState(
                    ease=float(row["ease"]),
                    interval_d=float(row["interval_d"]),
                    reps=int(row["reps"]),
                    lapses=int(row["lapses"]),
                ),
                grade_for_outcome(correct),
                on,
            )
            next_due = graded.next_due.isoformat()
            stamp = _now()
            try:
                self._conn.execute(
                    """
                    INSERT INTO attempts(
                      id, item_id, ts, response, correct, confidence, grader, next_due
                    ) VALUES (?, ?, ?, ?, ?, ?, 'exact', ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        key,
                        stamp,
                        response,
                        correct,
                        confidence,
                        next_due,
                    ),
                )
                self._conn.execute(
                    """
                    UPDATE reviews
                    SET ease=?, interval_d=?, reps=?, lapses=?, next_due=?, updated_at=?
                    WHERE item_id=?
                    """,
                    (
                        graded.ease,
                        graded.interval_d,
                        graded.reps,
                        graded.lapses,
                        next_due,
                        stamp,
                        key,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "item_id": key,
            "correct": correct,
            "grade": graded.grade,
            "grader": "exact",
            "response": response,
            "confidence": confidence,
            "next_due": next_due,
            "interval_d": graded.interval_d,
            "ease": graded.ease,
            "reps": graded.reps,
            "lapses": graded.lapses,
            "source_label": row["source_label"],
            "source_span": row["source_span"],
            "expected": row["answer"],
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()


def _public_item(row: sqlite3.Row) -> dict:
    """今日队列。不含答案，也不含出处片段，避免把答案直接露出来。"""
    return {
        "id": row["id"],
        "type": row["type"],
        "stem": row["stem"],
        "source_label": row["source_label"],
        "next_due": row["next_due"],
        "reps": int(row["reps"]),
        "lapses": int(row["lapses"]),
    }

