"""入库/删除的持久化日志。

向量库和 SQLite 不在同一个事务里。破坏性修改之前把可回滚的快照写进
``ingest_journal``；成功后删掉该行。进程在中途退出时，行还在，``repair``
可以按快照恢复旧向量，或清掉没有父文档的向量。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import ChunkLocation, Document, MediaRef
from ..vectorstore.base import StoredVector

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  op TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  collection TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_meta(meta: dict | None) -> dict:
    """快照元数据：只留 JSON 能存、Chroma 能写回的标量。"""
    out: dict = {}
    for key, value in (meta or {}).items():
        if isinstance(value, bool) or isinstance(value, (str, int, float)):
            out[key] = value
        elif hasattr(value, "item"):
            out[key] = value.item()
    return out


def dump_document(doc: Document) -> dict[str, Any]:
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "full_text": doc.full_text,
        "source_path": doc.source_path,
        "doc_type": doc.doc_type,
        "content_hash": doc.content_hash,
        "metadata": doc.metadata,
        "collection": doc.collection,
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
        "chunk_ids": list(doc.chunk_ids),
        "media": [item.to_metadata() for item in doc.media],
        "scope": doc.scope,
        "status": doc.status,
        "summary": doc.summary,
        "source_kind": doc.source_kind,
        "chunk_locations": [loc.to_dict() for loc in doc.chunk_locations],
    }


def load_document(data: dict[str, Any]) -> Document:
    media: list[MediaRef] = []
    for item in data.get("media") or []:
        if not isinstance(item, dict):
            continue
        ref = MediaRef.from_metadata(item)
        if ref is not None:
            media.append(ref)
    locations = [
        ChunkLocation.from_dict(item)
        for item in (data.get("chunk_locations") or [])
        if isinstance(item, dict)
    ]
    return Document(
        doc_id=str(data.get("doc_id") or ""),
        title=str(data.get("title") or ""),
        full_text=str(data.get("full_text") or ""),
        source_path=str(data.get("source_path") or ""),
        doc_type=str(data.get("doc_type") or "text"),
        content_hash=str(data.get("content_hash") or ""),
        metadata=dict(data.get("metadata") or {}),
        collection=str(data.get("collection") or "personal"),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        chunk_ids=list(data.get("chunk_ids") or []),
        media=media,
        scope=str(data.get("scope") or "default"),
        status=str(data.get("status") or "active"),
        summary=str(data.get("summary") or ""),
        source_kind=str(data.get("source_kind") or "inline"),
        chunk_locations=locations,
    )


def dump_vector(row: StoredVector) -> dict[str, Any]:
    embedding = None
    if row.embedding is not None:
        embedding = [float(value) for value in row.embedding]
    return {
        "id": row.id,
        "document": row.document,
        "metadata": _json_meta(row.metadata),
        "embedding": embedding,
    }


def load_vector(data: dict[str, Any]) -> StoredVector:
    raw = data.get("embedding")
    embedding = [float(value) for value in raw] if raw else None
    return StoredVector(
        id=str(data.get("id") or ""),
        document=str(data.get("document") or ""),
        metadata=dict(data.get("metadata") or {}),
        embedding=embedding,
    )


@dataclass
class JournalEntry:
    id: int
    op: str
    doc_id: str
    collection: str
    payload: dict[str, Any]


class IngestJournal:
    """``documents.sqlite3`` 里的写入日志。一行表示一次还没完成的变更。"""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def begin(self, op: str, doc_id: str, collection: str, payload: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO ingest_journal(op, doc_id, collection, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    op,
                    doc_id,
                    collection,
                    json.dumps(payload, ensure_ascii=False),
                    _now(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish(self, journal_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM ingest_journal WHERE id = ?", (int(journal_id),)
            )
            self._conn.commit()

    def pending(self) -> list[JournalEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ingest_journal ORDER BY id"
            ).fetchall()
        entries: list[JournalEntry] = []
        for row in rows:
            entries.append(
                JournalEntry(
                    id=int(row["id"]),
                    op=row["op"],
                    doc_id=row["doc_id"],
                    collection=row["collection"],
                    payload=json.loads(row["payload_json"] or "{}"),
                )
            )
        return entries

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True
