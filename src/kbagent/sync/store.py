"""sync.sqlite3：许可、设备稳定 id、本机路径映射、记录稳定 id。

不保存原件全文，也不保存切块正文。调用方只在使用同步预留时打开本库。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..errors import KBError, NotFoundError
from .ids import KIND_DEVICE, KIND_RECORD, KIND_VAULT, mint_stable_id, parse_stable_id
from .permissions import GRANT_NAMES, empty_grants

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  stable_id  TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
  name       TEXT PRIMARY KEY,
  granted    INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS path_maps (
  stable_id  TEXT PRIMARY KEY,
  local_path TEXT NOT NULL UNIQUE,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
  stable_id     TEXT PRIMARY KEY,
  vault_id      TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  local_doc_id  TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  UNIQUE (vault_id, relative_path)
);
"""

_MAX_LOCAL_DOC_ID = 128


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_local_doc_id(value: str) -> str:
    text = (value or "").strip()
    if len(text) > _MAX_LOCAL_DOC_ID or "\n" in text or "\x00" in text:
        raise KBError("本机文档 id 只作本机指针，不能写入正文")
    return text


class SyncStore:
    """同步预留库。线程安全。主键是稳定 id，本机路径只是映射列。"""

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
            self._ensure_device()

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

    def _ensure_device(self) -> str:
        row = self._conn.execute(
            "SELECT stable_id FROM device WHERE id = 1"
        ).fetchone()
        if row is not None:
            return str(row["stable_id"])
        stable_id = mint_stable_id(KIND_DEVICE)
        try:
            self._conn.execute(
                "INSERT INTO device(id, stable_id, created_at) VALUES (1, ?, ?)",
                (stable_id, _now()),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT stable_id FROM device WHERE id = 1"
            ).fetchone()
            if row is None:
                raise
            return str(row["stable_id"])
        return stable_id

    def device_id(self) -> str:
        with self._lock:
            self._check_open()
            return self._ensure_device()

    def grants(self) -> dict[str, bool]:
        found = empty_grants()
        with self._lock:
            self._check_open()
            rows = self._conn.execute("SELECT name, granted FROM grants").fetchall()
        for row in rows:
            if row["name"] in found:
                found[row["name"]] = bool(row["granted"])
        return found

    def set_grant(self, name: str, granted: bool) -> None:
        if name not in GRANT_NAMES:
            raise KBError("未知的同步许可。可选：remote、lan、health-remote")
        with self._lock:
            self._check_open()
            self._conn.execute(
                """
                INSERT INTO grants(name, granted, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  granted=excluded.granted,
                  updated_at=excluded.updated_at
                """,
                (name, int(bool(granted)), _now()),
            )
            self._conn.commit()

    def bind_root(self, local_path: str) -> str:
        with self._lock:
            self._check_open()
            existing = self._conn.execute(
                "SELECT stable_id FROM path_maps WHERE local_path = ?",
                (local_path,),
            ).fetchone()
            if existing is not None:
                return str(existing["stable_id"])
            stable_id = mint_stable_id(KIND_VAULT)
            try:
                self._conn.execute(
                    """
                    INSERT INTO path_maps(stable_id, local_path, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (stable_id, local_path, _now()),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                existing = self._conn.execute(
                    "SELECT stable_id FROM path_maps WHERE local_path = ?",
                    (local_path,),
                ).fetchone()
                if existing is None:
                    raise KBError("本机路径映射冲突")
                return str(existing["stable_id"])
            return stable_id

    def rebind_root(self, stable_id: str, local_path: str) -> None:
        stable_id = parse_stable_id(stable_id, KIND_VAULT)
        with self._lock:
            self._check_open()
            current = self._conn.execute(
                "SELECT local_path FROM path_maps WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if current is None:
                raise KBError("这个稳定 id 在本机还没有路径映射")
            other = self._conn.execute(
                "SELECT stable_id FROM path_maps WHERE local_path = ?",
                (local_path,),
            ).fetchone()
            if other is not None and other["stable_id"] != stable_id:
                raise KBError("该文件夹已经映射到另一个稳定 id")
            self._conn.execute(
                """
                UPDATE path_maps SET local_path = ?, updated_at = ?
                WHERE stable_id = ?
                """,
                (local_path, _now(), stable_id),
            )
            self._conn.commit()

    def path_for(self, stable_id: str) -> str | None:
        stable_id = parse_stable_id(stable_id, KIND_VAULT)
        with self._lock:
            self._check_open()
            row = self._conn.execute(
                "SELECT local_path FROM path_maps WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["local_path"])

    def roots(self) -> list[tuple[str, str]]:
        with self._lock:
            self._check_open()
            rows = self._conn.execute(
                "SELECT stable_id, local_path FROM path_maps ORDER BY stable_id"
            ).fetchall()
        return [(str(row["stable_id"]), str(row["local_path"])) for row in rows]

    def mapped_ids(self) -> list[str]:
        return [stable_id for stable_id, _path in self.roots()]

    def upsert_record(self, vault_id: str, relative: str, local_doc_id: str) -> str:
        vault_id = parse_stable_id(vault_id, KIND_VAULT)
        local_doc_id = _check_local_doc_id(local_doc_id)
        with self._lock:
            self._check_open()
            row = self._conn.execute(
                """
                SELECT stable_id FROM records
                WHERE vault_id = ? AND relative_path = ?
                """,
                (vault_id, relative),
            ).fetchone()
            if row is not None:
                if local_doc_id:
                    self._conn.execute(
                        "UPDATE records SET local_doc_id = ? WHERE stable_id = ?",
                        (local_doc_id, row["stable_id"]),
                    )
                    self._conn.commit()
                return str(row["stable_id"])
            stable_id = mint_stable_id(KIND_RECORD)
            self._conn.execute(
                """
                INSERT INTO records(
                  stable_id, vault_id, relative_path, local_doc_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (stable_id, vault_id, relative, local_doc_id, _now()),
            )
            self._conn.commit()
            return stable_id

    def relocate_record(self, stable_id: str, relative: str) -> None:
        stable_id = parse_stable_id(stable_id, KIND_RECORD)
        with self._lock:
            self._check_open()
            row = self._conn.execute(
                "SELECT vault_id FROM records WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("没有这条稳定 id")
            taken = self._conn.execute(
                """
                SELECT stable_id FROM records
                WHERE vault_id = ? AND relative_path = ? AND stable_id != ?
                """,
                (row["vault_id"], relative, stable_id),
            ).fetchone()
            if taken is not None:
                raise KBError("该相对路径已经有另一条稳定 id")
            self._conn.execute(
                "UPDATE records SET relative_path = ? WHERE stable_id = ?",
                (relative, stable_id),
            )
            self._conn.commit()

    def get_record(self, stable_id: str) -> dict[str, str] | None:
        stable_id = parse_stable_id(stable_id, KIND_RECORD)
        with self._lock:
            self._check_open()
            row = self._conn.execute(
                """
                SELECT stable_id, vault_id, relative_path, local_doc_id
                FROM records WHERE stable_id = ?
                """,
                (stable_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "stable_id": str(row["stable_id"]),
            "vault_id": str(row["vault_id"]),
            "relative_path": str(row["relative_path"]),
            "local_doc_id": str(row["local_doc_id"]),
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def _check_open(self) -> None:
        if self._closed:
            raise KBError("同步预留库已关闭")
