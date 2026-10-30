"""KnowledgeBase 门面：唯一业务入口（日常检索 + 组件集成的 Python API）。

线程安全；重型组件（嵌入模型、重排模型）懒加载。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .config import Settings
from .document_store.base import DocumentStore
from .embedding.local import SentenceTransformerEmbedder
from .errors import NotFoundError
from .flags import FeatureFlags
from .generation.llm import LLMClient
from .generation.pipeline import RAGPipeline
from .governance import DocMeta, GovernanceStore, is_visible
from .ingestion.indexer import IngestReport, Indexer
from .models import Answer, Document, RetrievalHit
from .retrieval.parent_expander import ParentExpander
from .retrieval.query_processor import QueryProcessor
from .retrieval.reranker import Reranker
from .retrieval.retriever import Retriever
from .storage.factory import make_document_store, make_vector_store
from .storage.filters import Where
from .vectorstore.base import VectorStore
from .vision.indexer import VisionIndexer


class KnowledgeBase:
    def __init__(
        self,
        settings: Settings | None = None,
        embedder=None,
        vector_store: VectorStore | None = None,
        doc_store: DocumentStore | None = None,
        llm=None,
        vision_indexer=None,
    ) -> None:
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        # KB 自管 HF 模型缓存：默认 KB_STORAGE_DIR/hf-cache（可用 HF_HOME/HF_HUB_CACHE 覆盖）
        import os

        os.environ.setdefault("HF_HOME", str(self.settings.hf_cache_dir))
        self._lock = threading.RLock()

        self._embedder = embedder or SentenceTransformerEmbedder(
            self.settings.embedding_model,
            query_instruction=self.settings.embedding_query_instruction,
            batch_size=self.settings.embedding_batch_size,
            ignore_proxy=self.settings.ignore_http_proxy,
        )
        self._vector_store = vector_store or make_vector_store(self.settings)
        self._doc_store = doc_store or make_document_store(self.settings)
        self.governance = GovernanceStore(self.settings.documents_db)
        self.flags = FeatureFlags(
            self.settings.documents_db,
            defaults={
                "study.enabled": self.settings.study_enabled,
                "study.concepts": self.settings.study_concepts,
                "study.items": self.settings.study_items,
                "study.queue": self.settings.study_queue,
                "study.weekly": self.settings.study_weekly,
                "study.dashboard": self.settings.study_dashboard,
                "study.notifications": self.settings.study_notifications,
            },
        )
        self._llm = llm or LLMClient(
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            model=self.settings.llm_model,
            temperature=self.settings.llm_temperature,
            max_tokens=self.settings.llm_max_tokens,
            timeout=self.settings.llm_timeout,
        )

        self._reranker = Reranker(
            self.settings.rerank_model,
            enabled=self.settings.rerank_enabled,
            ignore_proxy=self.settings.ignore_http_proxy,
        )
        self._query_processor = QueryProcessor(self._llm, self.settings)
        self._retriever = Retriever(
            self._embedder,
            self._vector_store,
            self.settings,
            doc_visibility=self._is_doc_visible,
        )
        self._parent_expander = ParentExpander(
            self._doc_store,
            max_parent_chars=self.settings.max_parent_chars,
            mode=self.settings.parent_mode,
        )
        self._vision_indexer = vision_indexer
        if self._vision_indexer is None and self.settings.multimodal_enabled:
            try:
                self._vision_indexer = VisionIndexer(self.settings, self._llm)
            except Exception:
                self._vision_indexer = None
        self._indexer = Indexer(
            self.settings,
            self._embedder,
            self._vector_store,
            self._doc_store,
            vision_indexer=self._vision_indexer,
            governance=self.governance,
        )
        self._pipeline = RAGPipeline(
            self.settings,
            self._llm,
            self._query_processor,
            self._retriever,
            self._reranker,
            self._parent_expander,
        )

    # ================= 写入 / 管理 =================
    def ingest(
        self,
        path: str | Path,
        collection: str | None = None,
        force: bool = False,
        recursive: bool = True,
        scope: str | None = None,
    ) -> IngestReport:
        with self._lock:
            return self._indexer.ingest_path(
                path,
                collection=collection,
                force=force,
                recursive=recursive,
                scope=scope,
            )

    def ingest_text(
        self,
        content: str,
        title: str | None = None,
        source_path: str = "",
        doc_type: str = "text",
        collection: str | None = None,
        metadata: dict | None = None,
        force: bool = False,
        scope: str | None = None,
    ) -> str | None:
        with self._lock:
            return self._indexer.ingest_text(
                content,
                title=title,
                source_path=source_path,
                doc_type=doc_type,
                collection=collection,
                metadata=metadata,
                force=force,
                scope=scope,
            )

    def delete_document(
        self,
        doc_id: str,
        collection: str | None = None,
        include_hidden: bool = False,
    ) -> bool:
        with self._lock:
            if not include_hidden and not self._is_doc_visible(doc_id):
                return False
            return self._indexer.delete_document(doc_id, collection)

    def update_document(
        self,
        doc_id: str,
        new_content: str,
        include_hidden: bool = False,
        **kwargs,
    ) -> str | None:
        with self._lock:
            if not include_hidden and not self._is_doc_visible(doc_id):
                raise NotFoundError(f"文档不存在: {doc_id}")
            return self._indexer.update_document(doc_id, new_content, **kwargs)

    # ================= 读取 =================
    def get_document(
        self, doc_id: str, include_hidden: bool = False
    ) -> Document | None:
        doc = self._doc_store.get(doc_id)
        if doc is None:
            return None
        if not include_hidden and not self._is_doc_visible(doc_id):
            return None
        return doc

    def list_documents(
        self, collection: str | None = None, include_hidden: bool = False
    ) -> list[Document]:
        docs = self._doc_store.list(collection)
        if include_hidden:
            return docs
        return [d for d in docs if self._is_doc_visible(d.doc_id)]

    # ================= 治理 =================
    def _is_doc_visible(self, doc_id: str) -> bool:
        meta = self.governance.get_meta(doc_id)
        if meta is not None:
            return is_visible(meta)
        doc = self._doc_store.get(doc_id)
        return bool(doc and not doc.is_hidden)

    def get_doc_meta(self, doc_id: str) -> DocMeta | None:
        return self.governance.get_meta(doc_id)

    def list_events(
        self,
        after_id: int = 0,
        limit: int = 100,
        unprocessed_only: bool = False,
    ) -> list[dict[str, Any]]:
        return self.governance.list_events(after_id, limit, unprocessed_only)

    def hide_document(self, doc_id: str) -> bool:
        with self._lock:
            doc = self._doc_store.get(doc_id)
            if doc is None:
                return False
            meta = self.governance.get_meta(doc_id) or DocMeta(
                doc_id=doc_id, content_hash=doc.content_hash, scope=doc.scope
            )
            meta.status = "hidden"
            self.governance.upsert_meta(meta)
            self._doc_store.set_status(doc_id, "hidden")
            self.governance.emit_event(doc_id, "hide")
            return True

    def unhide_document(self, doc_id: str) -> bool:
        with self._lock:
            doc = self._doc_store.get(doc_id)
            if doc is None:
                return False
            meta = self.governance.get_meta(doc_id) or DocMeta(
                doc_id=doc_id, content_hash=doc.content_hash, scope=doc.scope
            )
            meta.status = "active"
            self.governance.upsert_meta(meta)
            self._doc_store.set_status(doc_id, "active")
            self.governance.emit_event(doc_id, "unhide")
            return True

    def stats(self) -> dict[str, Any]:
        return self._indexer.stats()

    def collections(self) -> list[str]:
        return self._vector_store.collections()

    # ================= 运行时开关 =================
    def get_flags(self) -> dict[str, bool]:
        return self.flags.all()

    def describe_flags(self) -> dict[str, dict]:
        return self.flags.describe()

    def flag(self, name: str) -> bool:
        return self.flags.get(name)

    def set_flag(self, name: str, enabled: bool, actor: str = "local") -> bool:
        return self.flags.set(name, enabled, actor=actor)

    # ================= 检索 =================
    def search(
        self,
        query: str,
        top_k: int | None = None,
        where: Where = None,
        collection: str | None = None,
        rerank: bool | None = None,
        rewrite: bool | None = None,
        include_hidden: bool = False,
    ) -> list[RetrievalHit]:
        top_k = top_k or self.settings.top_k
        rerank_on = self.settings.rerank_enabled if rerank is None else rerank
        rewrite_on = self.settings.query_rewrite_enabled if rewrite is None else rewrite

        q = query.strip()
        if rewrite_on:
            saved = self.settings.query_rewrite_enabled
            self.settings.query_rewrite_enabled = True
            try:
                plan = self._query_processor.process(q)
                q = plan.rewritten_query
            finally:
                self.settings.query_rewrite_enabled = saved

        candidates_n = self.settings.rerank_candidates if rerank_on else top_k
        hits = self._retriever.search(
            q,
            top_k=max(candidates_n, 1),
            where=where,
            collection=collection,
            include_hidden=include_hidden,
        )
        if rerank_on:
            hits = self._reranker.rerank(q, hits, top_k=top_k)
        else:
            hits = hits[:top_k]
        return hits

    # ================= RAG 问答 =================
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
        with self._lock:
            return self._pipeline.ask(
                query,
                top_k=top_k,
                where=where,
                collection=collection,
                rerank=rerank,
                rewrite=rewrite,
                intent=intent,
                include_hidden=include_hidden,
            )

    # ================= 工具 =================
    def rebuild_reranker(self) -> None:
        """重排模型加载失败后可重试。"""
        self._reranker = Reranker(
            self.settings.rerank_model,
            enabled=self.settings.rerank_enabled,
            ignore_proxy=self.settings.ignore_http_proxy,
        )

    def close(self) -> None:
        self.flags.close()
        self.governance.close()
        self._doc_store.close()
        self._vector_store.close()
