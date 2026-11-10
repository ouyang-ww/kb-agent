"""重排器：Cross-Encoder 对 top-N 候选重排取 top-k；失败降级原排序。

``BAAI/bge-reranker-base`` 在 sentence-transformers 里默认 ``activation_fn``
是 Sigmoid，``predict`` 已经给出 0–1 的概率。闸门直接用这个数，不再 sigmoid 一次。
只有激活函数不是 Sigmoid、输出仍是 logit 时，才做一次数值稳定的 sigmoid。
"""

from __future__ import annotations

import logging
import math

from .._net import prepare_hf_env
from ..models import RetrievalHit

logger = logging.getLogger(__name__)


def stable_sigmoid(value: float) -> float:
    """logit → (0, 1)。``math.exp`` 在 ±1000 时不会抛 OverflowError。"""
    x = float(value)
    if x >= 0:
        # x 很大时 exp(-x) 下溢成 0，结果是 1。
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def to_gate_score(raw: float, *, probability: bool) -> float:
    """概率原样返回。logit 只做一次稳定 sigmoid，避免 sigmoid(0) 变成 0.5。"""
    score = float(raw)
    if probability:
        return score
    return stable_sigmoid(score)


def activation_is_sigmoid(model) -> bool:
    fn = getattr(model, "activation_fn", None)
    if fn is None:
        return False
    return type(fn).__name__.lower() == "sigmoid"


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
            raw_scores = model.predict([(query, h.text) for h in hits]).tolist()
            as_probability = activation_is_sigmoid(model)
            ranked = sorted(zip(hits, raw_scores), key=lambda item: -float(item[1]))
            out: list[RetrievalHit] = []
            for hit, raw in ranked:
                score = to_gate_score(float(raw), probability=as_probability)
                hit.score = score
                hit.rerank_score = score
                hit.rerank_error = None
                out.append(hit)
            return out[:top_k]
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("重排失败，改用向量分: %s", message, exc_info=True)
            kept = hits[:top_k]
            for hit in kept:
                hit.rerank_score = None
                hit.rerank_error = message
            return kept
