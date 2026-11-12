"""学习模块。和核心检索分开。

这一刀只做硬编码的 3 道题：今天到期、作答、写下一次到期。
不抽取概念，不生成正式题库，也不消费治理事件。
"""

from .cards import HARDCODED_CARDS, PROMPT_VERSION
from .scheduler import ReviewState, schedule
from .service import StudyService

__all__ = [
    "HARDCODED_CARDS",
    "PROMPT_VERSION",
    "ReviewState",
    "StudyService",
    "schedule",
]
