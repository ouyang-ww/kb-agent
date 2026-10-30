"""知识治理：scope、docs_meta、doc_events 与 consumer 游标。"""

from .scope import ACTIVE_STATUS, HIDDEN_SCOPES, is_visible, is_visible_values
from .store import DocMeta, GovernanceStore

__all__ = [
    "ACTIVE_STATUS",
    "HIDDEN_SCOPES",
    "DocMeta",
    "GovernanceStore",
    "is_visible",
    "is_visible_values",
]
