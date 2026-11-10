"""doc_events 的消费者。

P1 只接已经存在的向量索引。mentions / items / reports 还没有对应模块，
这里留下名字和游标位，不提前做学习系统。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from ..storage.filters import eq
from .store import GovernanceStore
from .vectors import sync_chunk_governance

logger = logging.getLogger(__name__)

VECTOR_CONSUMER = "vector_index"

# 设计文档里的下游。模块还没落地，游标保持 0，方便以后从事件头重放。
RESIDUAL_CONSUMERS = (
    "mentions",
    "items",
    "reports",
    "concept_pages",
    "queue",
    "cache",
)

_HIDE_EVENTS = frozenset({"hide", "pending_cleanup"})
_UNHIDE_EVENTS = frozenset({"unhide"})
_DELETE_EVENTS = frozenset({"delete"})


class VectorIndexConsumer:
    """把 hide / unhide / delete 反映到切块元数据。幂等，可重放。"""

    name = VECTOR_CONSUMER

    def __init__(self, vector_store, doc_store, settings: Settings) -> None:
        self.vector_store = vector_store
        self.doc_store = doc_store
        self.settings = settings

    def handle(self, event: dict[str, Any]) -> None:
        kind = str(event.get("event") or "")
        doc_id = str(event.get("doc_id") or "")
        if not doc_id or kind in {"ingest", "update"}:
            # 入库当时已经写过切块元数据，这里只前进游标。
            return
        collection = self._collection(event, doc_id)
        if kind in _HIDE_EVENTS:
            sync_chunk_governance(
                self.vector_store, collection, doc_id, status="hidden"
            )
            return
        if kind in _UNHIDE_EVENTS:
            sync_chunk_governance(
                self.vector_store, collection, doc_id, status="active"
            )
            return
        if kind in _DELETE_EVENTS:
            self.vector_store.delete(collection, where=eq("doc_id", doc_id))
            return
        logger.info("vector_index 忽略未知事件 %s", kind)

    def _collection(self, event: dict[str, Any], doc_id: str) -> str:
        payload = event.get("payload") or {}
        named = payload.get("collection") if isinstance(payload, dict) else None
        if isinstance(named, str) and named.strip():
            return named
        try:
            doc = self.doc_store.get(doc_id)
        except Exception:
            doc = None
        if doc is not None and doc.collection:
            return doc.collection
        return self.settings.default_collection


def drain_consumers(
    governance: GovernanceStore,
    consumers: list[Any],
    *,
    limit: int = 500,
) -> dict[str, Any]:
    """按各自游标消化事件。失败的事件进 dead letter 后跳过，避免一条毒事件堵住后面的 hide。

    向量库和 SQLite 不在同一个事务里。处理函数必须幂等，游标只在尝试之后前移。
    """
    report: dict[str, Any] = {}
    for consumer in consumers:
        name = str(consumer.name)
        processed = 0
        dead = 0
        errors: list[dict[str, Any]] = []
        cursor = governance.get_cursor(name)
        while True:
            events = governance.list_events(after_id=cursor, limit=limit)
            if not events:
                break
            for event in events:
                event_id = int(event["id"])
                try:
                    consumer.handle(event)
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    governance.record_dead_letter(name, event_id, message)
                    errors.append({"event_id": event_id, "error": message})
                    dead += 1
                    logger.warning(
                        "consumer %s 处理事件 %s 失败，已记入 dead letter",
                        name,
                        event_id,
                        exc_info=True,
                    )
                governance.set_cursor(name, event_id)
                cursor = event_id
                processed += 1
            if len(events) < limit:
                break
        report[name] = {
            "processed": processed,
            "dead_letter": dead,
            "errors": errors,
            "cursor": cursor,
        }
    report["residual"] = [
        {
            "consumer": name,
            "implemented": False,
            "cursor": governance.get_cursor(name),
        }
        for name in RESIDUAL_CONSUMERS
    ]
    return report
