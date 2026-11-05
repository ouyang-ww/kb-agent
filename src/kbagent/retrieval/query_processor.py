"""检索层：意图识别 + query 改写（LLM 结构化输出，规则关键词兜底）。"""

from __future__ import annotations

import json

from ..config import Settings
from ..errors import LLMError
from ..models import QueryPlan

_GREETINGS = [
    "你好", "您好", "嗨", "哈喽", "hello", "hi", "hey",
    "早上好", "下午好", "晚上好", "谢谢", "感谢", "thanks", "thank you",
]
_MANAGEMENT = [
    "列出", "统计", "有多少", "几个文档", "删除", "管理", "文档列表",
    "有哪些文档", "总共", "收藏", "list", "delete", "stats",
]


class QueryProcessor:
    """检索前处理：意图识别（kb_question/smalltalk/management/out_of_scope）与查询改写。

    意图与改写共用一次 LLM 调用（返回 JSON）；LLM 不可用时走规则兜底。
    """

    def __init__(self, llm, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    def process(
        self,
        query: str,
        *,
        intent_enabled: bool | None = None,
        rewrite_enabled: bool | None = None,
    ) -> QueryPlan:
        query = query.strip()
        if intent_enabled is None:
            intent_enabled = self.settings.intent_enabled
        if rewrite_enabled is None:
            rewrite_enabled = self.settings.query_rewrite_enabled
        if not intent_enabled and not rewrite_enabled:
            return QueryPlan(intent="kb_question", rewritten_query=query, original_query=query)
        try:
            result = self._llm_classify(query)
            intent = (
                result.get("intent", "kb_question")
                if intent_enabled else "kb_question"
            )
            rewritten = (
                result.get("rewritten_query") or query
                if rewrite_enabled else query
            )
            if intent not in {"kb_question", "smalltalk", "management", "out_of_scope"}:
                intent = "kb_question"
            return QueryPlan(intent=intent, rewritten_query=rewritten, original_query=query)
        except (LLMError, ValueError, json.JSONDecodeError):
            intent, rewritten = self._rules(query)
            return QueryPlan(intent=intent, rewritten_query=rewritten, original_query=query)

    def _llm_classify(self, query: str) -> dict:
        prompt = (
            "你是知识库问答路由。判断用户问题的意图并改写为更适合向量检索的查询。\n"
            "意图只允许以下之一：\n"
            "- kb_question：需要检索知识库才能回答的知识类问题\n"
            "- smalltalk：打招呼、寒暄、道谢等无需检索的对话\n"
            "- management：关于知识库本身的管理操作（列出/统计/删除文档等）\n"
            "- out_of_scope：与知识库无关、无法回答的问题\n"
            "改写要求：补充上下文、展开缩写、去除口语，保留原语义，便于检索。若无需改写则原样返回。\n"
            f"用户问题：{query}\n"
            "只输出 JSON：{\"intent\": \"...\", \"rewritten_query\": \"...\"}"
        )
        result = self.llm.chat_json([{"role": "user", "content": prompt}])
        if not isinstance(result, dict):
            raise ValueError("意图识别返回非 JSON")
        return result

    def _rules(self, query: str) -> tuple[str, str]:
        q = query.strip().lower()
        if any(k in q for k in _GREETINGS) and len(query) <= 30:
            return "smalltalk", query
        if any(k in q for k in _MANAGEMENT):
            return "management", query
        return "kb_question", query
