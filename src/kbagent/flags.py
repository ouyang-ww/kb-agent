"""运行时功能开关：SQLite 持久化 + 进程内缓存，支持热切换。

启动配置（.env / 环境变量）只提供默认值；运行时应通过本服务读写，
下一次请求 / 调度即可生效，无需重启进程。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .errors import KBError

# 已注册的开关（名称 -> 说明）。新增扩展时必须先在这里注册。
KNOWN_FLAGS: dict[str, str] = {
    "study.enabled": "学习扩展总开关",
    "study.concepts": "概念抽取与概念页",
    "study.items": "出题",
    "study.queue": "每日队列",
    "study.weekly": "周报",
    "study.dashboard": "概览页",
    "study.notifications": "主动提醒",
}

DEFAULT_FLAGS: dict[str, bool] = {
    "study.enabled": False,
    "study.concepts": True,
    "study.items": True,
    "study.queue": True,
    "study.weekly": True,
    "study.dashboard": True,
    "study.notifications": False,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_flags (
  name       TEXT PRIMARY KEY,
  enabled    INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FeatureFlags:
    """运行时开关存储。线程安全；写入后立即更新内存缓存。"""

    def __init__(
        self,
        db_path: str | Path,
        defaults: dict[str, bool] | None = None,
    ) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._defaults = dict(DEFAULT_FLAGS)
        if defaults:
            self._defaults.update(defaults)
        self._cache: dict[str, bool] = dict(self._defaults)
        self._version = 0
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self.refresh()

    def _validate(self, name: str) -> None:
        if name not in KNOWN_FLAGS:
            raise KBError(f"未知的运行时开关: {name!r}")

    def refresh(self) -> None:
        """从 SQLite 重新加载（多进程/外部修改时使用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, enabled FROM runtime_flags"
            ).fetchall()
            cache = dict(self._defaults)
            for row in rows:
                if row["name"] in KNOWN_FLAGS:
                    cache[row["name"]] = bool(row["enabled"])
            self._cache = cache
            self._version += 1

    def get(self, name: str) -> bool:
        self._validate(name)
        with self._lock:
            return bool(self._cache.get(name, self._defaults[name]))

    def is_enabled(self, name: str) -> bool:
        return self.get(name)

    def set(self, name: str, enabled: bool, actor: str = "local") -> bool:
        """写入开关并立即生效；返回新值。"""
        self._validate(name)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runtime_flags(name, enabled, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  enabled=excluded.enabled,
                  updated_at=excluded.updated_at,
                  updated_by=excluded.updated_by
                """,
                (name, int(bool(enabled)), _now(), actor),
            )
            self._conn.commit()
            self._cache[name] = bool(enabled)
            self._version += 1
            return bool(enabled)

    def all(self) -> dict[str, bool]:
        with self._lock:
            return {name: bool(self._cache.get(name, self._defaults[name]))
                    for name in KNOWN_FLAGS}

    def describe(self) -> dict[str, dict]:
        with self._lock:
            return {
                name: {
                    "enabled": bool(self._cache.get(name, self._defaults[name])),
                    "description": desc,
                }
                for name, desc in KNOWN_FLAGS.items()
            }

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def close(self) -> None:
        with self._lock:
            self._conn.close()
