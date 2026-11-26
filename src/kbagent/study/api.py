"""挂在 KnowledgeBase 上的薄入口。开关检查在门面里做。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

from ..errors import KBError
from .service import StudyService


class StudyAPI:
    def __init__(
        self,
        opener: Callable[[], StudyService],
        concepts: Callable[[], object] | None = None,
    ) -> None:
        self._open = opener
        self._concepts = concepts

    def today(self, on: date | None = None) -> list[dict]:
        return self._open().today(on=on)

    def answer(
        self,
        item_id: str,
        response: str,
        *,
        confidence: int | None = None,
        on: date | None = None,
    ) -> dict:
        return self._open().answer(
            item_id,
            response,
            confidence=confidence,
            on=on,
        )

    def _index(self):
        if self._concepts is None:
            raise KBError("概念抽取还没有接上")
        return self._concepts()

    def list_concepts(self) -> list[dict]:
        rows = self._index().list_visible()
        return [_public_concept(row) for row in rows]

    def concept_page(self, key: str) -> dict:
        return self._index().page(key)

    def audit_concepts(self) -> dict:
        report = self._index().audit()
        report["suggestions_detail"] = self._index().visible_suggestions()
        return report

    def merge_concepts(self, left: str, right: str, *, dry_run: bool = False) -> dict:
        return self._index().merge(left, right, dry_run=dry_run)

    def import_outline(self, path: str | Path) -> dict:
        return self._index().import_outline(path)

    def outline_coverage(self, domain: str | None = None) -> dict:
        return self._index().coverage(domain)


def _public_concept(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "domain": row["domain"],
        "definition": row["definition"],
        "aliases": list(row.get("aliases") or []),
        "entity_type": row.get("entity_type") or "Concept",
        "mastery": row.get("mastery"),
        "mentions": [
            {
                "doc_id": item["doc_id"],
                "title": item.get("title") or item["doc_id"],
                "chunk_id": item.get("chunk_id") or "",
                "snippet": item.get("snippet") or "",
            }
            for item in row.get("mentions") or []
        ],
    }
