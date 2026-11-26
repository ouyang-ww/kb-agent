"""study.sqlite3。

v1 是硬编码复习：题目、作答、复习状态。作答不能从文档重建。
v2 起增加概念、关系、mentions、参照系。这些是派生数据，可以按事件重放。
不建 learner_state，也不把原件全文写进这个库。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from ..errors import KBError, NotFoundError
from .calendar import shanghai_today
from .cards import HARDCODED_CARDS, PROMPT_VERSION, is_correct
from .scheduler import DEFAULT_EASE, ReviewState, grade_for_outcome, schedule

# 同一天再答，或到期日之前作答：尝试要留下，SM-2 不动。
SCHEDULE_HELD = "未到到期日，本次作答已记录，下次到期不变"

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

# 概念页正文在存储目录的派生文件里，不放进这张表。
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS concepts (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  normalized   TEXT NOT NULL,
  domain       TEXT NOT NULL,
  definition   TEXT NOT NULL DEFAULT '',
  aliases_json TEXT NOT NULL DEFAULT '[]',
  entity_type  TEXT NOT NULL DEFAULT 'Concept',
  status       TEXT NOT NULL DEFAULT 'active',
  merged_into  TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_concepts_norm
  ON concepts(normalized, domain) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS concept_edges (
  src_id     TEXT NOT NULL,
  dst_id     TEXT NOT NULL,
  type       TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  provenance TEXT NOT NULL DEFAULT '',
  evidence   TEXT NOT NULL DEFAULT '',
  status     TEXT NOT NULL DEFAULT 'active',
  PRIMARY KEY (src_id, dst_id, type)
);
CREATE TABLE IF NOT EXISTS mentions (
  id         TEXT PRIMARY KEY,
  concept_id TEXT NOT NULL,
  doc_id     TEXT NOT NULL,
  chunk_id   TEXT NOT NULL,
  snippet    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (concept_id, doc_id, chunk_id, snippet)
);
CREATE INDEX IF NOT EXISTS ix_mentions_concept ON mentions(concept_id);
CREATE INDEX IF NOT EXISTS ix_mentions_doc ON mentions(doc_id);
CREATE TABLE IF NOT EXISTS extractions (
  chunk_id       TEXT NOT NULL,
  doc_id         TEXT NOT NULL,
  chunk_hash     TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  extractor      TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (chunk_id, chunk_hash, prompt_version)
);
CREATE TABLE IF NOT EXISTS merge_suggestions (
  src_id     TEXT NOT NULL,
  dst_id     TEXT NOT NULL,
  score      REAL NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  PRIMARY KEY (src_id, dst_id)
);
CREATE TABLE IF NOT EXISTS outline_nodes (
  id           TEXT PRIMARY KEY,
  domain       TEXT NOT NULL,
  name         TEXT NOT NULL,
  normalized   TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  parent_id    TEXT,
  ord          INTEGER NOT NULL DEFAULT 0,
  source       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
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
    """缺省日期是 Asia/Shanghai 的今天，不是进程本地日，也不是 P2 的 UTC 日。"""
    if on is None:
        return shanghai_today()
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
        version = int(row["version"]) if row is not None else 0
        if version < 1:
            self._conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (1, ?)",
                (_now(),),
            )
            version = 1
        if version < 2:
            self._conn.executescript(_SCHEMA_V2)
            self._conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (2, ?)",
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
                       i.status, r.ease, r.interval_d, r.reps, r.lapses, r.next_due
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
            current_due = date.fromisoformat(row["next_due"])
            # 到期日之前（含同一天已经推过后）只记账。reps / lapses 也保持不动。
            if current_due > on:
                schedule_updated = False
                grade = grade_for_outcome(correct)
                next_due = row["next_due"]
                ease = float(row["ease"])
                interval_d = float(row["interval_d"])
                reps = int(row["reps"])
                lapses = int(row["lapses"])
            else:
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
                schedule_updated = True
                grade = graded.grade
                next_due = graded.next_due.isoformat()
                ease = graded.ease
                interval_d = graded.interval_d
                reps = graded.reps
                lapses = graded.lapses
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
                if schedule_updated:
                    self._conn.execute(
                        """
                        UPDATE reviews
                        SET ease=?, interval_d=?, reps=?, lapses=?, next_due=?, updated_at=?
                        WHERE item_id=?
                        """,
                        (ease, interval_d, reps, lapses, next_due, stamp, key),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "item_id": key,
            "correct": correct,
            "grade": grade,
            "grader": "exact",
            "response": response,
            "confidence": confidence,
            "schedule_updated": schedule_updated,
            "schedule_note": "" if schedule_updated else SCHEDULE_HELD,
            "next_due": next_due,
            "interval_d": interval_d,
            "ease": ease,
            "reps": reps,
            "lapses": lapses,
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

    # ---- 概念（派生，可重放）----
    def find_concept(self, concept_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM concepts WHERE id = ?", (concept_id,)
            ).fetchone()
        return _concept_row(row) if row is not None else None

    def insert_concept(
        self,
        *,
        concept_id: str,
        name: str,
        normalized: str,
        domain: str,
        definition: str,
        aliases: list[str],
        entity_type: str,
    ) -> dict:
        stamp = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO concepts(
                  id, name, normalized, domain, definition, aliases_json,
                  entity_type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    concept_id,
                    name,
                    normalized,
                    domain,
                    definition,
                    json.dumps(aliases, ensure_ascii=False),
                    entity_type or "Concept",
                    stamp,
                    stamp,
                ),
            )
            self._conn.commit()
        row = self.find_concept(concept_id)
        if row is None:
            raise KBError("概念没有写入")
        return row

    def add_aliases(self, concept_id: str, aliases: list[str], definition: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM concepts WHERE id = ?", (concept_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("概念不存在")
            current = json.loads(row["aliases_json"] or "[]")
            for alias in aliases:
                if alias and alias not in current and alias != row["name"]:
                    current.append(alias)
            body = row["definition"] or ""
            if definition and len(definition) > len(body):
                body = definition
            self._conn.execute(
                """
                UPDATE concepts
                SET aliases_json = ?, definition = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(current, ensure_ascii=False), body, _now(), concept_id),
            )
            self._conn.commit()
        found = self.find_concept(concept_id)
        assert found is not None
        return found

    def list_concepts(self, *, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM concepts"
        params: tuple = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY name, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_concept_row(row) for row in rows]

    def insert_mention(
        self, *, concept_id: str, doc_id: str, chunk_id: str, snippet: str
    ) -> None:
        if not snippet or len(snippet) > 180:
            raise KBError("出处片段不能为空，也不能超过 180 字")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO mentions(id, concept_id, doc_id, chunk_id, snippet, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(concept_id, doc_id, chunk_id, snippet) DO NOTHING
                """,
                (
                    _mention_id(concept_id, doc_id, chunk_id, snippet),
                    concept_id,
                    doc_id,
                    chunk_id,
                    snippet,
                    _now(),
                ),
            )
            self._conn.commit()

    def list_mentions(
        self, *, concept_id: str | None = None, doc_id: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM mentions WHERE 1=1"
        params: list = []
        if concept_id is not None:
            sql += " AND concept_id = ?"
            params.append(concept_id)
        if doc_id is not None:
            sql += " AND doc_id = ?"
            params.append(doc_id)
        sql += " ORDER BY doc_id, chunk_id, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_mention_row(row) for row in rows]

    def delete_chunk_mentions(self, doc_id: str, chunk_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM mentions WHERE doc_id = ? AND chunk_id = ?",
                (doc_id, chunk_id),
            )
            self._conn.commit()

    def delete_doc_derived(self, doc_id: str) -> None:
        """删掉这篇文档的 mentions 和抽取记录。概念行留着，没有可见出处就不再展示。"""
        with self._lock:
            self._conn.execute("DELETE FROM mentions WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM extractions WHERE doc_id = ?", (doc_id,))
            self._conn.execute(
                "DELETE FROM concept_edges WHERE provenance LIKE ?",
                (f"{doc_id}/%",),
            )
            self._conn.commit()

    def delete_mentions_outside(self, doc_id: str, chunk_ids: set[str]) -> None:
        with self._lock:
            if not chunk_ids:
                self._conn.execute("DELETE FROM mentions WHERE doc_id = ?", (doc_id,))
                self._conn.execute("DELETE FROM extractions WHERE doc_id = ?", (doc_id,))
            else:
                marks = ",".join("?" * len(chunk_ids))
                params = [doc_id, *sorted(chunk_ids)]
                self._conn.execute(
                    f"DELETE FROM mentions WHERE doc_id = ? AND chunk_id NOT IN ({marks})",
                    params,
                )
                self._conn.execute(
                    f"DELETE FROM extractions WHERE doc_id = ? AND chunk_id NOT IN ({marks})",
                    params,
                )
            self._conn.commit()

    def has_extraction(self, chunk_id: str, chunk_hash: str, prompt_version: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM extractions
                WHERE chunk_id = ? AND chunk_hash = ? AND prompt_version = ?
                """,
                (chunk_id, chunk_hash, prompt_version),
            ).fetchone()
        return row is not None

    def save_extraction(
        self,
        *,
        chunk_id: str,
        doc_id: str,
        chunk_hash: str,
        prompt_version: str,
        extractor: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO extractions(
                  chunk_id, doc_id, chunk_hash, prompt_version, extractor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id, chunk_hash, prompt_version) DO UPDATE SET
                  extractor = excluded.extractor,
                  doc_id = excluded.doc_id
                """,
                (chunk_id, doc_id, chunk_hash, prompt_version, extractor, _now()),
            )
            self._conn.commit()

    def clear_extractions(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM extractions")
            self._conn.commit()

    def upsert_edge(
        self,
        *,
        src_id: str,
        dst_id: str,
        rel_type: str,
        provenance: str,
        evidence: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO concept_edges(
                  src_id, dst_id, type, provenance, evidence, status
                ) VALUES (?, ?, ?, ?, ?, 'active')
                ON CONFLICT(src_id, dst_id, type) DO UPDATE SET
                  provenance = excluded.provenance,
                  evidence = excluded.evidence,
                  status = 'active'
                """,
                (src_id, dst_id, rel_type, provenance, evidence),
            )
            self._conn.commit()

    def list_edges(self, src_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM concept_edges WHERE status = 'active'"
        params: list = []
        if src_id is not None:
            sql += " AND src_id = ?"
            params.append(src_id)
        sql += " ORDER BY src_id, type, dst_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def add_suggestion(self, src_id: str, dst_id: str, score: float) -> None:
        left, right = sorted((src_id, dst_id))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO merge_suggestions(src_id, dst_id, score, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT(src_id, dst_id) DO UPDATE SET
                  score = excluded.score
                WHERE merge_suggestions.status = 'pending'
                """,
                (left, right, float(score), _now()),
            )
            self._conn.commit()

    def list_suggestions(self, status: str = "pending") -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM merge_suggestions
                WHERE status = ?
                ORDER BY score DESC, src_id
                """,
                (status,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_suggestion(self, src_id: str, dst_id: str, status: str) -> None:
        left, right = sorted((src_id, dst_id))
        with self._lock:
            self._conn.execute(
                """
                UPDATE merge_suggestions SET status = ?
                WHERE src_id = ? AND dst_id = ?
                """,
                (status, left, right),
            )
            self._conn.commit()

    def merge_concepts(self, keep_id: str, drop_id: str) -> dict:
        """把 drop 并进 keep。只改派生行，不改源文件。"""
        if keep_id == drop_id:
            raise KBError("不能把概念和自己合并")
        with self._lock:
            keep = self.find_concept(keep_id)
            drop = self.find_concept(drop_id)
            if keep is None or drop is None:
                raise NotFoundError("概念不存在")
            if keep["status"] != "active" or drop["status"] != "active":
                raise KBError("只能合并仍有效的概念")
            aliases = list(keep["aliases"])
            for item in [drop["name"], *drop["aliases"]]:
                if item and item != keep["name"] and item not in aliases:
                    aliases.append(item)
            self._conn.execute(
                """
                UPDATE concepts
                SET aliases_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(aliases, ensure_ascii=False), _now(), keep_id),
            )
            mentions = self._conn.execute(
                "SELECT * FROM mentions WHERE concept_id = ?", (drop_id,)
            ).fetchall()
            for mention in mentions:
                self._conn.execute(
                    """
                    INSERT INTO mentions(id, concept_id, doc_id, chunk_id, snippet, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(concept_id, doc_id, chunk_id, snippet) DO NOTHING
                    """,
                    (
                        _mention_id(keep_id, mention["doc_id"], mention["chunk_id"], mention["snippet"]),
                        keep_id,
                        mention["doc_id"],
                        mention["chunk_id"],
                        mention["snippet"],
                        mention["created_at"],
                    ),
                )
            self._conn.execute("DELETE FROM mentions WHERE concept_id = ?", (drop_id,))
            edges = self._conn.execute(
                """
                SELECT * FROM concept_edges
                WHERE src_id = ? OR dst_id = ?
                """,
                (drop_id, drop_id),
            ).fetchall()
            self._conn.execute(
                "DELETE FROM concept_edges WHERE src_id = ? OR dst_id = ?",
                (drop_id, drop_id),
            )
            for edge in edges:
                src = keep_id if edge["src_id"] == drop_id else edge["src_id"]
                dst = keep_id if edge["dst_id"] == drop_id else edge["dst_id"]
                if src == dst:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO concept_edges(
                      src_id, dst_id, type, confidence, provenance, evidence, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active')
                    ON CONFLICT(src_id, dst_id, type) DO NOTHING
                    """,
                    (
                        src,
                        dst,
                        edge["type"],
                        edge["confidence"],
                        edge["provenance"],
                        edge["evidence"],
                    ),
                )
            self._conn.execute(
                """
                UPDATE concepts
                SET status = 'merged', merged_into = ?, updated_at = ?
                WHERE id = ?
                """,
                (keep_id, _now(), drop_id),
            )
            self.mark_suggestion(keep_id, drop_id, "confirmed")
            self._conn.commit()
        merged = self.find_concept(keep_id)
        assert merged is not None
        return merged

    def replace_outline(self, domain: str, nodes: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM outline_nodes WHERE domain = ?", (domain,))
            for node in nodes:
                self._conn.execute(
                    """
                    INSERT INTO outline_nodes(
                      id, domain, name, normalized, aliases_json, parent_id, ord, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node["id"],
                        node["domain"],
                        node["name"],
                        node["normalized"],
                        json.dumps(node.get("aliases") or [], ensure_ascii=False),
                        node.get("parent_id"),
                        int(node.get("ord") or 0),
                        node.get("source") or "",
                    ),
                )
            self._conn.commit()

    def list_outline(self, domain: str | None = None) -> list[dict]:
        sql = "SELECT * FROM outline_nodes"
        params: tuple = ()
        if domain:
            sql += " WHERE domain = ?"
            params = (domain,)
        sql += " ORDER BY ord, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["aliases"] = json.loads(item.pop("aliases_json") or "[]")
            result.append(item)
        return result

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def get_meta_value(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def table_names(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {row["name"] for row in rows}


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


def _concept_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "normalized": row["normalized"],
        "domain": row["domain"],
        "definition": row["definition"],
        "aliases": json.loads(row["aliases_json"] or "[]"),
        "entity_type": row["entity_type"],
        "status": row["status"],
        "merged_into": row["merged_into"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _mention_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "concept_id": row["concept_id"],
        "doc_id": row["doc_id"],
        "chunk_id": row["chunk_id"],
        "snippet": row["snippet"],
        "created_at": row["created_at"],
    }


def _mention_id(concept_id: str, doc_id: str, chunk_id: str, snippet: str) -> str:
    digest = hashlib.sha256(
        f"{concept_id}\n{doc_id}\n{chunk_id}\n{snippet}".encode("utf-8")
    ).hexdigest()[:16]
    return f"m_{digest}"

