"""父文档存储协议。

实现可以是 SQLite / PostgreSQL / 其他；业务层只依赖本 Protocol。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..models import Document
from ..storage.filters import Where


@runtime_checkable
class DocumentStore(Protocol):
    """父文档库最小契约。绑定原件不要求保存整篇正文。"""

    def upsert(self, doc: Document) -> None: ...

    def get(self, doc_id: str) -> Document | None: ...

    def get_by_hash(self, content_hash: str) -> Document | None: ...

    def list(
        self,
        collection: str | None = None,
        where: Where = None,
    ) -> list[Document]: ...

    def delete(self, doc_id: str) -> bool: ...

    def stats(self) -> dict[str, Any]: ...

    def close(self) -> None: ...
