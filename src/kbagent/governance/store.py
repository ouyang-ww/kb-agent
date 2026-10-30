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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            self._conn.commit()

    # ---- docs_meta ----
    def upsert_meta(self, meta: DocMeta) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO docs_meta(
                  doc_id, content_hash, scope, status, authority,
                  valid_from, valid_until, review_by,
                  exclude_from_learning, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(doc_id) DO UPDATE SET
                  content_hash=excluded.content_hash,
                  scope=excluded.scope,
                  status=excluded.status,
                  authority=excluded.authority,
                  valid_from=excluded.valid_from,
                  valid_until=excluded.valid_until,
                  review_by=excluded.review_by,
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
