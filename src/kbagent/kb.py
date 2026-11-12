"""KnowledgeBase 门面：唯一业务入口（日常检索 + 组件集成的 Python API）。

线程安全；重型组件（嵌入模型、重排模型）懒加载。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .document_store.base import DocumentStore
from .embedding.local import SentenceTransformerEmbedder
from .errors import KBError, NotFoundError, StudyDisabledError
from .governance.validity import chunk_has_valid_until, chunk_valid_until, display_day, is_expired
from .governance.scope import visibility_scopes
from .flags import FeatureFlags
from .generation.llm import LLMClient
from .generation.pipeline import RAGPipeline
from .governance import ACTIVE_STATUS, HIDDEN_SCOPES, DocMeta, GovernanceStore, is_visible
from .governance.consumers import VectorIndexConsumer, drain_consumers
from .governance.vectors import sync_chunk_governance
from .governance.originals import RE_INGEST_HINT, migrate_document
from .ingestion.indexer import IngestReport, Indexer
from .models import Answer, Document, RetrievalHit
from .retrieval.governance_pass import RetrievalPolicy, finish_hits, shape_hits
from .retrieval.parent_expander import ParentExpander
from .retrieval.query_processor import QueryProcessor
from .retrieval.reranker import Reranker
from .retrieval.retriever import Retriever
from .storage.factory import make_document_store, make_vector_store
from .storage.filters import Where, eq
from .vectorstore.base import VectorStore
from .vision.indexer import VisionIndexer

logger = logging.getLogger(__name__)


def _stricter_visibility(
    doc_scope: str | None,
    doc_status: str | None,
    meta_scope: str | None,
    meta_status: str | None,
) -> tuple[str, str]:
    """文档行和治理元数据不一致时，保留更严的一边。"""
    scope = meta_scope or doc_scope or "default"
    status = meta_status or doc_status or ACTIVE_STATUS
    if doc_scope in HIDDEN_SCOPES or meta_scope in HIDDEN_SCOPES:
        scope = meta_scope if meta_scope in HIDDEN_SCOPES else doc_scope or scope
    if doc_status not in (None, "", ACTIVE_STATUS):
        status = doc_status or status
    if meta_status not in (None, "", ACTIVE_STATUS):
        status = meta_status or status
    return scope, status


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
        self.manifest_id = self.settings.embedding_manifest
        self.governance.upsert_manifest(
            self.manifest_id,
            model=self.settings.embedding_model,
            chunker_version=self.settings.chunker_version,
            collection=self.settings.default_collection,
        )
        self.governance.activate_manifest(self.manifest_id)
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
        # 学习库只在 study.enabled 打开且真正调用时创建。关闭时核心路径不碰它。
        self._study = None
        self._study_api = None
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
            doc_dates=self._doc_dates,
        )
        self._parent_expander = ParentExpander(
            self._doc_store,
            max_parent_chars=self.settings.max_parent_chars,
            mode=self.settings.parent_mode,
        )
        self._vision_error: str | None = None
        self._vision_indexer = vision_indexer
        if self._vision_indexer is None and self.settings.multimodal_enabled:
            try:
                self._vision_indexer = VisionIndexer(self.settings, self._llm)
            except Exception as exc:
                self._vision_indexer = None
                self._vision_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "VisionIndexer 初始化失败，多模态索引已关闭: %s",
                    self._vision_error,
                    exc_info=True,
                )
        self._indexer = Indexer(
            self.settings,
            self._embedder,
            self._vector_store,
            self._doc_store,
            vision_indexer=self._vision_indexer,
            governance=self.governance,
            llm=self._llm,
        )
        self._pipeline = RAGPipeline(
            self.settings,
            self._llm,
            self._query_processor,
            self._retriever,
            self._reranker,
            self._parent_expander,
            governance=self.governance,
        )
        self.last_text_ingest: dict[str, Any] | None = None
        self._vector_consumer = VectorIndexConsumer(
            self._vector_store, self._doc_store, self.settings
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
            doc_id = self._indexer.ingest_text(
                content,
                title=title,
                source_path=source_path,
                doc_type=doc_type,
                collection=collection,
                metadata=metadata,
                force=force,
                scope=scope,
            )
            partial = self._indexer._partial_index
            self.last_text_ingest = {
                "doc_id": doc_id,
                "ok": bool(doc_id) and partial is None,
                "partial": dict(partial) if partial else None,
            }
            return doc_id

    def delete_document(
        self,
        doc_id: str,
        collection: str | None = None,
        include_hidden: bool = False,
        actor: str = "local",
    ) -> bool:
        with self._lock:
            if not include_hidden and not self._is_doc_visible(doc_id):
                return False
            ok = self._indexer.delete_document(doc_id, collection)
            if ok:
                self.governance.record_audit(
                    "document.delete", doc_id, actor=actor
                )
                self.drain_consumers()
            return ok

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
        """默认检索是否可见。文档行或治理元数据任一标为不可见就隐藏。

        待清理如果只写成了文档行（治理更新失败），旧正文也不能再被 search 命中。
        """
        doc = self._doc_store.get(doc_id)
        if doc is not None and doc.is_hidden:
            return False
        meta = self.governance.get_meta(doc_id)
        if meta is not None:
            return is_visible(meta)
        return doc is not None

    def _doc_dates(self, doc_id: str) -> tuple[str | None, str | None]:
        meta = self.governance.get_meta(doc_id)
        if meta is None:
            return None, None
        return meta.valid_until, meta.last_verified

    def get_doc_meta(self, doc_id: str) -> DocMeta | None:
        return self.governance.get_meta(doc_id)

    def list_events(
        self,
        after_id: int = 0,
        limit: int = 100,
        unprocessed_only: bool = False,
    ) -> list[dict[str, Any]]:
        return self.governance.list_events(after_id, limit, unprocessed_only)

    def hide_document(self, doc_id: str, actor: str = "local") -> bool:
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
            self.governance.emit_event(
                doc_id,
                "hide",
                {"collection": doc.collection, "status": "hidden"},
            )
            self.governance.record_audit("document.hide", doc_id, actor=actor)
            self._sync_chunk_status(doc.collection, doc_id, "hidden")
            self.drain_consumers()
            return True

    def unhide_document(self, doc_id: str, actor: str = "local") -> bool:
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
            self.governance.emit_event(
                doc_id,
                "unhide",
                {"collection": doc.collection, "status": "active"},
            )
            self.governance.record_audit("document.unhide", doc_id, actor=actor)
            self._sync_chunk_status(doc.collection, doc_id, "active")
            self.drain_consumers()
            return True

    def vision_status(self) -> dict[str, Any]:
        """多模态是否启用、索引器是否可用，以及初始化失败原因。"""
        return {
            "enabled": bool(self.settings.multimodal_enabled),
            "available": self._vision_indexer is not None,
            "error": self._vision_error,
        }

    def stats(self) -> dict[str, Any]:
        data = self._indexer.stats()
        data["vision"] = self.vision_status()
        return data

    def repair(self) -> dict[str, Any]:
        """对账向量库与文档库，可重复执行。

        未完成的替换回到写入前的文档；没有父文档的向量会被删除；
        已标记待清理的旧文档会被删掉。没有快照的缺向量文档记在
        ``vectorless`` 中，便于告警。随后消化 doc_events 里还没被
        向量消费者处理的 hide / unhide / delete。
        """
        with self._lock:
            report = self._indexer.repair()
            report["consumers"] = self.drain_consumers()
            return report

    def drain_consumers(self) -> dict[str, Any]:
        """推进已实现的消费者。mentions / items / reports 只出现在 residual 里。"""
        return drain_consumers(self.governance, [self._vector_consumer])

    def _sync_chunk_status(self, collection: str, doc_id: str, status: str) -> None:
        try:
            sync_chunk_governance(
                self._vector_store,
                collection or self.settings.default_collection,
                doc_id,
                status=status,
            )
        except Exception as exc:
            logger.exception("文档 %s 的向量状态未能写成 %s", doc_id, status)
            raise KBError(
                f"文档 {doc_id} 的库内状态已更新，但向量元数据未能同步为 {status}: {exc}"
            ) from exc

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
        value = self.flags.set(name, enabled, actor=actor)
        self.governance.record_audit(
            "flag.set", name, actor=actor, detail={"enabled": value}
        )
        return value

    def _open_study(self):
        """打开学习库。模块关闭时失败，并且不创建 study.sqlite3。"""
        if not self.flag("study.enabled"):
            raise StudyDisabledError()
        if self._study is None:
            from .study.service import StudyService

            self._study = StudyService(self.settings.study_db)
        return self._study

    @property
    def study(self):
        """学习切片入口：today / answer。关闭时抛错，不跑学习链路。"""
        if not self.flag("study.enabled"):
            raise StudyDisabledError()
        if self._study_api is None:
            from .study.api import StudyAPI

            self._study_api = StudyAPI(self._open_study)
        return self._study_api

    # ================= 观测 / 回填 =================
    def get_embedding_manifest(self) -> dict[str, Any] | None:
        return self.governance.get_active_manifest()

    def list_answers(self, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self.governance.list_answers(after_id, limit)

    def list_audit(self, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self.governance.list_audit(after_id, limit)

    def backfill_governance(self, actor: str = "local") -> int:
        """为旧文档补充 docs_meta，并把 scope/status 写回已有切块。

        默认检索的向量 where 要求切块带 status=active 和 scope。
        升级前入库、元数据缺这两项的切块不会被搜到，跑一次本命令补上。
        返回新建的 docs_meta 条数；向量同步记在审计明细里。
        """
        created = 0
        synced = 0
        for doc in self._doc_store.list():
            meta = self.governance.get_meta(doc.doc_id)
            if meta is None:
                meta = DocMeta(
                    doc_id=doc.doc_id,
                    content_hash=doc.content_hash,
                    scope=doc.scope,
                    status=doc.status,
                )
                self.governance.upsert_meta(meta)
                created += 1
            scope, status = _stricter_visibility(doc.scope, doc.status, meta.scope, meta.status)
            until = chunk_valid_until(meta.valid_until)
            fill_open = meta.valid_until in (None, "")
            try:
                synced += sync_chunk_governance(
                    self._vector_store,
                    doc.collection or self.settings.default_collection,
                    doc.doc_id,
                    scope=scope,
                    status=status,
                    valid_until=None if fill_open else until,
                    fill_open_validity=fill_open,
                )
            except Exception:
                logger.warning(
                    "回填文档 %s 的向量 scope/status 失败", doc.doc_id, exc_info=True
                )
        if created or synced:
            self.governance.record_audit(
                "governance.backfill",
                actor=actor,
                detail={"created": created, "vectors_synced": synced},
            )
        return created

    def list_relations(
        self,
        *,
        status: str | None = None,
        rel_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.governance.list_relations(status=status, rel_type=rel_type, limit=limit)

    def list_near_duplicates(
        self, *, kind: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.governance.list_near_duplicates(kind=kind, limit=limit)

    def latest_doc(self, doc_id: str) -> str:
        """已确认的 supersedes 链的最新文档。pending 不改变结果。"""
        return self.governance.latest_doc(doc_id)

    def propose_relation(
        self,
        src_id: str,
        dst_id: str,
        rel_type: str,
        *,
        confidence: float = 1.0,
        created_by: str = "user",
    ) -> dict[str, Any] | None:
        return self.governance.propose_relation(
            src_id,
            dst_id,
            rel_type,
            confidence=confidence,
            created_by=created_by,
        )

    def confirm_relation(
        self,
        src_id: str,
        dst_id: str,
        rel_type: str,
        actor: str = "local",
    ) -> dict[str, Any]:
        """确认后才生效。

        ``supersedes`` / ``merged_into`` 只有在留下的那篇仍能被默认检索到时，
        才把另一边标成 superseded。后继是 private/secret/hidden 或已过期时，
        关系记为 confirmed，但旧文档保持 active，默认检索继续返回它。
        """
        row = self.governance.get_relation(src_id, dst_id, rel_type)
        if row is None:
            raise NotFoundError(f"关系不存在: {src_id} -{rel_type}-> {dst_id}")
        if row["status"] == "rejected":
            raise KBError("已拒绝的关系不能直接确认，请重新提议")
        updated = self.governance.set_relation_status(src_id, dst_id, rel_type, "confirmed")
        status_applied = False
        if rel_type == "supersedes":
            status_applied = self._supersede_if_successor_visible(src_id, dst_id)
        elif rel_type == "merged_into":
            status_applied = self._supersede_if_successor_visible(dst_id, src_id)
        result = dict(updated or row)
        if rel_type in {"supersedes", "merged_into"}:
            result["status_applied"] = status_applied
        self.governance.record_audit(
            "relation.confirm",
            f"{src_id}->{dst_id}",
            actor=actor,
            detail={"type": rel_type, "status_applied": status_applied},
        )
        self.drain_consumers()
        return result

    def _supersede_if_successor_visible(self, successor_id: str, older_id: str) -> bool:
        """后继能被默认检索到才把旧文档标成 superseded。返回是否改了状态。"""
        if not self._default_retrievable(successor_id):
            logger.warning(
                "确认取代 %s → %s，但新文档不在默认检索范围内，旧文档保持可检索",
                successor_id,
                older_id,
            )
            return False
        self._apply_superseded(older_id, successor_id)
        return True

    def _default_retrievable(self, doc_id: str) -> bool:
        """默认检索能否返回这篇：active、scope 在允许列表、valid_until 存在且未过期。"""
        if not doc_id or not self._is_doc_visible(doc_id):
            return False
        try:
            until, _verified = self._doc_dates(doc_id)
        except Exception:
            logger.warning("读取文档 %s 的时效失败，不把它当成可取代的后继", doc_id, exc_info=True)
            return False
        if is_expired(until):
            return False
        doc = self._doc_store.get(doc_id)
        collection = (doc.collection if doc is not None else "") or self.settings.default_collection
        try:
            rows = self._vector_store.get(collection, where=eq("doc_id", doc_id))
        except Exception:
            logger.warning("读取文档 %s 的切块失败，不把它当成可取代的后继", doc_id, exc_info=True)
            return False
        allowed = set(visibility_scopes(self.settings.default_scope))
        for row in rows:
            meta = row.metadata or {}
            if str(meta.get("status") or "") != ACTIVE_STATUS:
                continue
            scope = str(meta.get("scope") or "").strip()
            if scope not in allowed:
                continue
            if not chunk_has_valid_until(meta.get("valid_until")):
                continue
            if is_expired(display_day(meta.get("valid_until"))):
                continue
            return True
        return False

    def reject_relation(
        self,
        src_id: str,
        dst_id: str,
        rel_type: str,
        actor: str = "local",
    ) -> dict[str, Any]:
        row = self.governance.get_relation(src_id, dst_id, rel_type)
        if row is None:
            raise NotFoundError(f"关系不存在: {src_id} -{rel_type}-> {dst_id}")
        was_confirmed = row["status"] == "confirmed"
        updated = self.governance.set_relation_status(src_id, dst_id, rel_type, "rejected")
        if was_confirmed and rel_type == "supersedes":
            self._restore_if_unsuperseded(dst_id)
        elif was_confirmed and rel_type == "merged_into":
            self._restore_if_unsuperseded(src_id)
        self.governance.record_audit(
            "relation.reject",
            f"{src_id}->{dst_id}",
            actor=actor,
            detail={"type": rel_type},
        )
        return updated or row

    def _apply_superseded(self, doc_id: str, by: str) -> None:
        doc = self._doc_store.get(doc_id)
        if doc is None and self.governance.get_meta(doc_id) is None:
            return
        meta = self.governance.get_meta(doc_id) or DocMeta(doc_id=doc_id)
        meta.status = "superseded"
        self.governance.upsert_meta(meta)
        collection = ""
        if doc is not None:
            self._doc_store.set_status(doc_id, "superseded")
            collection = doc.collection
            self._sync_chunk_status(collection, doc_id, "superseded")
        self.governance.emit_event(
            doc_id,
            "supersede",
            {"by": by, "collection": collection, "status": "superseded"},
        )

    def _restore_if_unsuperseded(self, doc_id: str) -> None:
        if self.governance.confirmed_successor(doc_id):
            return
        meta = self.governance.get_meta(doc_id)
        if meta is None or meta.status != "superseded":
            return
        if is_expired(meta.valid_until):
            # 过期文档恢复成 active 后，默认检索仍会被 valid_until 排除。
            pass
        meta.status = "active"
        self.governance.upsert_meta(meta)
        doc = self._doc_store.get(doc_id)
        if doc is None:
            return
        self._doc_store.set_status(doc_id, "active")
        self._sync_chunk_status(doc.collection, doc_id, "active")

    def _stored_chunks(self, doc: Document) -> list:
        """向量库里这份文档已有的切块。迁移只用它们对齐定位。"""
        collection = doc.collection or self.settings.default_collection
        try:
            return self._vector_store.get(collection, where=eq("doc_id", doc.doc_id))
        except Exception:
            return []

    def migrate_parent_bodies(self, actor: str = "local") -> dict[str, Any]:
        """迁出已入库父全文。

        哈希与源文件一致才标 ``bound`` 并删除正文。不一致则标 ``diverged`` 并保留正文。
        源文件不在则标 ``orphaned_copy``。``diverged`` 时结果里带重新入库提示。
        """
        report: dict[str, Any] = {
            "bound": 0,
            "inline": 0,
            "orphaned_copy": 0,
            "diverged": 0,
            "unchanged": 0,
            "hash_upgraded": 0,
        }
        with self._lock:
            for doc in self._doc_store.list():
                outcome = migrate_document(doc, self._stored_chunks(doc))
                report[outcome] = int(report.get(outcome, 0)) + 1
                if outcome == "unchanged":
                    continue
                self._doc_store.upsert(doc)
            if report["diverged"]:
                report["re_ingest_hint"] = RE_INGEST_HINT.format(n=report["diverged"])
            changed = (
                report["bound"]
                + report["inline"]
                + report["orphaned_copy"]
                + report["diverged"]
                + report["hash_upgraded"]
            )
            if changed:
                self.governance.record_audit(
                    "governance.migrate_parents",
                    actor=actor,
                    detail=dict(report),
                )
        return report

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
        include_expired: bool = False,
        as_of: str | None = None,
    ) -> list[RetrievalHit]:
        top_k = top_k or self.settings.top_k
        rerank_on = self.settings.rerank_enabled if rerank is None else rerank
        rewrite_on = self.settings.query_rewrite_enabled if rewrite is None else rewrite

        q = query.strip()
        if rewrite_on:
            # 开关只作为这次调用的参数，不改共享 Settings，避免和 ask 并发时互相覆盖。
            plan = self._query_processor.process(
                q,
                rewrite_enabled=True,
                intent_enabled=self.settings.intent_enabled,
            )
            q = plan.rewritten_query

        candidates_n = self.settings.rerank_candidates if rerank_on else top_k
        candidates = self._retriever.search(
            q,
            top_k=max(candidates_n, 1),
            where=where,
            collection=collection,
            include_hidden=include_hidden,
            include_expired=include_expired,
            as_of=as_of,
        )
        collection_name = collection or self.settings.default_collection
        diverse, prepared, pairs = shape_hits(
            candidates,
            governance=self.governance,
            vector_store=self._vector_store,
            collection=collection_name,
            embedder=self._embedder,
            top_k=top_k,
            policy=RetrievalPolicy(
                include_hidden=include_hidden,
                include_expired=include_expired,
                as_of=as_of,
                default_scope=self.settings.default_scope,
                doc_visible=None if include_hidden else self._is_doc_visible,
                doc_dates=None if include_expired else self._doc_dates,
            ),
            cosine_threshold=float(self.settings.near_dup_cosine),
            hamming_guard=int(self.settings.near_dup_cosine_hamming),
            mmr_lambda=float(self.settings.mmr_lambda),
        )
        if rerank_on:
            hits = self._reranker.rerank(q, diverse, top_k=top_k)
        else:
            hits = diverse[:top_k]
        return finish_hits(hits, prepared, pairs)

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
        include_expired: bool = False,
        as_of: str | None = None,
    ) -> Answer:
        with self._lock:
            started = time.perf_counter()
            answer = self._pipeline.ask(
                query,
                top_k=top_k,
                where=where,
                collection=collection,
                rerank=rerank,
                rewrite=rewrite,
                intent=intent,
                include_hidden=include_hidden,
                include_expired=include_expired,
                as_of=as_of,
            )
            try:
                self.governance.log_answer(
                    query=query,
                    answer=answer.text,
                    rewritten_query=str(answer.meta.get("rewritten_query", "")),
                    retrieved=[
                        {
                            "doc_id": s.doc_id,
                            "title": s.title,
                            "score": s.score,
                        }
                        for s in answer.sources
                    ],
                    citations=[s.doc_id for s in answer.sources],
                    meta=answer.meta,
                    confidence=answer.meta.get("confidence"),
                    refusal_reason=answer.meta.get("refusal_reason"),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    verification=answer.meta.get("verification"),
                )
            except Exception:
                # 写日志失败不能吞掉已经生成的答案，但必须留下痕迹。
                logger.exception("写入 answer_log 失败，答案仍会返回")
                answer.meta["answer_log_error"] = True
            return answer

    # ================= 工具 =================
    def rebuild_reranker(self) -> None:
        """重排模型加载失败后可重试。"""
        self._reranker = Reranker(
            self.settings.rerank_model,
            enabled=self.settings.rerank_enabled,
            ignore_proxy=self.settings.ignore_http_proxy,
        )

    def close(self) -> None:
        study = self._study
        self._study = None
        if study is not None:
            study.close()
        self._indexer.close()
        self.flags.close()
        self.governance.close()
        self._doc_store.close()
        self._vector_store.close()
