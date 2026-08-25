"""LLM-as-judge：答案正确性 / 忠实度 / 相关性 / 引用准确率。"""

from __future__ import annotations

from ..config import Settings
from ..errors import LLMError

_CORRECTNESS_PROMPT = (
    "你是评测裁判。对照参考答案判断模型回答的正确性。\n"
    "问题：{query}\n参考答案：{expected}\n模型回答：{answer}\n"
    "只输出 JSON：{{\"label\": \"correct|partial|incorrect\"}}"
)

_FAITHFULNESS_PROMPT = (
    "你是事实一致性裁判。判断模型回答中的每个论断是否都被给定参考资料支持。\n"
    "参考资料：\n{context}\n\n模型回答：{answer}\n"
    "只输出 JSON：{{\"supported_claims\": 数字, \"unsupported_claims\": 数字}}"
)

_RELEVANCE_PROMPT = (
    "你是评测裁判。给模型回答与问题的相关性打分（1-5 分）。\n"
    "问题：{query}\n模型回答：{answer}\n"
    "只输出 JSON：{{\"score\": 数字}}"
)

_CITATION_PROMPT = (
    "你是引用质量裁判。检查模型回答中的 [n] 引用标注是否真实对应给定参考资料内容。\n"
    "参考资料：\n{context}\n\n模型回答：{answer}\n"
    "只输出 JSON：{{\"correct_citations\": 数字, \"total_citations\": 数字}}"
)


class LLMJudge:
    def __init__(self, llm, settings: Settings | None = None) -> None:
        self.llm = llm
        model = (settings.eval_judge_model if settings else "") or None
        self.model = model

    def correctness(self, query: str, answer: str, expected: str) -> str:
        if not expected:
            return "unknown"
        try:
            res = self.llm.chat_json(
                [{"role": "user", "content": _CORRECTNESS_PROMPT.format(
                    query=query, expected=expected, answer=answer
                )}]
            )
            return res.get("label", "unknown")
        except LLMError:
            return "unknown"

    def faithfulness(self, answer: str, context: str) -> float:
        """支持论断占比（0-1），无法判断返回 -1。"""
        try:
            res = self.llm.chat_json(
                [{"role": "user", "content": _FAITHFULNESS_PROMPT.format(
                    context=context[:6000], answer=answer
                )}]
            )
            supported = float(res.get("supported_claims", 0) or 0)
            unsupported = float(res.get("unsupported_claims", 0) or 0)
            total = supported + unsupported
            return supported / total if total > 0 else -1.0
        except (LLMError, ValueError):
            return -1.0

    def relevance(self, query: str, answer: str) -> int:
        try:
            res = self.llm.chat_json(
                [{"role": "user", "content": _RELEVANCE_PROMPT.format(
                    query=query, answer=answer
                )}]
            )
            return int(res.get("score", 0))
        except (LLMError, ValueError):
            return 0

    def citation_accuracy(self, answer: str, context: str) -> float:
        """正确引用占比（0-1），无引用返回 -1。"""
        try:
            res = self.llm.chat_json(
                [{"role": "user", "content": _CITATION_PROMPT.format(
                    context=context[:6000], answer=answer
                )}]
            )
            correct = float(res.get("correct_citations", 0) or 0)
            total = float(res.get("total_citations", 0) or 0)
            return correct / total if total > 0 else -1.0
        except (LLMError, ValueError):
            return -1.0
