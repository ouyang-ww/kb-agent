"""重排器：Cross-Encoder 对 top-N 候选重排取 top-k；失败降级原排序。"""

from __future__ import annotations

from .._net import prepare_hf_env
from ..models import RetrievalHit


class Reranker:
    def __init__(
        self, model_name: str, enabled: bool = True, ignore_proxy: bool = True
    ) -> None:
        self.model_name = model_name
        self.enabled = enabled
        self.ignore_proxy = ignore_proxy
        self._model = None

    def _lazy(self):
        if self._model is None:
            if self.ignore_proxy:
                prepare_hf_env(self.model_name)  # 剥离代理 + 缓存命中强制离线
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name)
        return self._model

    def rerank(
        self, query: str, hits: list[RetrievalHit], top_k: int | None = None
    ) -> list[RetrievalHit]:
        if not self.enabled or not hits:
            return hits[:top_k]
        top_k = top_k if top_k is not None else len(hits)
        try:
            model = self._lazy()
            scores = model.predict([(query, h.text) for h in hits]).tolist()
            ranked = sorted(zip(hits, scores), key=lambda x: -x[1])
            out: list[RetrievalHit] = []
            for h, s in ranked:
                h.score = float(s)
                out.append(h)
            return out[:top_k]
        except Exception:
            # 模型加载/推理失败：降级为原始相似度排序
            return hits[:top_k]
