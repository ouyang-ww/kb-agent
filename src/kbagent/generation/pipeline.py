"""RAG 生成管线：意图 → 改写 → 检索 → 重排 → 闸1 可答性 → 生成 → 闸2 引用契约。

支持多模态：命中图文单元且有视觉 LLM 配置时附加图片消息；否则用描述文本。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from ..errors import LLMError
from ..models import Answer, SourceRef
from ..retrieval.governance_pass import RetrievalPolicy, finish_hits, shape_hits
from ..retrieval.parent_expander import ContextItem, ParentExpander
from ..storage.filters import Where
from .answerability import (
    WEAK_EVIDENCE_HINT,
    assess,
    is_refusal_text,
    low_score_refusal,
)
from .citations import (
    CITATION_SYSTEM_PROMPT,
    apply_citation_contract,
    downgrade_confidence,
)
from .llm import LLMClient
from .verification import LLMClaimVerifier, Verification, verify_answer

logger = logging.getLogger(__name__)

CONFLICT_NOTE = "存在两种说法，下列来源互相矛盾，请分别查看，不要合并成一种结论。"


class RAGPipeline:
    def __init__(
        self,
        settings: Settings,
        llm,
        query_processor,
        retriever,
        reranker,
        parent_expander: ParentExpander,
        governance=None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.query_processor = query_processor
        self.retriever = retriever
        self.reranker = reranker
        self.parent_expander = parent_expander
        self.governance = governance
        # 测试可以换成假的校验器。生产环境留空，有 key 时用 LLM。
        self.verifier = None

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
        include_expired: bool = False,
        as_of: str | None = None,
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
                sources=[],
                meta={**meta, "confidence": "low", "refusal_reason": "out_of_scope", "answerability": "refuse"},
            )

        # 检索候选 → 重排 → top-k
        candidates_n = self.settings.rerank_candidates if rerank_on else top_k
        candidates = self.retriever.search(
            plan.rewritten_query,
            top_k=max(candidates_n, 1),
            where=where,
            collection=collection,
            include_hidden=include_hidden,
            include_expired=include_expired,
            as_of=as_of,
        )
        collection_name = collection or self.settings.default_collection
        diverse, prepared, conflict_pairs = shape_hits(
            candidates,
            governance=self.governance,
            vector_store=self.retriever.vector_store,
            collection=collection_name,
            embedder=self.retriever.embedder,
            top_k=top_k,
            policy=RetrievalPolicy(
                include_hidden=include_hidden,
                include_expired=include_expired,
                as_of=as_of,
                default_scope=self.settings.default_scope,
                doc_visible=None if include_hidden else self.retriever.doc_visibility,
                doc_dates=None if include_expired else self.retriever.doc_dates,
            ),
            cosine_threshold=float(self.settings.near_dup_cosine),
            hamming_guard=int(self.settings.near_dup_cosine_hamming),
            mmr_lambda=float(self.settings.mmr_lambda),
        )
        rerank_error = None
        if rerank_on:
            hits = self.reranker.rerank(plan.rewritten_query, diverse, top_k=top_k)
            if hits and hits[0].rerank_error:
                rerank_error = hits[0].rerank_error
        else:
            hits = diverse[:top_k]
        hits = finish_hits(hits, prepared, conflict_pairs)

        if not hits:
            return self._refuse(
                "知识库中没有找到相关内容。",
                [],
                {**meta, "retrieved": 0, "confidence": "low", "refusal_reason": "no_hits", "answerability": "refuse"},
            )

        # 重排没跑成时 rerank_score 为空，闸门用向量分。meta 必须写明这次退回，
        # 不能让 score_kind=vector 看起来像一次主动的向量标定。
        decision = assess(
            hits[0].score,
            None if rerank_error else hits[0].rerank_score,
            self.settings,
        )
        meta.update(decision.as_meta())
        meta["retrieved"] = len(hits)
        if rerank_error:
            meta["rerank_fallback"] = True
            meta["rerank_error"] = rerank_error

        context = self.parent_expander.expand(hits)
        sources = self._sources(context)
        meta["expanded"] = len(context)

        if decision.action == "refuse":
            title = context[0].title if context else hits[0].title
            return self._refuse(low_score_refusal(title), sources, meta)

        try:
            answer_text = self._generate(plan.rewritten_query, context)
        except LLMError:
            # LLM 不可用 → 降级返回检索到的资料摘要。摘要自带编号，引用后检能通过。
            digest = "\n\n".join(
                f"[{i + 1}] {c.title}\n{c.text[:300]}" for i, c in enumerate(context)
            )
            answer_text = (
                "⚠️ 无法生成回答（LLM 调用失败，请检查 KB_LLM_API_KEY / KB_LLM_BASE_URL 配置）。\n\n"
                f"以下为检索到的相关资料：\n\n{digest}"
            )
            meta["llm_error"] = True

        checked = apply_citation_contract(answer_text, len(context))
        meta["citation_check"] = checked.as_meta()
        if checked.forced_refusal:
            meta["confidence"] = "low"
            meta["refusal_reason"] = "invalid_citations"
            meta["answerability"] = "refuse"
            return self._refuse(checked.text, sources, meta)

        text = checked.text
        verified = self._verify_claims(text, context)
        if verified is not None:
            meta["verification"] = verified.as_meta()
            meta["gaps"] = list(verified.gaps)
            if verified.downgraded:
                meta["confidence"] = "low"
                meta["refusal_reason"] = "unverified_claims"
                meta["answerability"] = "refuse"
                return self._refuse(verified.text, sources, meta)
            if verified.skipped is None:
                text = verified.text

        meta["confidence"] = downgrade_confidence(
            decision.confidence, checked.confidence_steps
        )
        if decision.action == "weak" and not is_refusal_text(text):
            text = f"{text}\n\n{WEAK_EVIDENCE_HINT}"
            meta["confidence"] = "low"
        text = self._annotate(text, sources, conflict_pairs, meta)
        meta["refusal_reason"] = None
        return Answer(text=text, sources=sources, meta=meta)

    def _sources(self, context: list[ContextItem]) -> list[SourceRef]:
        return [
            SourceRef(
                doc_id=c.doc_id,
                title=c.title,
                source_path=c.source_path,
                score=c.score,
                snippet=c.text[:200],
                section=c.section,
                media=c.media,
                note=c.note,
            )
            for c in context
        ]

    def _verify_claims(self, text: str, context: list[ContextItem]) -> Verification | None:
        """严格模式才跑。没有 key、也没有注入校验器时跳过，不改已经通过的闸 1 / 闸 2。"""
        if not self.settings.answer_strict:
            return None
        if is_refusal_text(text):
            return None
        verifier = self.verifier
        if verifier is None and not (self.settings.llm_api_key or "").strip():
            return Verification(text=text, skipped="no_llm_key")
        if verifier is None:
            verifier = LLMClaimVerifier(self.llm)
        snippets = [item.text for item in context]
        return verify_answer(
            text,
            snippets,
            verifier,
            max_fail_rate=float(self.settings.answer_strict_max_fail_rate),
        )

    def _annotate(
        self,
        text: str,
        sources: list[SourceRef],
        conflict_pairs: set[frozenset[str]],
        meta: dict[str, Any],
    ) -> str:
        present = {source.doc_id for source in sources if source.doc_id}
        active = [pair for pair in conflict_pairs if pair <= present and len(pair) == 2]
        if active:
            meta["conflict"] = True
            meta["conflict_pairs"] = [sorted(pair) for pair in active]
            if "存在两种说法" not in text:
                text = f"{text}\n\n{CONFLICT_NOTE}"
        notes = [source.note for source in sources if source.note]
        if notes:
            meta["expired_sources"] = True
            extra = " ".join(dict.fromkeys(notes))
            if "可能已过时" not in text:
                text = f"{text}\n\n{extra}"
        return text

    def _refuse(self, text: str, sources: list[SourceRef], meta: dict[str, Any]) -> Answer:
        """拒答是正常输出：记原因和置信度，不抛异常。"""
        logger.info(
            "拒答 reason=%s confidence=%s top1=%s kind=%s",
            meta.get("refusal_reason"),
            meta.get("confidence"),
            meta.get("top1_score"),
            meta.get("score_kind"),
        )
        return Answer(text=text, sources=sources, meta=meta)

    # ---- 生成 ----
    def _generate(self, query: str, context: list[ContextItem]) -> str:
        context_block = "\n\n".join(
            f"[{i + 1}] 标题：{c.title}\n{c.text}" for i, c in enumerate(context)
        )
        user_text = (
            f"参考资料：\n{context_block}\n\n问题：{query}\n\n"
            "请只根据上述编号资料作答，每个事实句用 [n] 标注。"
            "不得引入资料之外的事实。资料不足时只回答固定拒答句，"
            "并在最后一行输出 SELF_CHECK JSON。"
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
            {"role": "system", "content": CITATION_SYSTEM_PROMPT},
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
