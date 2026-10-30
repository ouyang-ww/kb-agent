"""可见性规则：scope / status 的统一定义。

默认策略：
- status 必须是 active
- scope 不在 HIDDEN_SCOPES 中
- 缺失元数据视为可见（兼容旧数据）
"""

from __future__ import annotations

from typing import Protocol

HIDDEN_SCOPES = frozenset({"private", "secret"})
ACTIVE_STATUS = "active"


class MetaLike(Protocol):
    scope: str
    status: str


def is_visible(meta: MetaLike | None, include_hidden: bool = False) -> bool:
    if include_hidden or meta is None:
        return True
    return meta.status == ACTIVE_STATUS and meta.scope not in HIDDEN_SCOPES


def is_visible_values(
    scope: str | None,
    status: str | None,
    include_hidden: bool = False,
) -> bool:
    if include_hidden:
        return True
    if status not in (None, ACTIVE_STATUS):
        return False
    return (scope or "default") not in HIDDEN_SCOPES
