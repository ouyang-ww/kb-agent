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


def visibility_filter():
    """默认检索的向量 where：只要 active，且 scope 不是 private/secret。

    缺了这道条件就不能查。调用方忘了传过滤器时，检索器会自己补上；
    补不上就拒绝执行，而不是静默搜全库。
    """
    from ..storage.filters import Condition, Filter

    return Filter(
        conditions=(
            Condition("status", "eq", ACTIVE_STATUS),
            Condition("scope", "nin", ["private", "secret"]),
        )
    )


def _and_conditions(filt) -> list:
    """只收集 AND 链上的条件。OR 分支里的条件不算「已经强制过滤」。"""
    found = list(filt.conditions)
    for sub in getattr(filt, "conjuncts", ()) or ():
        found.extend(_and_conditions(sub))
    return found


def visibility_is_enforced(where) -> bool:
    """合并后的过滤是否同时卡住 status=active 和 hidden scope。"""
    from ..storage.filters import Filter, normalize_filter

    filt = where if isinstance(where, Filter) else normalize_filter(where)
    if not filt:
        return False
    conds = _and_conditions(filt)
    hidden = set(HIDDEN_SCOPES)
    has_status = any(
        c.field == "status" and c.op == "eq" and c.value == ACTIVE_STATUS
        for c in conds
    )
    has_scope = any(
        c.field == "scope"
        and c.op == "nin"
        and hidden <= {str(v) for v in c.value}
        for c in conds
    )
    return has_status and has_scope
