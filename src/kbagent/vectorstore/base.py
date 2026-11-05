"""向量存储协议与统一结果模型。

业务层只依赖这里的 Protocol / dataclass，不依赖 Chroma 的具体类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..errors import KBError
from ..storage.filters import Where

_METRICS = frozenset({"l2", "cosine", "ip"})


@dataclass
class StoredVector:
    """向量库中的一条记录（不参与相似度查询）。"""

    id: str
    document: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # 仅在 get(..., include_embeddings=True) 时填充，供替换失败后回滚。
    embedding: list[float] | None = None


@dataclass
class VectorHit(StoredVector):
    """相似度查询命中：带原始距离、归一化分数与度量方式。"""

    distance: float = 0.0
    score: float = 0.0
    metric: str = "l2"


def distance_to_score(distance: float, metric: str = "l2") -> float:
    """把不同后端的距离换算成 0–1 的相关度分数（越大越相关）。

    - l2：distance 为非负欧氏距离，使用 1/(1+d)
    - cosine / ip：Chroma 等后端返回 1 - similarity，使用 1-d 并裁剪到 [0,1]
    """
    if metric not in _METRICS:
        raise KBError(f"不支持的向量度量: {metric}")
    d = max(0.0, float(distance))
    if metric == "l2":
        return 1.0 / (1.0 + d)
    return max(0.0, min(1.0, 1.0 - d))


@runtime_checkable
class VectorStore(Protocol):
    """向量库最小契约。新增后端时实现同名方法即可替换。"""

    def add(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None: ...

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        top_k: int,
        where: Where = None,
    ) -> list[VectorHit]: ...

    def delete(
        self,
        collection: str,
        ids: list[str] | None = None,
        where: Where = None,
    ) -> None: ...

    def get(
        self,
        collection: str,
        ids: list[str] | None = None,
        where: Where = None,
        limit: int | None = None,
        *,
        include_embeddings: bool = False,
    ) -> list[StoredVector]: ...

    def count(self, collection: str) -> int: ...

    def collections(self) -> list[str]: ...

    def close(self) -> None: ...
