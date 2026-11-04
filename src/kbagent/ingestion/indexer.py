"""索引器：编排入库链路 loaders → cleaner → chunker → 嵌入 → Chroma(子) + SQLite(父记录)。

绑定源文件时 SQLite 不保存整篇正文。

含内容哈希去重、update/delete 级联清理、统计与报告。
"""

from __future__ import annotations

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
from .loaders import content_hash, doc_id_for, load_file, scan_path
from .source_text import locate_chunks, summarize


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
        identity = source_path or f"{collection}:{title}:{h}"
        doc_id = doc_id_for(identity)

        duplicate = self.doc_store.get_by_hash(h)
        if duplicate and not force:
            return None  # 去重跳过
        prior = self.doc_store.get(doc_id)
        if prior and prior.content_hash == h and not force:
            return None

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

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
            self.delete_document(duplicate.doc_id, duplicate.collection)
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
        所以替换必须按 doc_id 清掉旧向量。清掉之后如果 add / SQLite / 治理失败，
        用快照把旧向量和旧父文档写回去。
        """
        collection = doc.collection
        replacing = prior is not None
        same_collection = replacing and prior.collection == collection
        snapshot: list[StoredVector] = []
        deleted_old = False
        wrote_new_vectors = False
        wrote_sqlite = False
        wrote_meta = False
        prior_meta = None
        if replacing and self.governance is not None:
            prior_meta = self.governance.get_meta(doc.doc_id)

        try:
            if same_collection:
                snapshot = self._snapshot_vectors(collection, doc.doc_id)
                if snapshot:
                    # 同 doc_id 替换：先删光该文档的向量，再写入新 chunk。
                    self.vector_store.delete(collection, where=eq("doc_id", doc.doc_id))
                    deleted_old = True
                self.vector_store.add(collection, ids, embeddings, texts, metas)
                wrote_new_vectors = True
            else:
                self.vector_store.add(collection, ids, embeddings, texts, metas)
                wrote_new_vectors = True

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

            if replacing and not same_collection:
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
                raise IngestError(
                    f"入库失败且无法恢复旧文档: {rollback_exc}"
                ) from exc
            raise

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

    # ---- 管理 ----
    def delete_document(self, doc_id: str, collection: str | None = None) -> bool:
        doc = self.doc_store.get(doc_id)
        if not doc:
            return False
        coll = collection or doc.collection
        self.vector_store.delete(coll, where=eq("doc_id", doc_id))
        self.doc_store.delete(doc_id)
        if self.governance is not None:
            self.governance.mark_deleted(doc_id)
            self.governance.emit_event(doc_id, "delete")
            self.governance.increment_manifest_docs(
                self.settings.embedding_manifest, -1
            )
        media_dir = self.settings.media_root / doc_id
        if media_dir.exists():
            shutil.rmtree(media_dir, ignore_errors=True)
        return True

    def update_document(self, doc_id: str, new_content: str, **kwargs) -> str | None:
        """用新内容替换文档。先入库，成功后再删除被换掉的旧 doc_id。

        未显式重新绑定路径时，新正文是 inline，不继承旧的源文件路径；
        内容变化会得到新 doc_id，此时才删除旧文档。同一路径原地替换时
        doc_id 不变，由入库路径按该 id 换向量。清洗、嵌入或向量写入失败
        时不删除，旧文档仍可读取、仍可检索。
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
        self.delete_document(doc_id, doc.collection)
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
