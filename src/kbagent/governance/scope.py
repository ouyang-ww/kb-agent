"""可见性规则：scope / status 的统一定义。

文档行缺元数据时仍视为可见，兼容还没 backfill 的旧库。
向量检索反过来：scope 必须落在允许列表里。Chroma 的 ``$nin`` 会把缺字段
也算进去，所以默认 where 用 ``scope $in``，空 scope 和缺 scope 都不返回。
"""

from __future__ import annotations

from typing import Protocol

HIDDEN_SCOPES = frozenset({"private", "secret"})
# 设计里的可见范围。调用方自己的 default_scope 会再并进来，空字符串不会。
VISIBLE_SCOPES = ("default", "work")
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


def visibility_scopes(default_scope: str | None = None) -> list[str]:
    """默认检索允许的 scope。不含空字符串，也不含 private/secret。"""
    scopes: list[str] = []
    for raw in (*VISIBLE_SCOPES, default_scope or ""):
        text = str(raw).strip()
        if not text or text in HIDDEN_SCOPES or text in scopes:
            continue
        scopes.append(text)
    return scopes


def visibility_filter(default_scope: str | None = None):
    """默认检索的向量 where：status=active，且 scope 落在允许列表里。

    用 ``$in`` 而不是 ``$nin``。缺 scope、空 scope 都匹配不上，不会被搜出来。
    缺了这道条件就不能查。检索器会自己补上；补不上就拒绝执行。
    """
    from ..storage.filters import Condition, Filter

    return Filter(
        conditions=(
            Condition("status", "eq", ACTIVE_STATUS),
            Condition("scope", "in", visibility_scopes(default_scope)),
        )
    )


def _and_conditions(filt) -> list:
    """只收集 AND 链上的条件。OR 分支里的条件不算「已经强制过滤」。"""
    found = list(filt.conditions)
    for sub in getattr(filt, "conjuncts", ()) or ():
        found.extend(_and_conditions(sub))
    return found


def _scope_allowlist_closed(values) -> bool:
    """正向允许列表：非空、没有空字符串、没有 private/secret。"""
    if not isinstance(values, list) or not values:
        return False
    labels = {str(v).strip() for v in values}
    if "" in labels or not labels.isdisjoint(HIDDEN_SCOPES):
        return False
    return "default" in labels or bool(labels)


def visibility_is_enforced(where) -> bool:
    """合并后的过滤是否同时要求 status=active 和一份关闭的 scope 允许列表。"""
    from ..storage.filters import Filter, normalize_filter

    filt = where if isinstance(where, Filter) else normalize_filter(where)
    if not filt:
        return False
    conds = _and_conditions(filt)
    has_status = any(
        c.field == "status" and c.op == "eq" and c.value == ACTIVE_STATUS
        for c in conds
    )
    has_scope = any(
        c.field == "scope" and c.op == "in" and _scope_allowlist_closed(c.value)
        for c in conds
    )
    return has_status and has_scope
