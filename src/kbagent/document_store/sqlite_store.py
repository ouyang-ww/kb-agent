"""文档库：SQLite 适配器（实现 DocumentStore Protocol）。

存父文档全文（子母文档中的「母」），支持哈希去重查询、列表/过滤与统计。
SQL 全部收敛在本模块，外部只依赖 Document 模型与中立 Filter。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from ..models import Document, MediaRef
from ..storage.filters import Where, compile_sql

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_path TEXT DEFAULT '',
  doc_type TEXT DEFAULT 'text',
  content_hash TEXT DEFAULT '',
  metadata TEXT DEFAULT '{}',
  collection TEXT DEFAULT 'personal',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  full_text TEXT NOT NULL,
  chunk_ids TEXT DEFAULT '[]',
  media TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_docs_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_docs_collection ON documents(collection);
"""

# 允许通过 DocumentStore.list(where=...) 过滤的列（白名单，防注入）
_FILTER_FIELDS = frozenset(
    {
        "doc_id",
        "title",
        "source_path",
        "doc_type",
        "content_hash",
        "collection",
        "created_at",
        "updated_at",
    }
)


class SQLiteDocumentStore:
    """父文档全文库：按 doc_id 存取、哈希去重查询、列表与统计。"""

    def __init__(self, path: str | Any) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._closed = False
        with self._lock:
            # 单机多读单写：WAL + busy_timeout，降低后台任务与前台查询的锁冲突
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- 写 ----
    def upsert(self, doc: Document) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    doc.doc_id,
                    doc.title,
                    doc.source_path,
                    doc.doc_type,
                    doc.content_hash,
                    json.dumps(doc.metadata, ensure_ascii=False),
                    doc.collection,
                    doc.created_at,
                    doc.updated_at,
                    doc.full_text,
                    json.dumps(doc.chunk_ids),
                    json.dumps(
                        [m.to_metadata() for m in doc.media], ensure_ascii=False
                    ),
                ),
            )
            self._conn.commit()

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM documents WHERE doc_id = ?", (doc_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ---- 读 ----
    @staticmethod
    def _row_to_doc(row: sqlite3.Row) -> Document:
        media = [
            MediaRef.from_metadata(m) or MediaRef()
            for m in json.loads(row["media"] or "[]")
        ]
        return Document(
            doc_id=row["doc_id"],
            title=row["title"],
            full_text=row["full_text"],
            source_path=row["source_path"],
            doc_type=row["doc_type"],
            content_hash=row["content_hash"],
            metadata=json.loads(row["metadata"] or "{}"),
            collection=row["collection"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            chunk_ids=json.loads(row["chunk_ids"] or "[]"),
            media=media,
        )

    def get(self, doc_id: str) -> Document | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return self._row_to_doc(row) if row else None

    def get_by_hash(self, content_hash: str) -> Document | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        return self._row_to_doc(row) if row else None

    def list(
        self,
        collection: str | None = None,
        where: Where = None,
    ) -> list[Document]:
        clauses: list[str] = []
        params: list[Any] = []
        if collection:
            clauses.append("collection = ?")
            params.append(collection)
        sql_where, where_params = compile_sql(where, _FILTER_FIELDS)
        if sql_where != "1=1":
            clauses.append(f"({sql_where})")
            params.extend(where_params)
        query = "SELECT * FROM documents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_doc(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]
            by_collection = {
                r["collection"]: r["c"]
                for r in self._conn.execute(
                    "SELECT collection, COUNT(*) AS c FROM documents GROUP BY collection"
                ).fetchall()
            }
            total_chunks = self._conn.execute(
                "SELECT COALESCE(SUM(json_array_length(chunk_ids)), 0) FROM documents"
            ).fetchone()[0]
        return {
            "documents": total,
            "chunks": total_chunks,
            "by_collection": by_collection,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True
