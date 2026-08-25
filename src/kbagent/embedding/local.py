"""嵌入层：本地 CPU 模型（sentence-transformers），懒加载 + bge 查询指令。"""

from __future__ import annotations

from typing import Protocol

from .._net import prepare_hf_env
from ..errors import KBError


class Embedder(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class SentenceTransformerEmbedder:
    """本地 CPU 嵌入模型。模型在首次使用时才加载。"""

    def __init__(
        self,
        model_name: str,
        query_instruction: str = "",
        device: str | None = None,
        batch_size: int = 32,
        ignore_proxy: bool = True,
    ) -> None:
        self.model_name = model_name
        self.query_instruction = query_instruction
        self.device = device
        self.batch_size = batch_size
        self.ignore_proxy = ignore_proxy
        self._model = None

    def _lazy(self):
        if self._model is None:
            if self.ignore_proxy:
                prepare_hf_env(self.model_name)  # 剥离代理 + 缓存命中强制离线
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover
                raise KBError(
                    "缺少 sentence-transformers，请先安装: pip install sentence-transformers"
                ) from exc
            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        model = self._lazy()
        vectors = model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        model = self._lazy()
        if self.query_instruction:
            text = self.query_instruction + text
        v = model.encode([text], normalize_embeddings=True, show_progress_bar=False)
        return v[0].tolist()
