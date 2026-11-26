"""学习模块。和核心检索分开。

硬编码复习仍然是 today → answer → 下次到期。
概念、出处、消歧和概念页只在 study.enabled 与 study.concepts 都打开时，
异步消费 doc_events。关闭不删已有派生数据，也不回放历史。
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
