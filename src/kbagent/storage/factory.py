"""按配置创建存储适配器。

业务层通过这里获取 store，而不是直接 import 具体实现。
新增后端时：实现 Protocol → 在工厂注册 → 配置切换。
"""

from __future__ import annotations

from ..config import Settings
from ..document_store.base import DocumentStore
from ..errors import KBError
from ..vectorstore.base import VectorStore


def make_vector_store(settings: Settings) -> VectorStore:
    kind = (settings.vector_store or "chroma").strip().lower()
    if kind == "chroma":
        from ..vectorstore.chroma_store import ChromaStore

        return ChromaStore(settings.chroma_dir, metric=settings.vector_metric)
    raise KBError(f"不支持的向量库后端: {kind!r}（当前支持: chroma）")


def make_document_store(settings: Settings) -> DocumentStore:
    kind = (settings.doc_store or "sqlite").strip().lower()
    if kind == "sqlite":
        from ..document_store.sqlite_store import SQLiteDocumentStore

        return SQLiteDocumentStore(settings.documents_db)
    raise KBError(f"不支持的文档库后端: {kind!r}（当前支持: sqlite）")
