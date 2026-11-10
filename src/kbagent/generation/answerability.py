"""闸 1：重排之后、生成之前，按 top1 分数决定拒答、弱证据或正常回答。

τ 不是通用常数。向量默认值按 ``BAAI/bge-small-zh-v1.5``、L2 经 ``1/(1+d)``、
样例笔记加库外集标定。重排默认值按 ``BAAI/bge-reranker-base`` 的 ``predict``
输出标定，那个数已经是 Sigmoid 概率，这里不再做第二次 sigmoid。
换嵌入、度量、重排模型或语料后都要重标。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings

# 拒答正文。产品输出，不是异常。
INSUFFICIENT_EVIDENCE = "知识库中没有直接依据回答这个问题。"
WEAK_EVIDENCE_HINT = "（依据较弱：请核对原文后再采用。）"

# 这些句子都算拒答。评测和引用后检共用，避免各写一套。
REFUSAL_MARKERS = (
    INSUFFICIENT_EVIDENCE,
    "知识库中没有找到相关内容。",
    "知识库中没有相关内容",
    "超出了我的知识库范围",
)


@dataclass(frozen=True)
class Answerability:
    action: str  # refuse | weak | answer
    confidence: str  # high | low
    top1_score: float
    tau_low: float
    tau_high: float
    score_kind: str
    reason: str | None = None
    calibrated: bool = True

    def as_meta(self) -> dict:
        return {
            "answerability": self.action,
            "confidence": self.confidence,
            "top1_score": self.top1_score,
            "tau_low": self.tau_low,
            "tau_high": self.tau_high,
            "score_kind": self.score_kind,
            "tau_calibrated": self.calibrated,
            "refusal_reason": self.reason,
        }


def is_refusal_text(text: str) -> bool:
    body = text or ""
    return any(marker in body for marker in REFUSAL_MARKERS)


def low_score_refusal(title: str) -> str:
    name = title.strip() or "未命名资料"
    return (
        f"{INSUFFICIENT_EVIDENCE}"
        f"最接近的是 [1]《{name}》，但相关度低于可答阈值。"
        "你可以补充相关笔记，或换个更具体的问题。"
    )


def _decide(score: float, low: float, high: float, kind: str, calibrated: bool) -> Answerability:
    if score < low:
        return Answerability(
            "refuse", "low", score, low, high, kind, "below_tau_low", calibrated
        )
    if score < high:
        return Answerability("weak", "low", score, low, high, kind, None, calibrated)
    return Answerability("answer", "high", score, low, high, kind, None, calibrated)


def assess(score: float, rerank_score: float | None, settings: Settings) -> Answerability:
    """``rerank_score`` 有值就用重排阈值。它必须已经和 ``KB_ANSWER_RERANK_TAU_*`` 同一把尺子。

    bge-reranker 的 predict 是概率，重排器原样写入。logit 模型由重排器做一次稳定 sigmoid
    再写入。这里不再 sigmoid，避免把 0 变成 0.5。
    """
    if not settings.answerability_enabled:
        top = rerank_score if rerank_score is not None else score
        return Answerability(
            "answer",
            "high",
            float(top),
            settings.answer_tau_low,
            settings.answer_tau_high,
            "disabled",
            None,
            False,
        )
    if rerank_score is not None:
        return _decide(
            float(rerank_score),
            float(settings.answer_rerank_tau_low),
            float(settings.answer_rerank_tau_high),
            "rerank",
            True,
        )
    return _decide(
        float(score),
        settings.answer_tau_low,
        settings.answer_tau_high,
        "vector",
        True,
    )
