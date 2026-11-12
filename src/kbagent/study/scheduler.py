"""按题目计算下次到期。公式来自 mvp-l1-to-l3 §5.2 的 SM-2 简化版。

对 = 4，部分 = 3，错 = 1。答对不额外加分，所以 ease 在答对时保持不变。
成功的间隔是 1 天、3 天，之后 round(间隔 × 新 ease)。
答错把间隔打回 1 天，ease 减 0.2，重复次数清零，下次成功重新从 1 天走。
这里不算概念掌握度。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

EASE_MIN = 1.3
EASE_MAX = 2.8
DEFAULT_EASE = 2.5

_GRADES = frozenset({1, 3, 4})


@dataclass(frozen=True)
class ReviewState:
    ease: float = DEFAULT_EASE
    interval_d: float = 0
    reps: int = 0
    lapses: int = 0


@dataclass(frozen=True)
class Scheduled:
    ease: float
    interval_d: float
    reps: int
    lapses: int
    next_due: date
    grade: int


def grade_for_outcome(correct: float) -> int:
    """0 / 0.5 / 1 映射到错 / 部分 / 对。"""
    if correct >= 1:
        return 4
    if correct >= 0.5:
        return 3
    return 1


def clamp_ease(value: float) -> float:
    return min(EASE_MAX, max(EASE_MIN, round(float(value), 4)))


def schedule(state: ReviewState, grade: int, today: date) -> Scheduled:
    if grade not in _GRADES:
        raise ValueError(f"grade 只能是 1、3 或 4，收到 {grade}")
    if type(today) is not date:
        raise ValueError("today 必须是 datetime.date")
    if grade < 3:
        ease = clamp_ease(state.ease - 0.2)
        return Scheduled(
            ease=ease,
            interval_d=1,
            reps=0,
            lapses=state.lapses + 1,
            next_due=today + timedelta(days=1),
            grade=grade,
        )
    gap = 5 - grade
    ease = clamp_ease(state.ease + (0.1 - gap * (0.08 + gap * 0.02)))
    reps = state.reps + 1
    if reps == 1:
        interval = 1
    elif reps == 2:
        interval = 3
    else:
        interval = max(1, int(round(state.interval_d * ease)))
    return Scheduled(
        ease=ease,
        interval_d=float(interval),
        reps=reps,
        lapses=state.lapses,
        next_due=today + timedelta(days=interval),
        grade=grade,
    )
