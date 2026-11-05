"""RAG 生成管线：意图 → 改写 → 检索 → 重排 → 父扩写 → 编号引用提示词 → LLM。

支持多模态：命中图文单元且有视觉 LLM 配置时附加图片消息；否则用描述文本。
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..errors import LLMError
from ..models import Answer, SourceRef
from ..retrieval.parent_expander import ContextItem, ParentExpander
from ..storage.filters import Where
from .llm import LLMClient

_SYSTEM_PROMPT = (
    "你是一个个人知识库助手。请严格依据提供的参考资料回答问题。\n"
    "要求：\n"
    "1. 回答内容必须来自参考资料，使用 [1][2] 等编号标注引用来源（编号对应参考资料序号）。\n"
    "2. 如果参考资料中没有足够信息回答，请直接说明\"知识库中没有相关内容\"，不要编造。\n"
    "3. 用简洁、清晰的中文回答。\n"
)


class RAGPipeline:
    def __init__(
        self,
        settings: Settings,
        llm,
        query_processor,
        retriever,
        reranker,
        parent_expander: ParentExpander,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.query_processor = query_processor
        self.retriever = retriever
        self.reranker = reranker
        self.parent_expander = parent_expander

    def ask(
        self,
        query: str,
        top_k: int | None = None,
        where: Where = None,
        collection: str | None = None,
        rerank: bool | None = None,
        rewrite: bool | None = None,
        intent: bool | None = None,
        include_hidden: bool = False,
    ) -> Answer:
        top_k = top_k or self.settings.top_k
        rerank_on = self.settings.rerank_enabled if rerank is None else rerank
        rewrite_on = self.settings.query_rewrite_enabled if rewrite is None else rewrite
        intent_on = self.settings.intent_enabled if intent is None else intent

        # 本次请求的开关作为参数传入，不改共享 Settings。
        plan = self.query_processor.process(
            query,
            intent_enabled=intent_on,
            rewrite_enabled=rewrite_on,
        )

        meta: dict[str, Any] = {
            "intent": plan.intent,
            "rewritten_query": plan.rewritten_query,
            "original_query": query,
        }

        if plan.intent == "smalltalk":
            return Answer(
                text="你好！我是你的个人知识库助手，可以问我知识库里的内容，比如「什么是向量检索？」",
                sources=[], meta=meta,
            )
        if plan.intent == "management":
            return Answer(
                text="这是一个知识库管理类问题。请使用命令行 `kb list / kb stats / kb delete <doc_id>` 或 REST API（GET /v1/documents 等）来管理知识库。",
                sources=[], meta=meta,
            )
        if plan.intent == "out_of_scope":
            return Answer(
                text="这个问题超出了我的知识库范围，我无法回答。你可以问我知识库中已收录内容相关的问题。",
                sources=[], meta=meta,
            )

        # 检索候选 → 重排 → top-k
        candidates_n = self.settings.rerank_candidates if rerank_on else top_k
        candidates = self.retriever.search(
            plan.rewritten_query,
            top_k=max(candidates_n, 1),
            where=where,
            collection=collection,
            include_hidden=include_hidden,
        )
        if rerank_on:
            hits = self.reranker.rerank(plan.rewritten_query, candidates, top_k=top_k)
        else:
            hits = candidates[:top_k]

        if not hits:
            return Answer(
                text="知识库中没有找到相关内容。",
                sources=[], meta={**meta, "retrieved": 0},
            )

        context = self.parent_expander.expand(hits)
        sources = [
            SourceRef(
                doc_id=c.doc_id,
                title=c.title,
                source_path=c.source_path,
                score=c.score,
                snippet=c.text[:200],
                section=c.section,
                media=c.media,
            )
            for c in context
        ]

        try:
            answer_text = self._generate(plan.rewritten_query, context)
        except LLMError:
            # LLM 不可用 → 降级返回检索到的资料摘要
            digest = "\n\n".join(
                f"[{i + 1}] {c.title}\n{c.text[:300]}" for i, c in enumerate(context)
            )
            answer_text = (
                "⚠️ 无法生成回答（LLM 调用失败，请检查 KB_LLM_API_KEY / KB_LLM_BASE_URL 配置）。\n\n"
                f"以下为检索到的相关资料：\n\n{digest}"
            )
        return Answer(
            text=answer_text,
            sources=sources,
            meta={**meta, "retrieved": len(hits), "expanded": len(context)},
        )

    # ---- 生成 ----
    def _generate(self, query: str, context: list[ContextItem]) -> str:
        context_block = "\n\n".join(
            f"[{i + 1}] 标题：{c.title}\n{c.text}" for i, c in enumerate(context)
        )
        user_text = (
            f"参考资料：\n{context_block}\n\n问题：{query}\n\n"
            "请结合参考资料作答，并用 [n] 标注引用来源。"
        )

        # 多模态：命中图文单元且有视觉模型配置时，附加图片
        image_paths: list[str] = []
        for c in context:
            if c.media and c.media.image_path and c.media.image_path not in image_paths:
                image_paths.append(c.media.image_path)
        vision_model = self.settings.vision_llm_model
        if self.settings.multimodal_enabled and image_paths and vision_model:
            try:
                return self._vision_generate(
                    user_text,
                    image_paths[: self.settings.vision_max_images],
                    vision_model,
                )
            except LLMError:
                pass  # 视觉失败 → 降级为纯文本（描述已在上下文里）

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        return self.llm.chat(messages)

    def _vision_generate(self, user_text: str, images: list[str], model: str) -> str:
        s = self.settings
        client = LLMClient(
            base_url=s.vision_llm_base_url or s.llm_base_url,
            api_key=s.llm_api_key,
            model=model,
            temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens,
            timeout=s.llm_timeout,
        )
        return client.vision_chat(user_text, images, model=model)
