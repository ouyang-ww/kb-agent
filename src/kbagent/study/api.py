"""挂在 KnowledgeBase 上的薄入口。开关检查在门面里做。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from .service import StudyService


class StudyAPI:
    def __init__(self, opener: Callable[[], StudyService]) -> None:
        self._open = opener

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
