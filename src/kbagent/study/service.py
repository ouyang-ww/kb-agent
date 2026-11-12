"""学习切片的门面：today → answer → 下次到期。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .store import StudyStore, resolve_day


class StudyService:
    def __init__(self, db_path: str | Path) -> None:
        self.store = StudyStore(db_path)

    def today(self, on: date | None = None) -> list[dict]:
        return self.store.due(resolve_day(on))

    def answer(
        self,
        item_id: str,
        response: str,
        *,
        confidence: int | None = None,
        on: date | None = None,
    ) -> dict:
        return self.store.answer(
            item_id,
            response,
            confidence=confidence,
            on=resolve_day(on),
        )

    def close(self) -> None:
        self.store.close()
