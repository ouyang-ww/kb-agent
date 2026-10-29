"""存储抽象：过滤模型、适配器工厂与协议。"""

from .filters import (
    Condition,
    Filter,
    Where,
    all_of,
    any_of,
    eq,
    gt,
    gte,
    in_,
    lt,
    lte,
    ne,
    nin,
)

__all__ = [
    "Condition",
    "Filter",
    "Where",
    "all_of",
    "any_of",
    "eq",
    "gt",
    "gte",
    "in_",
    "lt",
    "lte",
    "make_document_store",
    "make_vector_store",
    "ne",
    "nin",
]


def __getattr__(name: str):
    """惰性导出工厂函数，避免 storage ↔ document_store 的循环导入。"""
    if name in {"make_vector_store", "make_document_store"}:
        from . import factory

        return getattr(factory, name)
    raise AttributeError(name)
