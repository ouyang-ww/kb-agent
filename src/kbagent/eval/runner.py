"""评估执行器：retrieval/full 模式 + 参数对比实验。

Recall / Accuracy / Faithfulness 仍是检索与回答的质量护栏。
ADR 0002 将父文档扩写改为回读源文件，这是有意的存储变更，不单独记为回归失败。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Settings
from ..errors import LLMError
from ..generation.answerability import is_refusal_text
from .dataset import EvalItem
from .judge import LLMJudge
from .metrics import aggregate, refusal_recall

# 复合分权重（可配）
DEFAULT_WEIGHTS = {
    "recall": 0.3,
    "accuracy": 0.3,
    "faithfulness": 0.2,
    "relevance": 0.2,
}


@dataclass
class EvalResult:
    mode: str
    config: dict[str, Any]
    metrics: dict[str, float] = field(default_factory=dict)
    per_item: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Evaluator:
    def __init__(
        self,
        kb,
        settings: Settings,
        judge: LLMJudge | None = None,
        weights: dict | None = None,
    ) -> None:
        self.kb = kb
        self.settings = settings
        self.judge = judge or LLMJudge(kb._llm, settings)
        self.weights = weights or DEFAULT_WEIGHTS

    # ---- 入口 ----
    def run(
        self,
        items: list[EvalItem],
        mode: str = "full",
        top_k: int | None = None,
        rerank: bool | None = None,
        rewrite: bool | None = None,
        collection: str | None = None,
    ) -> EvalResult:
        top_k = top_k or self.settings.top_k
        config = {
            "mode": mode,
            "top_k": top_k,
            "rerank": self.settings.rerank_enabled if rerank is None else rerank,
            "rewrite": self.settings.query_rewrite_enabled if rewrite is None else rewrite,
            "intent": self.settings.intent_enabled,
            "collection": collection or self.settings.default_collection,
        }
        result = EvalResult(mode=mode, config=config)
        per_item: list[dict] = []

        if mode == "refusal":
            for item in items:
                per_item.append(self._refusal_row(item, top_k, rerank, rewrite, collection))
            result.per_item = per_item
            result.metrics = self._aggregate(per_item, top_k, mode)
            result.warnings = self._redlines(result.metrics, mode)
            return result

        for item in items:
            row: dict[str, Any] = {
                "query": item.query,
                "category": item.category,
                "relevant_doc_ids": item.relevant_doc_ids,
            }
            t0 = time.perf_counter()
            hits = self.kb.search(
                item.query,
                top_k=top_k,
                rerank=rerank,
                rewrite=rewrite,
                collection=collection,
            )
            row["retrieval_latency_ms"] = round(
                (time.perf_counter() - t0) * 1000, 1
            )
            row["retrieved_doc_ids"] = list(
                dict.fromkeys(h.doc_id for h in hits)
            )  # 文档级去重（同一 doc 多个 chunk 只计一次）
            row["image_hit"] = any(h.media is not None for h in hits)

            if mode == "full":
                t0 = time.perf_counter()
                ans = self.kb.ask(
                    item.query,
                    top_k=top_k,
                    rerank=rerank,
                    rewrite=rewrite,
                    collection=collection,
                )
                row["answer_latency_ms"] = round(
                    (time.perf_counter() - t0) * 1000, 1
                )
                row["answer"] = ans.text
                row["intent"] = ans.meta.get("intent")
                row["sources"] = [
                    {"doc_id": s.doc_id, "title": s.title, "score": s.score}
                    for s in ans.sources
                ]
                context = "\n\n".join(
                    f"[{i + 1}] {s.title}\n{s.snippet}" for i, s in enumerate(ans.sources)
                )
                label = self.judge.correctness(item.query, ans.text, item.expected_answer)
                row["correctness"] = label
                row["accuracy"] = 1.0 if label == "correct" else (0.5 if label == "partial" else 0.0)
                if label == "unknown":
                    row["accuracy"] = -1.0
                row["faithfulness"] = self.judge.faithfulness(ans.text, context)
                row["relevance"] = self.judge.relevance(item.query, ans.text)
                row["citation_accuracy"] = self.judge.citation_accuracy(ans.text, context)
            per_item.append(row)

        result.per_item = per_item
        result.metrics = self._aggregate(per_item, top_k, mode)
        result.warnings = self._redlines(result.metrics, mode)
        return result

    def _refusal_row(
        self,
        item: EvalItem,
        top_k: int,
        rerank: bool | None,
        rewrite: bool | None,
        collection: str | None,
    ) -> dict[str, Any]:
        """库外集只看拒答，不调用裁判。没有 LLM key 时，低于 τ 的拒答仍然计入。"""
        row: dict[str, Any] = {
            "query": item.query,
            "category": item.category,
            "should_refuse": True if item.should_refuse or item.category == "out_of_kb" else False,
            "relevant_doc_ids": item.relevant_doc_ids,
        }
        t0 = time.perf_counter()
        ans = self.kb.ask(
            item.query,
            top_k=top_k,
            rerank=rerank,
            rewrite=rewrite,
            collection=collection,
        )
        row["answer_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        row["answer"] = ans.text
        row["refusal_reason"] = ans.meta.get("refusal_reason")
        row["confidence"] = ans.meta.get("confidence")
        row["score_kind"] = ans.meta.get("score_kind")
        row["top1_score"] = ans.meta.get("top1_score")
        row["tau_calibrated"] = ans.meta.get("tau_calibrated")
        row["rerank_fallback"] = bool(ans.meta.get("rerank_fallback"))
        row["refused"] = bool(ans.meta.get("refusal_reason")) or is_refusal_text(ans.text)
        return row

    # ---- 对比实验 ----
    def compare(
        self,
        items: list[EvalItem],
        mode: str = "full",
        param_sets: list[dict[str, Any]] | None = None,
        collection: str | None = None,
    ) -> list[EvalResult]:
        """param_sets: 例如 [{"top_k": 3}, {"top_k": 5, "rerank": False}]。"""
        param_sets = param_sets or [{}]
        results: list[EvalResult] = []
        for params in param_sets:
            results.append(
                self.run(
                    items,
                    mode=mode,
                    top_k=params.get("top_k"),
                    rerank=params.get("rerank"),
                    rewrite=params.get("rewrite"),
                    collection=collection,
                )
            )
        return results

    # ---- 聚合 ----
    def _aggregate(self, per_item: list[dict], top_k: int, mode: str) -> dict[str, float]:
        if mode == "refusal":
            eligible = [row for row in per_item if row.get("should_refuse")]
            if not eligible:
                eligible = per_item
            flags = [bool(row.get("refused")) for row in eligible]
            metrics = {
                "refusal_recall": round(refusal_recall(flags), 4),
                "refused": float(sum(flags)),
                "total": float(len(eligible)),
            }
            lat = [row.get("answer_latency_ms", 0) for row in per_item]
            if lat:
                lat_sorted = sorted(lat)
                metrics["answer_latency_p50_ms"] = round(
                    lat_sorted[len(lat_sorted) // 2], 1
                )
            return metrics
        ret_metrics = aggregate(per_item, top_k)
        metrics: dict[str, float] = {**ret_metrics}
        if mode == "full":
            rows = [r for r in per_item if r.get("accuracy", -1) >= 0]
            if rows:
                metrics["accuracy"] = round(
                    sum(r["accuracy"] for r in rows) / len(rows), 4
                )
            else:
                metrics["accuracy"] = -1.0
            f_rows = [r for r in per_item if r.get("faithfulness", -1) >= 0]
            metrics["faithfulness"] = (
                round(sum(r["faithfulness"] for r in f_rows) / len(f_rows), 4)
                if f_rows else -1.0
            )
            rel_rows = [r for r in per_item if r.get("relevance", 0) > 0]
            metrics["relevance"] = (
                round(sum(r["relevance"] for r in rel_rows) / len(rel_rows), 4)
                if rel_rows else -1.0
            )
            cit_rows = [r for r in per_item if r.get("citation_accuracy", -1) >= 0]
            metrics["citation_accuracy"] = (
                round(sum(r["citation_accuracy"] for r in cit_rows) / len(cit_rows), 4)
                if cit_rows else -1.0
            )
            # 复合分
            parts = []
            weights_sum = 0.0
            if metrics.get(f"recall@{top_k}", 0) is not None:
                parts.append(metrics[f"recall@{top_k}"] * self.weights["recall"])
                weights_sum += self.weights["recall"]
            for key, w in (("accuracy", self.weights["accuracy"]),
                           ("faithfulness", self.weights["faithfulness"]),
                           ("relevance", self.weights["relevance"])):
                if metrics.get(key, -1) >= 0:
                    parts.append(metrics[key] * w)
                    weights_sum += w
            metrics["composite"] = (
                round(sum(parts) / weights_sum, 4) if weights_sum > 0 else -1.0
            )
        # 延迟
        lat = [r.get("retrieval_latency_ms", 0) for r in per_item if r]
        if lat:
            lat_sorted = sorted(lat)
            metrics["retrieval_latency_p50_ms"] = round(lat_sorted[len(lat_sorted) // 2], 1)
            metrics["retrieval_latency_p95_ms"] = round(
                lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * 0.95))], 1
            )
        return metrics

    @staticmethod
    def _redlines(metrics: dict[str, float], mode: str) -> list[str]:
        warnings: list[str] = []
        if mode == "refusal":
            val = metrics.get("refusal_recall", -1)
            if val >= 0 and val < 0.9:
                warnings.append(f"红线告警: refusal_recall={val} < 0.9")
            return warnings
        if mode == "full":
            checks = [
                ("recall@5", 0.8),
                ("accuracy", 0.75),
                ("faithfulness", 0.9),
            ]
            for key, threshold in checks:
                val = metrics.get(key, -1)
                if val >= 0 and val < threshold:
                    warnings.append(f"红线告警: {key}={val} < {threshold}")
        return warnings
