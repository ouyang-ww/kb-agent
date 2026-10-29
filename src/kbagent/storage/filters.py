"""后端中立的元数据过滤模型。

调用方可以构造 :class:`Filter` / :class:`Condition`；也接受旧版 Chroma
dict 语法（如 ``{"tag": "food"}``、``{"$and": [...]}``），内部统一转换为
``Filter``，再由各适配器编译成自己的查询语法。

这样业务层与 REST API 不再把某一种向量库的过滤语法当作公开契约。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ..errors import KBError

Scalar = str | int | float | bool
Op = Literal["eq", "ne", "in", "nin", "gt", "gte", "lt", "lte"]

_OP_VALUES: frozenset[str] = frozenset(
    {"eq", "ne", "in", "nin", "gt", "gte", "lt", "lte"}
)
_LEGACY_OP_MAP = {
    "$eq": "eq",
    "$ne": "ne",
    "$in": "in",
    "$nin": "nin",
    "$gt": "gt",
    "$gte": "gte",
    "$lt": "lt",
    "$lte": "lte",
}
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_OPS = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


@dataclass(frozen=True)
class Condition:
    """单个字段条件。``value`` 在 in / nin 时必须是列表。"""

    field: str
    op: Op
    value: Scalar | list[Scalar]

    def __post_init__(self) -> None:
        if self.op not in _OP_VALUES:
            raise KBError(f"不支持的过滤操作: {self.op}")
        if not _FIELD_RE.match(self.field):
            raise KBError(f"非法的过滤字段: {self.field!r}")
        if self.op in ("in", "nin"):
            if not isinstance(self.value, list):
                raise KBError(f"{self.op} 的值必须是列表")
            if not all(isinstance(v, (str, int, float, bool)) for v in self.value):
                raise KBError(f"{self.op} 的值必须是标量列表")
        elif isinstance(self.value, (list, dict)):
            raise KBError(f"{self.op} 的值必须是标量")


@dataclass(frozen=True)
class Filter:
    """条件为 AND；``any_of`` 中的每个子 Filter 之间为 OR。"""

    conditions: tuple[Condition, ...] = ()
    any_of: tuple["Filter", ...] = ()

    def __and__(self, other: "Filter") -> "Filter":
        return Filter(
            conditions=self.conditions + other.conditions,
            any_of=self.any_of + other.any_of,
        )

    def __or__(self, other: "Filter") -> "Filter":
        return Filter(any_of=(self, other))

    def __bool__(self) -> bool:
        return bool(self.conditions or self.any_of)


# 旧版 API 兼容：Store 方法接受 Filter / dict / None
Where = Filter | dict[str, Any] | None


def eq(field: str, value: Scalar) -> Filter:
    return Filter((Condition(field, "eq", value),))


def ne(field: str, value: Scalar) -> Filter:
    return Filter((Condition(field, "ne", value),))


def in_(field: str, values: list[Scalar]) -> Filter:
    return Filter((Condition(field, "in", values),))


def nin(field: str, values: list[Scalar]) -> Filter:
    return Filter((Condition(field, "nin", values),))


def gt(field: str, value: Scalar) -> Filter:
    return Filter((Condition(field, "gt", value),))


def gte(field: str, value: Scalar) -> Filter:
    return Filter((Condition(field, "gte", value),))


def lt(field: str, value: Scalar) -> Filter:
    return Filter((Condition(field, "lt", value),))


def lte(field: str, value: Scalar) -> Filter:
    return Filter((Condition(field, "lte", value),))


def all_of(*filters: Filter) -> Filter:
    """多个 Filter 取 AND。"""
    conditions: tuple[Condition, ...] = ()
    groups: tuple[Filter, ...] = ()
    for f in filters:
        conditions += f.conditions
        groups += f.any_of
    return Filter(conditions, groups)


def any_of(*filters: Filter) -> Filter:
    """多个 Filter 取 OR。"""
    return Filter(any_of=tuple(filters))


def normalize_filter(where: Where) -> Filter | None:
    """把 Filter / 旧版 Chroma dict / None 统一成 Filter。"""
    if where is None:
        return None
    if isinstance(where, Filter):
        return where or None
    if isinstance(where, dict):
        return _from_legacy(where) or None
    raise KBError(f"不支持的过滤条件类型: {type(where).__name__}")


def _from_legacy(where: dict[str, Any]) -> Filter:
    conditions: list[Condition] = []
    groups: list[Filter] = []
    for key, value in where.items():
        if key == "$and":
            if not isinstance(value, list):
                raise KBError("$and 的值必须是列表")
            for item in value:
                if not isinstance(item, dict):
                    raise KBError("$and 的每一项必须是 dict")
                sub = _from_legacy(item)
                conditions.extend(sub.conditions)
                groups.extend(sub.any_of)
        elif key == "$or":
            if not isinstance(value, list):
                raise KBError("$or 的值必须是列表")
            subs = []
            for item in value:
                if not isinstance(item, dict):
                    raise KBError("$or 的每一项必须是 dict")
                subs.append(_from_legacy(item))
            groups.append(Filter(any_of=tuple(subs)))
        elif key.startswith("$"):
            raise KBError(f"不支持的过滤操作: {key}")
        elif isinstance(value, dict):
            for op, operand in value.items():
                if op not in _LEGACY_OP_MAP:
                    raise KBError(f"不支持的过滤操作: {op}")
                conditions.append(Condition(key, _LEGACY_OP_MAP[op], operand))
        elif isinstance(value, list):
            raise KBError(f"字段 {key!r} 的值不能是列表，请使用 $in")
        else:
            conditions.append(Condition(key, "eq", value))
    return Filter(tuple(conditions), tuple(groups))


def compile_chroma(where: Where) -> dict[str, Any] | None:
    """编译为 Chroma ``where`` 语法（适配器内部使用）。"""
    f = normalize_filter(where)
    if not f:
        return None
    return _compile_chroma_filter(f)


def _compile_chroma_filter(f: Filter) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [
        _compile_chroma_condition(c) for c in f.conditions
    ]
    if f.any_of:
        parts.append({"$or": [_compile_chroma_filter(g) for g in f.any_of]})
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    return {"$and": parts}


def _compile_chroma_condition(c: Condition) -> dict[str, Any]:
    if c.op == "eq":
        return {c.field: c.value}
    operand = list(c.value) if c.op in ("in", "nin") else c.value
    return {c.field: {f"${c.op}": operand}}


def compile_sql(
    where: Where, allowed_fields: set[str] | frozenset[str]
) -> tuple[str, list[Any]]:
    """编译为参数化 SQL WHERE 片段（适配器内部使用）。

    返回 ``("1=1", [])`` 表示无条件。字段必须在 ``allowed_fields`` 中，
    避免把用户输入拼进 SQL 标识符。
    """
    f = normalize_filter(where)
    if not f:
        return "1=1", []
    return _compile_sql_filter(f, allowed_fields)


def _compile_sql_filter(
    f: Filter, allowed_fields: set[str] | frozenset[str]
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for c in f.conditions:
        if c.field not in allowed_fields or not _FIELD_RE.match(c.field):
            raise KBError(f"不支持的过滤字段: {c.field!r}")
        if c.op in ("in", "nin"):
            values = list(c.value)
            if not values:
                clauses.append("0=1" if c.op == "in" else "1=1")
                continue
            placeholders = ",".join("?" for _ in values)
            keyword = "IN" if c.op == "in" else "NOT IN"
            clauses.append(f"{c.field} {keyword} ({placeholders})")
            params.extend(values)
        else:
            clauses.append(f"{c.field} {_SQL_OPS[c.op]} ?")
            params.append(c.value)
    if f.any_of:
        or_parts: list[str] = []
        for g in f.any_of:
            sub, sub_params = _compile_sql_filter(g, allowed_fields)
            or_parts.append(f"({sub})")
            params.extend(sub_params)
        clauses.append("(" + " OR ".join(or_parts) + ")")
    return (" AND ".join(clauses) or "1=1"), params
