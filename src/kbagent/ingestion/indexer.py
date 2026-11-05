"""索引器：编排入库链路 loaders → cleaner → chunker → 嵌入 → Chroma(子) + SQLite(父记录)。

绑定源文件时 SQLite 不保存整篇正文。

含内容哈希去重、update/delete 级联清理、统计与报告。
向量库与 SQLite 无法同一事务提交：破坏性写入前先落修复日志，失败则补偿，
进程中断后用 ``repair`` 恢复。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..document_store.base import DocumentStore
from ..errors import IngestError, NotFoundError
from ..governance import DocMeta, GovernanceStore
from ..models import (
    SOURCE_BOUND,
    SOURCE_INLINE,
    SOURCE_ORPHANED,
    Document,
    MediaRef,
)
from ..storage.filters import eq
from ..vectorstore.base import StoredVector, VectorStore
from .chunker import Chunker
from .cleaner import Cleaner
from .journal import IngestJournal, dump_document, dump_vector, load_document, load_vector
from .loaders import (
    LEGACY_HASH_HEX_LEN,
    content_hash,
    doc_id_for,
    hashes_equal,
    is_legacy_hash,
    load_file,
    scan_path,
)
from .source_text import locate_chunks, summarize

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def _source_kind(source_path: str, requested: str | None) -> str:
    """有仍存在的外部文件则为 bound；声称了路径但文件不在则为 orphaned_copy。"""
    if requested == SOURCE_INLINE:
        return SOURCE_INLINE
    path = (source_path or "").strip()
    if path and Path(path).is_file():
        return SOURCE_BOUND
    if path:
        return SOURCE_ORPHANED
    return SOURCE_INLINE


def _legacy_inline_id(collection: str, title: str, full_hash: str) -> str:
    """旧 inline 身份把截断后的内容哈希拼进主键。"""
    legacy_identity = f"{collection}:{title}:{full_hash[:LEGACY_HASH_HEX_LEN]}"
    return doc_id_for(legacy_identity)[:LEGACY_HASH_HEX_LEN]


def _scalarize(meta: dict) -> dict:
    """Chroma 元数据只接受标量值，过滤掉嵌套结构。"""
    return {
        k: v
        for k, v in meta.items()
        if isinstance(v, (str, int, float, bool)) or v is None
    }


def _plain_meta(meta: dict | None) -> dict:
    """回滚写入用的元数据：去掉 None 和 Chroma 读回来的 numpy 标量。"""
    out: dict = {}
    for key, value in (meta or {}).items():
        if isinstance(value, bool) or isinstance(value, (str, int, float)):
            out[key] = value
        elif hasattr(value, "item"):
            out[key] = value.item()
    return out


class Indexer:
    def __init__(
        self,
        settings: Settings,
        embedder,
        vector_store: VectorStore,
        doc_store: DocumentStore,
        vision_indexer=None,
        governance: GovernanceStore | None = None,
    ) -> None:
        self.settings = settings
        self.embedder = embedder
        self.vector_store = vector_store
        self.doc_store = doc_store
        self.vision_indexer = vision_indexer
        self.governance = governance
        self.chunker = Chunker(settings.chunk_size, settings.chunk_overlap)
        self.cleaner = Cleaner()
        # 与文档库同一文件、独立连接。删除/替换向量之前先提交一行日志。
        self.journal = IngestJournal(settings.documents_db)

    # ---- 入口 ----
    def ingest_path(
        self,
        path: str | Path,
        collection: str | None = None,
        force: bool = False,
        recursive: bool = True,
        scope: str | None = None,
    ) -> IngestReport:
        files = scan_path(Path(path), recursive=recursive)
        report = IngestReport()
        for f in files:
            try:
                raws = load_file(f)
                for raw in raws:
                    raw["collection"] = collection or self.settings.default_collection
                    if scope:
                        raw["scope"] = scope
                    doc_id = self._ingest_raw(raw, force=force)
                    if doc_id:
                        report.added.append(doc_id)
                    else:
                        report.skipped.append(str(f))
            except IngestError as exc:
                report.failed.append({"path": str(f), "error": str(exc)})
        return report

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
        """入库一段文本。

        ``source_path`` 指向仍存在的文件时视为绑定原件：索引和扩写都读该文件，
        不把整篇正文写入 SQLite。没有外部路径时标为 ``inline``，正文留在应用库。
        """
        raw = {
            "title": title or (Path(source_path).stem if source_path else "untitled"),
            "source_path": source_path,
            "doc_type": doc_type,
            "content": content,
            "metadata": metadata or {},
            "collection": collection or self.settings.default_collection,
            "scope": scope,
        }
        return self._ingest_raw(raw, force=force)

    def _adopt_legacy_doc(
        self,
        full_id: str,
        source_path: str,
        collection: str,
        title: str,
        full_hash: str,
        cleaned: str,
    ) -> Document | None:
        """沿用旧的 12 hex doc_id。路径或全文对不上就放弃，避免截断碰撞覆盖别的文档。"""
        if source_path:
            row = self.doc_store.get(full_id[:LEGACY_HASH_HEX_LEN])
            if row is None:
                return None
            if (row.source_path or "") != source_path:
                logger.warning(
                    "旧 doc_id %s 属于另一路径，新文档改用完整哈希",
                    row.doc_id,
                )
                return None
            return row
        row = self.doc_store.get(_legacy_inline_id(collection, title, full_hash))
        if row is None:
            return None
        if (row.source_path or "").strip():
            return None
        if row.collection != collection or row.title != title:
            return None
        if row.full_text:
            if content_hash(row.full_text) != full_hash and row.full_text != cleaned:
                logger.warning(
                    "旧 doc_id %s 的全文与当前内容不一致，拒绝按截断 id 覆盖",
                    row.doc_id,
                )
                return None
            return row
        if row.content_hash and not hashes_equal(row.content_hash, full_hash):
            logger.warning(
                "旧 doc_id %s 的 content_hash 与当前内容不一致，拒绝覆盖",
                row.doc_id,
            )
            return None
        return row

    # ---- 核心 ----
    def _ingest_raw(self, raw: dict, force: bool) -> str | None:
        collection = raw.get("collection") or self.settings.default_collection
        source_path = raw.get("source_path", "") or ""
        title = raw.get("title", "untitled")
        content: str = raw.get("content", "")
        user_meta = raw.get("metadata") or {}
        # 绑定文件以磁盘为准，避免提交文本和回读窗口各写一份。
        if (
            source_path
            and Path(source_path).is_file()
            and raw.get("source_kind") != SOURCE_INLINE
        ):
            loaded = load_file(Path(source_path))
            if loaded and (loaded[0].get("content") or "").strip():
                content = loaded[0]["content"]
                raw["doc_type"] = raw.get("doc_type") or loaded[0].get("doc_type", "text")
        scope = raw.get("scope") or user_meta.get("scope") or self.settings.default_scope
        status = "active"
        if not content.strip():
            raise IngestError("空内容，跳过")

        clean_result = self.cleaner.clean(content)
        cleaned = clean_result.text
        h = content_hash(cleaned)

        # 同一 source_path 的 doc_id 稳定。先算出 id，再决定跳过还是替换，
        # 避免内容变更时直接 add：Chroma 对已有 id 不覆盖，chunk 变少会留下旧块。
        # 新文档用完整 SHA256。旧库 doc_id 是同一哈希的前 12 hex，对得上才沿用，
        # 避免把已入库文件再写成另一条，也避免截断碰撞时覆盖别人的文档。
        identity = source_path or f"{collection}:{title}:{h}"
        doc_id = doc_id_for(identity)
        prior = self.doc_store.get(doc_id)
        if prior is None:
            legacy = self._adopt_legacy_doc(
                doc_id, source_path, collection, title, h, cleaned
            )
            if legacy is not None:
                prior = legacy
                doc_id = legacy.doc_id

        # 去重只认完整哈希。多行碰撞时 get_by_hash 返回 None，不能跳过。
        duplicate = self.doc_store.get_by_hash(h)
        if duplicate and not force:
            return None
        if prior and prior.content_hash == h and not force:
            return None
        if (
            prior
            and is_legacy_hash(prior.content_hash)
            and hashes_equal(prior.content_hash, h)
        ):
            logger.info(
                "doc_id=%s 的 content_hash 仍是 12 hex，本次入库升级为完整 SHA256",
                prior.doc_id,
            )

        chunks = self.chunker.chunk(doc_id, cleaned, base_meta=_scalarize({
            **user_meta,
            "doc_id": doc_id,
            "title": title,
            "source_path": source_path,
            "doc_type": raw.get("doc_type", "text"),
            "scope": scope,
            "status": status,
        }))

        media_refs: list[MediaRef] = []
        if self.vision_indexer:
            media_chunks, media_refs = self.vision_indexer.process_document(
                doc_id=doc_id,
                title=title,
                doc_type=raw.get("doc_type", "text"),
                content=content,
                source_path=source_path,
                collection=collection,
            )
            chunks.extend(media_chunks)

        if not chunks:
            raise IngestError("切块后无内容，跳过")

        source_kind = _source_kind(source_path, raw.get("source_kind"))
        if source_kind == SOURCE_INLINE:
            # inline 没有外部原件，不把临时路径或声称的路径当成绑定源
            source_path = ""
        locations = locate_chunks(cleaned, chunks)
        for chunk, loc in zip(chunks, locations):
            if loc.start >= 0:
                chunk.metadata["char_start"] = loc.start
                chunk.metadata["char_end"] = loc.end

        ids = [c.chunk_id for c in chunks]
        texts = [c.text for c in chunks]
        manifest_id = self.settings.embedding_manifest
        metas = [
            {
                **c.metadata,
                "doc_id": c.doc_id,
                "title": title,
                "source_path": source_path,
                "section": c.section,
                # 向量版本元数据：迁移/回滚时用于识别向量所属模型与切块版本
                "embedding_manifest_id": manifest_id,
                "embedding_model": self.settings.embedding_model,
                "chunker_version": self.settings.chunker_version,
                "scope": scope,
                "status": status,
                **(c.media.to_metadata() if c.media else {}),
            }
            for c in chunks
        ]

        # 嵌入放在任何删除之前。清洗 / 嵌入失败时，旧文档和旧向量都还在。
        embeddings: list[list[float]] = []
        batch = self.settings.embedding_batch_size
        for i in range(0, len(texts), batch):
            embeddings.extend(self.embedder.embed_texts(texts[i : i + batch]))

        now = _utcnow()
        # 绑定原件只留在文件系统。inline / orphaned_copy 才把正文写入应用库。
        stored_body = "" if source_kind == SOURCE_BOUND else cleaned
        doc = Document(
            doc_id=doc_id,
            title=title,
            full_text=stored_body,
            source_path=source_path,
            doc_type=raw.get("doc_type", "text"),
            content_hash=h,
            metadata={**user_meta, "clean": clean_result.stats},
            collection=collection,
            created_at=prior.created_at if prior else now,
            updated_at=now,
            chunk_ids=ids,
            media=media_refs,
            scope=scope,
            status=status,
            summary=summarize(cleaned),
            source_kind=source_kind,
            chunk_locations=locations,
        )
        self._commit_ingest(
            prior=prior,
            doc=doc,
            ids=ids,
            embeddings=embeddings,
            texts=texts,
            metas=metas,
            scope=scope,
            status=status,
            content_hash=h,
            exclude_from_learning=bool(user_meta.get("exclude_from_learning", False)),
        )

        # 相同内容强制写入到另一个 doc_id：新文档成功后再删旧副本。
        if duplicate and force and duplicate.doc_id != doc_id:
            self._delete_superseded(duplicate.doc_id, duplicate.collection, doc_id)
        return doc_id

    def _commit_ingest(
        self,
        *,
        prior: Document | None,
        doc: Document,
        ids: list[str],
        embeddings: list[list[float]],
        texts: list[str],
        metas: list[dict],
        scope: str,
        status: str,
        content_hash: str,
        exclude_from_learning: bool,
    ) -> None:
        """写入向量和父文档。同 doc_id 先删旧向量再 add；失败则回滚。

        Chroma 的 add 遇到已有 id 不会覆盖，chunk 变少时高序号也会残留，
        所以替换必须按 doc_id 清掉旧向量。清掉之前把旧向量快照写入修复日志，
        这样进程在删除和写入之间退出后，``repair`` 仍能把旧文档检索回来。
        异常路径用内存快照立即回滚；回滚自己也失败时留下日志，交给 ``repair``。
        """
        collection = doc.collection
        replacing = prior is not None
        same_collection = prior is not None and prior.collection == collection
        snapshot: list[StoredVector] = []
        if replacing and prior is not None:
            snapshot = self._snapshot_vectors(prior.collection, doc.doc_id)
        journal_id = self.journal.begin(
            "commit",
            doc.doc_id,
            collection,
            {
                "new_doc": dump_document(doc),
                "prior_doc": dump_document(prior) if prior else None,
                "prior_vectors": [dump_vector(row) for row in snapshot],
                "new_ids": list(ids),
                "same_collection": same_collection,
            },
        )
        deleted_old = False
        wrote_new_vectors = False
        wrote_sqlite = False
        wrote_meta = False
        prior_meta = None
        if replacing and self.governance is not None:
            prior_meta = self.governance.get_meta(doc.doc_id)

        try:
            if same_collection and snapshot:
                # 同 doc_id 替换：先删光该文档的向量，再写入新 chunk。
                # 标记放在删除之前，删除抛错时也按「旧向量可能已经不在」回滚。
                deleted_old = True
                self.vector_store.delete(collection, where=eq("doc_id", doc.doc_id))
            # 标记放在 add 之前，写入中途失败时回滚会清掉半截向量。
            wrote_new_vectors = True
            self.vector_store.add(collection, ids, embeddings, texts, metas)

            self.doc_store.upsert(doc)
            wrote_sqlite = True

            if self.governance is not None:
                self.governance.upsert_meta(
                    DocMeta(
                        doc_id=doc.doc_id,
                        content_hash=content_hash,
                        scope=scope,
                        status=status,
                        exclude_from_learning=exclude_from_learning,
                    )
                )
                wrote_meta = True
                self.governance.emit_event(
                    doc.doc_id,
                    "ingest",
                    {"scope": scope, "collection": collection},
                )
                # 原地替换不增加文档数；新 doc_id 再删旧文档才会一增一减。
                if not replacing:
                    self.governance.increment_manifest_docs(
                        self.settings.embedding_manifest, 1
                    )

            if replacing and not same_collection and prior is not None:
                self.vector_store.delete(
                    prior.collection, where=eq("doc_id", doc.doc_id)
                )
        except Exception as exc:
            try:
                self._rollback_ingest(
                    prior=prior,
                    doc=doc,
                    snapshot=snapshot,
                    deleted_old=deleted_old,
                    wrote_new_vectors=wrote_new_vectors,
                    wrote_sqlite=wrote_sqlite,
                    wrote_meta=wrote_meta,
                    same_collection=same_collection,
                    prior_meta=prior_meta,
                )
            except Exception as rollback_exc:
                logger.error(
                    "入库失败且无法恢复文档 %s，已保留修复日志: %s",
                    doc.doc_id,
                    rollback_exc,
                )
                raise IngestError(
                    f"入库失败且无法恢复旧文档，请运行 kb repair: {rollback_exc}"
                ) from exc
            self._safe_finish(journal_id)
            raise
        self._safe_finish(journal_id)

    def _snapshot_vectors(self, collection: str, doc_id: str) -> list[StoredVector]:
        return self.vector_store.get(
            collection,
            where=eq("doc_id", doc_id),
            include_embeddings=True,
        )

    def _restore_vectors(
        self,
        collection: str,
        doc_id: str,
        snapshot: list[StoredVector],
    ) -> None:
        """丢掉这次没写完的向量，再把快照加回去。"""
        self.vector_store.delete(collection, where=eq("doc_id", doc_id))
        if not snapshot:
            return
        ids: list[str] = []
        embeddings: list[list[float]] = []
        documents: list[str] = []
        metadatas: list[dict] = []
        for row in snapshot:
            if not row.embedding:
                raise IngestError("向量回滚失败：快照缺少嵌入")
            ids.append(row.id)
            embeddings.append([float(x) for x in row.embedding])
            documents.append(row.document)
            metadatas.append(_plain_meta(row.metadata))
        self.vector_store.add(collection, ids, embeddings, documents, metadatas)

    def _rollback_ingest(
        self,
        *,
        prior: Document | None,
        doc: Document,
        snapshot: list[StoredVector],
        deleted_old: bool,
        wrote_new_vectors: bool,
        wrote_sqlite: bool,
        wrote_meta: bool,
        same_collection: bool,
        prior_meta: DocMeta | None,
    ) -> None:
        collection = doc.collection
        if same_collection and (deleted_old or wrote_new_vectors):
            self._restore_vectors(collection, doc.doc_id, snapshot)
        elif wrote_new_vectors:
            self.vector_store.delete(collection, where=eq("doc_id", doc.doc_id))
            # 换了集合时，旧向量在收尾阶段才删。失败时按快照放回旧集合。
            if (
                prior is not None
                and snapshot
                and prior.collection != collection
            ):
                self._restore_vectors(prior.collection, prior.doc_id, snapshot)

        if wrote_sqlite:
            if prior is not None:
                self.doc_store.upsert(prior)
            else:
                self.doc_store.delete(doc.doc_id)

        if self.governance is None or not wrote_meta:
            return
        if prior_meta is not None:
            self.governance.upsert_meta(prior_meta)
        elif prior is not None:
            self.governance.upsert_meta(
                DocMeta(
                    doc_id=prior.doc_id,
                    content_hash=prior.content_hash,
                    scope=prior.scope,
                    status=prior.status,
                )
            )
        else:
            self.governance.mark_deleted(doc.doc_id)

    def _safe_finish(self, journal_id: int | None) -> None:
        if journal_id is None:
            return
        try:
            self.journal.finish(journal_id)
        except Exception:
            logger.error(
                "修复日志 %s 未能关闭，请运行 kb repair", journal_id, exc_info=True
            )

    def _finalize_delete(self, doc_id: str) -> None:
        if self.governance is not None:
            self.governance.mark_deleted(doc_id)
            self.governance.emit_event(doc_id, "delete")
            self.governance.increment_manifest_docs(
                self.settings.embedding_manifest, -1
            )
        media_dir = self.settings.media_root / doc_id
        if media_dir.exists():
            shutil.rmtree(media_dir, ignore_errors=True)

    def _compensate_delete(
        self,
        doc_id: str,
        collection: str,
        snapshot: list[StoredVector],
        journal_id: int,
        exc: Exception,
    ) -> None:
        """删除中途失败：文档还在就把向量放回去；文档行已经没了就不要把向量写回。"""
        try:
            still_there = self.doc_store.get(doc_id) is not None
        except Exception:
            still_there = True
        try:
            if still_there:
                self._restore_vectors(collection, doc_id, snapshot)
                logger.warning("删除文档 %s 失败，已恢复向量: %s", doc_id, exc)
            else:
                self.vector_store.delete(collection, where=eq("doc_id", doc_id))
                self._finalize_delete(doc_id)
                logger.warning(
                    "删除文档 %s 的收尾失败，文档行已不在: %s", doc_id, exc
                )
            self._safe_finish(journal_id)
        except Exception as rollback_exc:
            logger.error(
                "删除文档 %s 失败且无法恢复向量，已保留修复日志: %s",
                doc_id,
                rollback_exc,
            )
            raise IngestError(
                f"删除文档 {doc_id} 失败且无法恢复向量，请运行 kb repair: {rollback_exc}"
            ) from exc

    # ---- 管理 ----
    def delete_document(self, doc_id: str, collection: str | None = None) -> bool:
        doc = self.doc_store.get(doc_id)
        if not doc:
            return False
        coll = collection or doc.collection
        snapshot = self._snapshot_vectors(coll, doc_id)
        journal_id = self.journal.begin(
            "delete",
            doc_id,
            coll,
            {"snapshot": [dump_vector(row) for row in snapshot]},
        )
        try:
            # 标记语义放在调用前：删除抛错时也尝试按快照恢复。
            self.vector_store.delete(coll, where=eq("doc_id", doc_id))
            self.doc_store.delete(doc_id)
            self._finalize_delete(doc_id)
        except Exception as exc:
            self._compensate_delete(doc_id, coll, snapshot, journal_id, exc)
            raise
        self._safe_finish(journal_id)
        return True

    def _quarantine(self, doc_id: str, replaced_by: str) -> None:
        """删不掉的旧文档先从默认检索里拿掉，避免新旧正文一起被搜到。

        文档行和治理元数据都要标成 hidden。检索先看文档行，所以治理写入失败时
        只要文档行隐藏成功，默认 search 就不再返回旧正文。两处都失败则抛错。
        """
        doc = self.doc_store.get(doc_id)
        if doc is None:
            return
        if doc.status != "hidden":
            doc.status = "hidden"
            try:
                self.doc_store.upsert(doc)
            except Exception:
                logger.warning("无法把待清理文档 %s 标为隐藏", doc_id, exc_info=True)
        stored = self.doc_store.get(doc_id)
        row_hidden = stored is not None and stored.is_hidden

        meta_hidden = False
        if self.governance is not None:
            try:
                meta = self.governance.get_meta(doc_id) or DocMeta(doc_id=doc_id)
                meta.status = "hidden"
                self.governance.upsert_meta(meta)
            except Exception:
                logger.warning("无法把待清理文档 %s 的治理状态标为隐藏", doc_id, exc_info=True)
            else:
                try:
                    self.governance.emit_event(
                        doc_id, "pending_cleanup", {"replaced_by": replaced_by}
                    )
                    self.governance.record_audit(
                        "document.pending_cleanup",
                        doc_id,
                        detail={"replaced_by": replaced_by},
                    )
                except Exception:
                    logger.warning("无法记录文档 %s 的待清理事件", doc_id, exc_info=True)
            try:
                saved = self.governance.get_meta(doc_id)
            except Exception:
                saved = None
            meta_hidden = saved is not None and saved.status == "hidden"

        if row_hidden or meta_hidden:
            return
        raise IngestError(f"未能隐藏待清理文档 {doc_id}")

    def _delete_superseded(self, old_id: str, collection: str, new_id: str) -> None:
        """新文档已经提交。删旧失败时重试一次，仍失败则标记待清理并报错。"""
        last: Exception | None = None
        for _ in range(2):
            try:
                self.delete_document(old_id, collection)
                return
            except Exception as exc:
                last = exc
        self.journal.begin(
            "cleanup",
            old_id,
            collection,
            {"replaced_by": new_id},
        )
        try:
            self._quarantine(old_id, new_id)
        except Exception as exc:
            logger.error(
                "新文档 %s 已提交，旧文档 %s 删除失败且未能隐藏", new_id, old_id
            )
            raise IngestError(
                f"新文档 {new_id} 已提交，旧文档 {old_id} 删除失败，"
                f"且未能从默认检索隐藏。已标记待清理。请运行 kb repair。原因: {exc}"
            ) from exc
        logger.error(
            "新文档 %s 已提交，旧文档 %s 删除失败，已标记待清理", new_id, old_id
        )
        raise IngestError(
            f"新文档 {new_id} 已提交，旧文档 {old_id} 删除失败，已标记待清理。"
            f"请运行 kb repair。原因: {last}"
        ) from last

    def update_document(self, doc_id: str, new_content: str, **kwargs) -> str | None:
        """用新内容替换文档。先入库，成功后再删除被换掉的旧 doc_id。

        未显式重新绑定路径时，新正文是 inline，不继承旧的源文件路径；
        内容变化会得到新 doc_id，此时才删除旧文档。同一路径原地替换时
        doc_id 不变，由入库路径按该 id 换向量。清洗、嵌入或向量写入失败
        时不删除，旧文档仍可读取、仍可检索。删旧失败不会静默留下可检索的双份。
        """
        doc = self.doc_store.get(doc_id)
        if not doc:
            raise NotFoundError(f"文档不存在: {doc_id}")
        new_id = self.ingest_text(
            content=new_content,
            title=kwargs.get("title", doc.title),
            source_path=kwargs.get("source_path", ""),
            doc_type=kwargs.get("doc_type", doc.doc_type),
            collection=kwargs.get("collection", doc.collection),
            metadata=kwargs.get("metadata", doc.metadata),
            scope=kwargs.get("scope", doc.scope),
        )
        if not new_id or new_id == doc_id:
            # 内容未变，或已按同一 doc_id 原地替换。不要再 delete。
            return doc_id
        self._delete_superseded(doc_id, doc.collection, new_id)
        return new_id

    def list_documents(self, collection: str | None = None) -> list[Document]:
        return self.doc_store.list(collection)

    def get_document(self, doc_id: str) -> Document | None:
        return self.doc_store.get(doc_id)

    def stats(self) -> dict[str, Any]:
        s = self.doc_store.stats()
        s["collections"] = self.vector_store.collections()
        s["chunks_by_collection"] = {
            c: self.vector_store.count(c) for c in s["collections"]
        }
        return s

    def close(self) -> None:
        self.journal.close()

    # ---- 修复 ----
    def repair(self) -> dict[str, Any]:
        """重放未完成的写入日志，并清掉没有父文档的向量。

        可重复执行。活文档的哈希如果已经既不是日志里的旧版、也不是那次没写完的
        新版，这条日志视为被后续写入取代，只删除日志、不恢复快照。
        没有快照、又已经没有向量的文档只记在 ``vectorless`` 里，调用方据此告警。
        """
        report: dict[str, Any] = {
            "restored": [],
            "rolled_back": [],
            "superseded": [],
            "orphans_removed": 0,
            "cleanups": [],
            "vectorless": [],
            "errors": [],
        }
        for entry in self.journal.pending():
            try:
                if entry.op == "commit":
                    self._repair_commit(entry, report)
                elif entry.op == "delete":
                    self._repair_delete(entry, report)
                elif entry.op == "cleanup":
                    self._repair_cleanup(entry, report)
                else:
                    report["errors"].append(
                        {"id": entry.id, "error": f"未知日志: {entry.op}"}
                    )
            except Exception as exc:
                logger.exception("修复日志 %s 失败", entry.id)
                report["errors"].append({"id": entry.id, "error": str(exc)})
        report["orphans_removed"] += self._gc_orphan_vectors()
        self._note_vectorless(report)
        if self.governance is not None and self._repair_dirty(report):
            try:
                self.governance.record_audit("repair", detail=report)
            except Exception:
                logger.warning("修复结果未能写入审计日志", exc_info=True)
        return report

    @staticmethod
    def _repair_dirty(report: dict[str, Any]) -> bool:
        return bool(
            report["restored"]
            or report["rolled_back"]
            or report["superseded"]
            or report["orphans_removed"]
            or report["cleanups"]
            or report["vectorless"]
            or report["errors"]
        )

    def _ids_present(self, collection: str, ids: list[str]) -> bool:
        if not ids:
            return False
        rows = self.vector_store.get(collection, ids=ids)
        return {row.id for row in rows} >= set(ids)

    def _repair_commit(self, entry, report: dict[str, Any]) -> None:
        payload = entry.payload
        new_doc = load_document(payload["new_doc"])
        prior_raw = payload.get("prior_doc")
        prior_doc = load_document(prior_raw) if isinstance(prior_raw, dict) else None
        prior_vectors = [
            load_vector(item)
            for item in (payload.get("prior_vectors") or [])
            if isinstance(item, dict)
        ]
        new_ids = [str(item) for item in (payload.get("new_ids") or [])]
        same_collection = bool(payload.get("same_collection"))
        current = self.doc_store.get(new_doc.doc_id)
        sqlite_ok = (
            current is not None and current.content_hash == new_doc.content_hash
        )
        vectors_ok = self._ids_present(new_doc.collection, new_ids)
        if sqlite_ok and vectors_ok:
            if prior_doc is not None and prior_doc.collection != new_doc.collection:
                self.vector_store.delete(
                    prior_doc.collection, where=eq("doc_id", new_doc.doc_id)
                )
            self.journal.finish(entry.id)
            return

        # 崩溃后如果又成功写过一版，活文档的哈希既不是日志里的旧版，也不是
        # 这次没写完的新版。这时快照已经过期，只能丢掉日志，不能把旧向量盖回去。
        if self._commit_superseded(current, prior_doc, new_doc):
            logger.warning(
                "跳过已被后续写入取代的修复日志 %s（doc_id=%s）",
                entry.id,
                new_doc.doc_id,
            )
            report["superseded"].append(new_doc.doc_id)
            self.journal.finish(entry.id)
            return

        if same_collection:
            self._restore_vectors(new_doc.collection, new_doc.doc_id, prior_vectors)
            if prior_doc is not None and prior_vectors:
                report["restored"].append(new_doc.doc_id)
        else:
            self.vector_store.delete(
                new_doc.collection, where=eq("doc_id", new_doc.doc_id)
            )
            if prior_doc is not None and prior_vectors:
                existing = self.vector_store.get(
                    prior_doc.collection, where=eq("doc_id", prior_doc.doc_id)
                )
                if len(existing) < len(prior_vectors):
                    self._restore_vectors(
                        prior_doc.collection, prior_doc.doc_id, prior_vectors
                    )
                    report["restored"].append(prior_doc.doc_id)
        if sqlite_ok:
            if prior_doc is not None:
                self.doc_store.upsert(prior_doc)
            else:
                self.doc_store.delete(new_doc.doc_id)
        report["rolled_back"].append(new_doc.doc_id)
        self.journal.finish(entry.id)

    @staticmethod
    def _commit_superseded(
        current: Document | None,
        prior_doc: Document | None,
        new_doc: Document,
    ) -> bool:
        """活文档已经不是这条日志的旧版或未完成新版。"""
        if current is None:
            # 有旧文档的日志不能把已经删掉的行复活；全新文档还要清掉半截向量。
            return prior_doc is not None
        if current.content_hash == new_doc.content_hash:
            return False
        if prior_doc is not None and current.content_hash == prior_doc.content_hash:
            return False
        return True

    def _repair_delete(self, entry, report: dict[str, Any]) -> None:
        doc_id = entry.doc_id
        collection = entry.collection
        snapshot = [
            load_vector(item)
            for item in (entry.payload.get("snapshot") or [])
            if isinstance(item, dict)
        ]
        doc = self.doc_store.get(doc_id)
        if doc is None:
            self.vector_store.delete(collection, where=eq("doc_id", doc_id))
            if self.governance is not None:
                self.governance.mark_deleted(doc_id)
            self.journal.finish(entry.id)
            return
        if not snapshot:
            self.journal.finish(entry.id)
            return
        rows = self.vector_store.get(collection, where=eq("doc_id", doc_id))
        have = {row.id for row in rows}
        need = {item.id for item in snapshot}
        if need <= have:
            self.journal.finish(entry.id)
            return
        self._restore_vectors(collection, doc_id, snapshot)
        report["restored"].append(doc_id)
        self.journal.finish(entry.id)

    def _repair_cleanup(self, entry, report: dict[str, Any]) -> None:
        doc_id = entry.doc_id
        collection = entry.collection
        if self.doc_store.get(doc_id) is not None:
            self.delete_document(doc_id, collection)
        else:
            self.vector_store.delete(collection, where=eq("doc_id", doc_id))
        self.journal.finish(entry.id)
        report["cleanups"].append(doc_id)

    def _pending_doc_ids(self) -> set[str]:
        return {entry.doc_id for entry in self.journal.pending()}

    def _gc_orphan_vectors(self) -> int:
        pending = self._pending_doc_ids()
        removed = 0
        for collection in self.vector_store.collections():
            grouped: dict[str, list[str]] = {}
            for row in self.vector_store.get(collection):
                doc_id = str((row.metadata or {}).get("doc_id") or "")
                grouped.setdefault(doc_id, []).append(row.id)
            for doc_id, ids in grouped.items():
                if not ids or doc_id in pending:
                    continue
                if doc_id and self.doc_store.get(doc_id) is not None:
                    continue
                self.vector_store.delete(collection, ids=ids)
                removed += len(ids)
                logger.warning(
                    "清理无父文档向量 %s 条（doc_id=%s, collection=%s）",
                    len(ids),
                    doc_id or "<empty>",
                    collection,
                )
        return removed

    def _note_vectorless(self, report: dict[str, Any]) -> None:
        pending = self._pending_doc_ids()
        seen = set(report["vectorless"])
        for doc in self.doc_store.list():
            if doc.doc_id in pending or doc.doc_id in seen:
                continue
            rows = self.vector_store.get(
                doc.collection, where=eq("doc_id", doc.doc_id)
            )
            if rows:
                continue
            report["vectorless"].append(doc.doc_id)
            seen.add(doc.doc_id)
            logger.warning(
                "文档 %s 仍在库中但没有向量，且没有可恢复快照", doc.doc_id
            )
