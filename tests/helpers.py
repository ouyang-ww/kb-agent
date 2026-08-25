"""测试辅助：确定性 FakeEmbedder / FakeLLM 等。"""

from __future__ import annotations

import math


class FakeEmbedder:
    """确定性嵌入：按字符袋（ord 哈希）生成向量，重叠文本 → 相似向量。"""

    DIM = 32

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.DIM
        for ch in text:
            v[ord(ch) % self.DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [round(x / norm, 6) for x in v]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class FakeLLM:
    """可编排的假 LLM：按提示词关键词返回不同 JSON/文本。"""

    def __init__(self) -> None:
        self.calls: list = []
        self.chat_text = "根据资料 [1]，向量检索通过 embedding 把文本映射为向量后进行相似度搜索。"
        self.intent = "kb_question"
        self.rewritten = "向量检索的原理"

    def chat(self, messages, **kwargs) -> str:
        self.calls.append(messages)
        return self.chat_text

    def chat_json(self, messages, **kwargs) -> dict:
        self.calls.append(messages)
        prompt = messages[-1]["content"] if messages else ""
        if "事实一致性" in prompt:
            return {"supported_claims": 2, "unsupported_claims": 0}
        if "相关性" in prompt:
            return {"score": 5}
        if "引用质量" in prompt:
            return {"correct_citations": 2, "total_citations": 2}
        if "意图" in prompt or "知识库问答路由" in prompt:
            return {"intent": self.intent, "rewritten_query": self.rewritten}
        return {"label": "correct"}

    def vision_chat(self, text, image_paths, model=None) -> str:
        return "这是一张测试图片，包含图表数据"


class FailingLLM(FakeLLM):
    """chat_json 阶段抛错（触发规则兜底）。"""

    def chat_json(self, messages, **kwargs) -> dict:
        from kbagent.errors import LLMError

        raise LLMError("LLM 不可用")


class FakeCaptioner:
    def caption(self, image_path, hint=""):
        return "这是一张测试图片，包含图表数据"
