"""KnowledgeBase 门面：唯一业务入口（日常检索 + 组件集成的 Python API）。

线程安全；重型组件（嵌入模型、重排模型）懒加载。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .config import Settings
from .document_store.sqlite_store import SQLiteDocumentStore
from .embedding.local import SentenceTransformerEmbedder
from .errors import NotFoundError
from .generation.llm import LLMClient
from .generation.pipeline import RAGPipeline
from .ingestion.indexer import IngestReport, Indexer
from .models import Answer, Document, RetrievalHit
from .retrieval.parent_expander import ParentExpander
from .retrieval.query_processor import QueryProcessor
from .retrieval.reranker import Reranker
from .retrieval.retriever import Retriever
from .vectorstore.chroma_store import ChromaStore
from .vision.indexer import VisionIndexer


class KnowledgeBase:
    def __init__(
        self,
        settings: Settings | None = None,
        embedder=None,
        vector_store=None,
        doc_store=None,
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
        self._vector_store = vector_store or ChromaStore(self.settings.chroma_dir)
        self._doc_store = doc_store or SQLiteDocumentStore(self.settings.documents_db)
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
        self._retriever = Retriever(self._embedder, self._vector_store, self.settings)
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
    ) -> IngestReport:
        with self._lock:
            return self._indexer.ingest_path(
                path, collection=collection, force=force, recursive=recursive
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
            )

    def delete_document(self, doc_id: str, collection: str | None = None) -> bool:
        with self._lock:
            return self._indexer.delete_document(doc_id, collection)

    def update_document(
        self, doc_id: str, new_content: str, **kwargs
    ) -> str | None:
        with self._lock:
            return self._indexer.update_document(doc_id, new_content, **kwargs)

    # ================= 读取 =================
    def get_document(self, doc_id: str) -> Document | None:
        return self._doc_store.get(doc_id)

    def list_documents(self, collection: str | None = None) -> list[Document]:
        return self._doc_store.list(collection)

    def stats(self) -> dict[str, Any]:
        return self._indexer.stats()

    def collections(self) -> list[str]:
        return self._vector_store.collections()

    # ================= 检索 =================
    def search(
        self,
        query: str,
        top_k: int | None = None,
        where: dict[str, Any] | None = None,
        collection: str | None = None,
        rerank: bool | None = None,
        rewrite: bool | None = None,
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
            q, top_k=max(candidates_n, 1), where=where, collection=collection
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
        where: dict[str, Any] | None = None,
        collection: str | None = None,
        rerank: bool | None = None,
        rewrite: bool | None = None,
        intent: bool | None = None,
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
        self._doc_store.close()
