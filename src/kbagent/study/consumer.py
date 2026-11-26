"""概念消费者。只在学习模块开启概念抽取时由门面挂上。

游标和向量库不在同一个事务里。处理必须幂等。
"""

from __future__ import annotations

from .concepts import ConceptIndex

_EXTRACT_EVENTS = frozenset({"ingest", "update", "unhide"})


class MentionsConsumer:
    """概念、关系和 mentions。hide 不删行，展示时再按可见性过滤。"""

    name = "mentions"

    def __init__(self, index: ConceptIndex) -> None:
        self.index = index

    def handle(self, event: dict) -> None:
        kind = str(event.get("event") or "")
        doc_id = str(event.get("doc_id") or "")
        if not doc_id:
            return
        if kind == "delete":
            self.index.forget_doc(doc_id)
            return
        if kind in _EXTRACT_EVENTS:
            self.index.sync_doc(doc_id)


class ConceptPagesConsumer:
    """按当前可见概念重写派生页。源目录不动。"""

    name = "concept_pages"

    def __init__(self, index: ConceptIndex) -> None:
        self.index = index

    def handle(self, event: dict) -> None:
        if not event.get("doc_id") and not event.get("event"):
            return
        self.index.rebuild_pages()
