"""文档库：SQLite 适配器（实现 DocumentStore Protocol）。

绑定源文件的文档不保存整篇正文，只保存路径、哈希、摘要与切块定位。
无外部原件的 inline 正文，以及源文件已丢失的 orphaned_copy，仍留在本库。
SQL 全部收敛在本模块，外部只依赖 Document 模型与中立 Filter。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from ..models import (
    SOURCE_BOUND,
    ChunkLocation,
    Document,
    MediaRef,
)
from ..storage.filters import Where, compile_sql

logger = logging.getLogger(__name__)

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
  media TEXT DEFAULT '[]',
  scope TEXT DEFAULT 'default',
  status TEXT DEFAULT 'active',
  summary TEXT DEFAULT '',
  source_kind TEXT DEFAULT 'inline',
  chunk_locations TEXT DEFAULT '[]'
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
        "scope",
        "status",
        "source_kind",
    }
)


def _load_locations(raw: str | None) -> list[ChunkLocation]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [ChunkLocation.from_dict(item) for item in data if isinstance(item, dict)]


def _dump_locations(locations: list[ChunkLocation]) -> str:
    return json.dumps([loc.to_dict() for loc in locations], ensure_ascii=False)


class SQLiteDocumentStore:
    """父文档库：按 doc_id 存取、哈希去重查询、列表与统计。

    ``source_kind=bound`` 时不落整篇正文（写入会被清空）。
    """

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
            # 旧库迁移：补充 scope / status 列
            cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(documents)")
            }
            if "scope" not in cols:
                self._conn.execute(
                    "ALTER TABLE documents ADD COLUMN scope TEXT DEFAULT 'default'"
                )
            if "status" not in cols:
                self._conn.execute(
                    "ALTER TABLE documents ADD COLUMN status TEXT DEFAULT 'active'"
                )
            # 旧库默认 legacy：父全文还在，等 migrate-parents 分类
            if "summary" not in cols:
                self._conn.execute(
                    "ALTER TABLE documents ADD COLUMN summary TEXT DEFAULT ''"
                )
            if "source_kind" not in cols:
                self._conn.execute(
                    "ALTER TABLE documents ADD COLUMN source_kind TEXT DEFAULT 'legacy'"
                )
            if "chunk_locations" not in cols:
                self._conn.execute(
                    "ALTER TABLE documents ADD COLUMN chunk_locations TEXT DEFAULT '[]'"
                )
            self._conn.commit()

    # ---- 写 ----
    def upsert(self, doc: Document) -> None:
        # 绑定原件不把整篇正文写入应用库
        full_text = "" if doc.source_kind == SOURCE_BOUND else doc.full_text
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO documents(
                  doc_id, title, source_path, doc_type, content_hash,
                  metadata, collection, created_at, updated_at, full_text,
                  chunk_ids, media, scope, status, summary, source_kind,
                  chunk_locations
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
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
                    full_text,
                    json.dumps(doc.chunk_ids),
                    json.dumps(
                        [m.to_metadata() for m in doc.media], ensure_ascii=False
                    ),
                    doc.scope,
                    doc.status,
                    doc.summary,
                    doc.source_kind,
                    _dump_locations(doc.chunk_locations),
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

    def set_status(self, doc_id: str, status: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE documents SET status = ? WHERE doc_id = ?",
                (status, doc_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ---- 读 ----
    @staticmethod
    def _row_to_doc(row: sqlite3.Row) -> Document:
        keys = row.keys()
        media = [
            MediaRef.from_metadata(m) or MediaRef()
            for m in json.loads(row["media"] or "[]")
        ]
        kind = row["source_kind"] if "source_kind" in keys else "legacy"
        return Document(
            doc_id=row["doc_id"],
            title=row["title"],
            full_text=row["full_text"] or "",
            source_path=row["source_path"],
            doc_type=row["doc_type"],
            content_hash=row["content_hash"],
            metadata=json.loads(row["metadata"] or "{}"),
            collection=row["collection"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            chunk_ids=json.loads(row["chunk_ids"] or "[]"),
            media=media,
            scope=(row["scope"] or "default") if "scope" in keys else "default",
            status=(row["status"] or "active") if "status" in keys else "active",
            summary=(row["summary"] or "") if "summary" in keys else "",
            source_kind=kind or "legacy",
            chunk_locations=_load_locations(
                row["chunk_locations"] if "chunk_locations" in keys else "[]"
            ),
        )

    def get(self, doc_id: str) -> Document | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return self._row_to_doc(row) if row else None

    def list_by_hash(self, content_hash: str) -> list[Document]:
        """同一 ``content_hash`` 的全部行。空哈希不查，避免把未填哈希的文档扫出来。"""
        key = (content_hash or "").strip()
        if not key:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM documents WHERE content_hash = ? ORDER BY doc_id",
                (key,),
            ).fetchall()
        return [self._row_to_doc(row) for row in rows]

    def get_by_hash(self, content_hash: str) -> Document | None:
        """按哈希取一行用于去重。

        只有一行时返回它。多行但全文相同，视为真重复，返回第一行。
        多行且全文不一致（截断哈希碰撞，或库已损坏）时不任取一行，返回 None，
        调用方应继续入库或告警，而不是跳过。
        """
        rows = self.list_by_hash(content_hash)
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0]
        bodies = {(row.full_text or "") for row in rows}
        ids = [row.doc_id for row in rows]
        if len(bodies) == 1:
            logger.warning(
                "content_hash 命中 %d 行且全文相同，按重复处理: %s",
                len(rows),
                ids,
            )
            return rows[0]
        logger.warning(
            "content_hash 碰撞且全文不一致，拒绝任取一行: %s",
            ids,
        )
        return None

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
            by_source_kind = {
                (r["source_kind"] or "legacy"): r["c"]
                for r in self._conn.execute(
                    "SELECT source_kind, COUNT(*) AS c FROM documents GROUP BY source_kind"
                ).fetchall()
            }
        return {
            "documents": total,
            "chunks": total_chunks,
            "by_collection": by_collection,
            "by_source_kind": by_source_kind,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True
