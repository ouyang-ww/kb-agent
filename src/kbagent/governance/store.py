"""治理存储：docs_meta、doc_events、consumer_cursor。

与 documents.sqlite3 共用数据库文件，独立连接（WAL），负责：
- 文档级 scope / status / 时效元数据
- 变更事件 outbox
- consumer 游标
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs_meta (
  doc_id   TEXT PRIMARY KEY,
  content_hash TEXT DEFAULT '',
  scope    TEXT NOT NULL DEFAULT 'default',
  status   TEXT NOT NULL DEFAULT 'active',
  authority INTEGER NOT NULL DEFAULT 3,
  valid_from TEXT,
  valid_until TEXT,
  review_by TEXT,
  last_verified TEXT,
  simhash TEXT,
  exclude_from_learning INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id       TEXT NOT NULL,
  event        TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL,
  processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_pending ON doc_events(processed_at, id);
CREATE TABLE IF NOT EXISTS consumer_cursor (
  consumer   TEXT PRIMARY KEY,
  last_event INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS answer_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  query TEXT NOT NULL,
  rewritten_query TEXT DEFAULT '',
  retrieved_json TEXT DEFAULT '[]',
  answer TEXT NOT NULL,
  citations_json TEXT DEFAULT '[]',
  meta_json TEXT DEFAULT '{}',
  confidence TEXT,
  refusal_reason TEXT,
  latency_ms INTEGER,
  feedback INTEGER,
  verification_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_answer_log_ts ON answer_log(ts);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT DEFAULT 'local',
  action TEXT NOT NULL,
  target TEXT DEFAULT '',
  detail_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS embedding_manifest (
  id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  revision TEXT DEFAULT '',
  dim INTEGER NOT NULL DEFAULT 0,
  normalize INTEGER NOT NULL DEFAULT 1,
  chunker_version TEXT NOT NULL DEFAULT 'c1',
  collection TEXT NOT NULL DEFAULT 'personal',
  status TEXT NOT NULL DEFAULT 'active',
  doc_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  activated_at TEXT
);
CREATE TABLE IF NOT EXISTS runtime_pointer (
  name TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_dead_letter (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  consumer TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  error TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dead_letter_consumer
  ON consumer_dead_letter(consumer, event_id);
CREATE TABLE IF NOT EXISTS doc_relations (
  src_id     TEXT NOT NULL,
  dst_id     TEXT NOT NULL,
  type       TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  created_by TEXT DEFAULT '',
  status     TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  PRIMARY KEY (src_id, dst_id, type)
);
CREATE INDEX IF NOT EXISTS idx_relations_status ON doc_relations(status, type);
CREATE TABLE IF NOT EXISTS near_duplicates (
  kind         TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  duplicate_of TEXT NOT NULL,
  method       TEXT NOT NULL,
  score        REAL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (kind, source_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


@dataclass
class DocMeta:
    doc_id: str
    content_hash: str = ""
    scope: str = "default"
    status: str = "active"
    authority: int = 3
    valid_from: str | None = None
    valid_until: str | None = None
    review_by: str | None = None
    last_verified: str | None = None
    simhash: str | None = None
    exclude_from_learning: bool = False
    updated_at: str = field(default_factory=_now)


class GovernanceStore:
    """轻量治理存储（单进程下线程安全）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """旧库补列。CREATE TABLE IF NOT EXISTS 不会给已有表加字段。"""
        self._ensure_column("docs_meta", "last_verified", "TEXT")
        self._ensure_column("docs_meta", "simhash", "TEXT")
        self._ensure_column("answer_log", "verification_json", "TEXT")

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {row["name"] for row in rows}
        if column not in names:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # ---- docs_meta ----
    def upsert_meta(self, meta: DocMeta) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO docs_meta(
                  doc_id, content_hash, scope, status, authority,
                  valid_from, valid_until, review_by, last_verified, simhash,
                  exclude_from_learning, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(doc_id) DO UPDATE SET
                  content_hash=excluded.content_hash,
                  scope=excluded.scope,
                  status=excluded.status,
                  authority=excluded.authority,
                  valid_from=excluded.valid_from,
                  valid_until=excluded.valid_until,
                  review_by=excluded.review_by,
                  last_verified=excluded.last_verified,
                  simhash=excluded.simhash,
                  exclude_from_learning=excluded.exclude_from_learning,
                  updated_at=excluded.updated_at
                """,
                (
                    meta.doc_id,
                    meta.content_hash,
                    meta.scope,
                    meta.status,
                    meta.authority,
                    meta.valid_from,
                    meta.valid_until,
                    meta.review_by,
                    meta.last_verified,
                    meta.simhash,
                    int(meta.exclude_from_learning),
                    meta.updated_at,
                ),
            )
            self._conn.commit()

    def get_meta(self, doc_id: str) -> DocMeta | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM docs_meta WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return self._row_to_meta(row) if row else None

    @staticmethod
    def _row_to_meta(row: sqlite3.Row) -> DocMeta:
        return DocMeta(
            doc_id=row["doc_id"],
            content_hash=row["content_hash"] or "",
            scope=row["scope"] or "default",
            status=row["status"] or "active",
            authority=row["authority"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            review_by=row["review_by"],
            last_verified=row["last_verified"] if "last_verified" in row.keys() else None,
            simhash=row["simhash"] if "simhash" in row.keys() else None,
            exclude_from_learning=bool(row["exclude_from_learning"]),
            updated_at=row["updated_at"],
        )

    def set_status(self, doc_id: str, status: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE docs_meta SET status = ?, updated_at = ? WHERE doc_id = ?",
                (status, _now(), doc_id),
            )
            if cur.rowcount == 0:
                self.upsert_meta(DocMeta(doc_id=doc_id, status=status))
            self._conn.commit()
        return True

    def mark_deleted(self, doc_id: str) -> None:
        self.set_status(doc_id, "deleted")

    # ---- doc_events ----
    def emit_event(self, doc_id: str, event: str, payload: dict[str, Any] | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO doc_events(doc_id, event, payload_json, created_at) VALUES (?,?,?,?)",
                (doc_id, event, json.dumps(payload or {}, ensure_ascii=False), _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_events(
        self,
        after_id: int = 0,
        limit: int = 100,
        unprocessed_only: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM doc_events WHERE id > ?"
        params: list[Any] = [after_id]
        if unprocessed_only:
            sql += " AND processed_at IS NULL"
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "doc_id": row["doc_id"],
                "event": row["event"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "created_at": row["created_at"],
                "processed_at": row["processed_at"],
            }
            for row in rows
        ]

    def mark_event_processed(self, event_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE doc_events SET processed_at = ? WHERE id = ?",
                (_now(), event_id),
            )
            self._conn.commit()

    # ---- consumer cursor ----
    def get_cursor(self, consumer: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_event FROM consumer_cursor WHERE consumer = ?",
                (consumer,),
            ).fetchone()
        return int(row["last_event"]) if row else 0

    def record_dead_letter(self, consumer: str, event_id: int, error: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO consumer_dead_letter(consumer, event_id, error, created_at)
                VALUES (?,?,?,?)
                """,
                (consumer, int(event_id), error[:2000], _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_dead_letters(
        self, consumer: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM consumer_dead_letter"
        params: list[Any] = []
        if consumer:
            sql += " WHERE consumer = ?"
            params.append(consumer)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "consumer": row["consumer"],
                "event_id": row["event_id"],
                "error": row["error"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def set_cursor(self, consumer: str, last_event: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO consumer_cursor(consumer, last_event) VALUES (?, ?)
                ON CONFLICT(consumer) DO UPDATE SET last_event=excluded.last_event
                """,
                (consumer, int(last_event)),
            )
            self._conn.commit()

    # ---- answer_log ----
    def log_answer(
        self,
        query: str,
        answer: str,
        rewritten_query: str = "",
        retrieved: list[dict[str, Any]] | None = None,
        citations: list[str] | None = None,
        meta: dict[str, Any] | None = None,
        confidence: str | None = None,
        refusal_reason: str | None = None,
        latency_ms: int | None = None,
        verification: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO answer_log(
                  ts, query, rewritten_query, retrieved_json,
                  answer, citations_json, meta_json, confidence,
                  refusal_reason, latency_ms, verification_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _now(),
                    query,
                    rewritten_query,
                    json.dumps(retrieved or [], ensure_ascii=False),
                    answer,
                    json.dumps(citations or [], ensure_ascii=False),
                    json.dumps(meta or {}, ensure_ascii=False),
                    confidence,
                    refusal_reason,
                    latency_ms,
                    json.dumps(verification, ensure_ascii=False) if verification is not None else None,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_answers(self, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM answer_log WHERE id > ? ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "query": row["query"],
                "rewritten_query": row["rewritten_query"],
                "retrieved": json.loads(row["retrieved_json"] or "[]"),
                "answer": row["answer"],
                "citations": json.loads(row["citations_json"] or "[]"),
                "meta": json.loads(row["meta_json"] or "{}"),
                "confidence": row["confidence"],
                "refusal_reason": row["refusal_reason"],
                "latency_ms": row["latency_ms"],
                "feedback": row["feedback"],
                "verification": _loads(row["verification_json"])
                if "verification_json" in row.keys() and row["verification_json"]
                else None,
            }
            for row in rows
        ]

    # ---- audit_log ----
    def record_audit(
        self,
        action: str,
        target: str = "",
        actor: str = "local",
        detail: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit_log(ts, actor, action, target, detail_json) VALUES (?,?,?,?,?)",
                (_now(), actor, action, target, json.dumps(detail or {}, ensure_ascii=False)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_audit(self, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log WHERE id > ? ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "actor": row["actor"],
                "action": row["action"],
                "target": row["target"],
                "detail": json.loads(row["detail_json"] or "{}"),
            }
            for row in rows
        ]

    # ---- embedding manifest / runtime pointer ----
    def upsert_manifest(
        self,
        manifest_id: str,
        model: str,
        chunker_version: str,
        collection: str,
        revision: str = "",
        dim: int = 0,
        normalize: bool = True,
        status: str = "active",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO embedding_manifest(
                  id, model, revision, dim, normalize, chunker_version,
                  collection, status, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  model=excluded.model,
                  revision=excluded.revision,
                  dim=excluded.dim,
                  normalize=excluded.normalize,
                  chunker_version=excluded.chunker_version,
                  collection=excluded.collection,
                  status=excluded.status
                """,
                (
                    manifest_id,
                    model,
                    revision,
                    int(dim),
                    int(bool(normalize)),
                    chunker_version,
                    collection,
                    status,
                    _now(),
                ),
            )
            self._conn.commit()

    def activate_manifest(self, manifest_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE embedding_manifest SET status = 'inactive' WHERE id != ?",
                (manifest_id,),
            )
            self._conn.execute(
                "UPDATE embedding_manifest SET status = 'active', activated_at = ? WHERE id = ?",
                (_now(), manifest_id),
            )
            self.set_pointer("active_embedding", manifest_id)
            self._conn.commit()

    def get_manifest(self, manifest_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM embedding_manifest WHERE id = ?", (manifest_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_active_manifest(self) -> dict[str, Any] | None:
        pointer = self.get_pointer("active_embedding")
        return self.get_manifest(pointer) if pointer else None

    def increment_manifest_docs(self, manifest_id: str, delta: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE embedding_manifest
                SET doc_count = MAX(0, doc_count + ?) WHERE id = ?
                """,
                (int(delta), manifest_id),
            )
            self._conn.commit()

    def set_pointer(self, name: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runtime_pointer(name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value=excluded.value
                """,
                (name, value),
            )
            self._conn.commit()

    def get_pointer(self, name: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM runtime_pointer WHERE name = ?", (name,)
            ).fetchone()
        return row["value"] if row else None

    # ---- 关系：pending 不生效，确认后才改状态 ----
    def propose_relation(
        self,
        src_id: str,
        dst_id: str,
        rel_type: str,
        *,
        confidence: float = 1.0,
        created_by: str = "hook",
    ) -> dict[str, Any] | None:
        """写入 pending。已经确认或拒绝的同一三元组不降级、不重开。"""
        if not src_id or not dst_id or src_id == dst_id:
            return None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO doc_relations(
                  src_id, dst_id, type, confidence, created_by, status, created_at
                ) VALUES (?,?,?,?,?,'pending',?)
                ON CONFLICT(src_id, dst_id, type) DO UPDATE SET
                  confidence=excluded.confidence,
                  created_by=excluded.created_by
                WHERE doc_relations.status = 'pending'
                """,
                (src_id, dst_id, rel_type, float(confidence), created_by, _now()),
            )
            self._conn.commit()
        return self.get_relation(src_id, dst_id, rel_type)

    def get_relation(self, src_id: str, dst_id: str, rel_type: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM doc_relations
                WHERE src_id = ? AND dst_id = ? AND type = ?
                """,
                (src_id, dst_id, rel_type),
            ).fetchone()
        return _relation_row(row) if row else None

    def list_relations(
        self,
        *,
        status: str | None = None,
        rel_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM doc_relations WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if rel_type:
            sql += " AND type = ?"
            params.append(rel_type)
        sql += " ORDER BY created_at, src_id LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_relation_row(row) for row in rows]

    def set_relation_status(
        self, src_id: str, dst_id: str, rel_type: str, status: str
    ) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE doc_relations SET status = ?
                WHERE src_id = ? AND dst_id = ? AND type = ?
                """,
                (status, src_id, dst_id, rel_type),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_relation(src_id, dst_id, rel_type)

    def confirmed_successor(self, doc_id: str) -> str | None:
        """谁已确认取代了这篇。多条时取得分高、写入晚的那条。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT src_id FROM doc_relations
                WHERE dst_id = ? AND type = 'supersedes' AND status = 'confirmed'
                ORDER BY confidence DESC, created_at DESC
                LIMIT 1
                """,
                (doc_id,),
            ).fetchone()
        return row["src_id"] if row else None

    def latest_doc(self, doc_id: str) -> str:
        seen: set[str] = set()
        current = doc_id
        while current and current not in seen:
            seen.add(current)
            nxt = self.confirmed_successor(current)
            if not nxt or nxt == current:
                return current
            current = nxt
        return current or doc_id

    def confirmed_contradictions(self, doc_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT src_id, dst_id FROM doc_relations
                WHERE status = 'confirmed' AND type = 'contradicts'
                  AND (src_id = ? OR dst_id = ?)
                """,
                (doc_id, doc_id),
            ).fetchall()
        others: list[str] = []
        for row in rows:
            other = row["dst_id"] if row["src_id"] == doc_id else row["src_id"]
            if other and other not in others:
                others.append(other)
        return others

    def iter_simhashes(self) -> list[tuple[str, str, str | None]]:
        """活跃文档的 (doc_id, simhash, valid_until)。隐藏和已删除的不参与近似去重。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT doc_id, simhash, valid_until FROM docs_meta
                WHERE status = 'active' AND simhash IS NOT NULL AND simhash != ''
                """
            ).fetchall()
        return [(row["doc_id"], row["simhash"], row["valid_until"]) for row in rows]

    # ---- 近似重复映射。不是合并，只记录 duplicate_of ----
    def record_near_duplicate(
        self,
        kind: str,
        source_id: str,
        duplicate_of: str,
        method: str,
        score: float | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO near_duplicates(
                  kind, source_id, duplicate_of, method, score, created_at
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(kind, source_id) DO UPDATE SET
                  duplicate_of=excluded.duplicate_of,
                  method=excluded.method,
                  score=excluded.score,
                  created_at=excluded.created_at
                """,
                (kind, source_id, duplicate_of, method, score, _now()),
            )
            self._conn.commit()

    def list_near_duplicates(
        self, *, kind: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM near_duplicates"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "kind": row["kind"],
                "source_id": row["source_id"],
                "duplicate_of": row["duplicate_of"],
                "method": row["method"],
                "score": row["score"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _relation_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "src_id": row["src_id"],
        "dst_id": row["dst_id"],
        "type": row["type"],
        "confidence": row["confidence"],
        "created_by": row["created_by"],
        "status": row["status"],
        "created_at": row["created_at"],
    }
