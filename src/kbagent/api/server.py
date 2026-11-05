"""FastAPI REST 服务（供外部 agent 项目跨语言集成；可选 X-API-Key 鉴权）。"""

from __future__ import annotations

import secrets
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from ..config import Settings
from ..errors import IngestError, KBError, NotFoundError
from ..ingestion.loaders import load_file
from ..kb import KnowledgeBase
from ..models import SOURCE_BOUND, MediaRef
from .schemas import (
    AskResponse,
    DocumentOut,
    FlagUpdate,
    HitOut,
    IngestResponse,
    IngestTextRequest,
    MediaOut,
    QueryRequest,
    SearchResponse,
    SourceOut,
    StatsOut,
)

def _key_matches(supplied: str, expected: str) -> bool:
    """比较 API Key。

    统一按 UTF-8 字节做 ``secrets.compare_digest``。3.10–3.12 上对不等长
    ``str`` 调用 ``hmac.compare_digest`` 会 ``ValueError``，非 ASCII 会
    ``TypeError``；未捕获时错误 Key 会变成 500。字节比较在这些版本上对
    不等长输入返回 False，不会把鉴权失败变成异常。
    """
    if not isinstance(supplied, str) or not isinstance(expected, str):
        return False
    if not supplied or not expected:
        return False
    try:
        return secrets.compare_digest(
            supplied.encode("utf-8"),
            expected.encode("utf-8"),
        )
    except (TypeError, ValueError):
        return False


def _media_out(m: MediaRef | None) -> MediaOut | None:
    if m is None:
        return None
    return MediaOut(
        media_type=m.media_type,
        image_path=m.image_path,
        page=m.page,
        caption=m.caption,
    )


def _hit_out(h) -> HitOut:
    return HitOut(
        chunk_id=h.chunk_id,
        doc_id=h.doc_id,
        title=h.title,
        source_path=h.source_path,
        section=h.section,
        score=round(h.score, 4),
        snippet=h.text[:300],
        media=_media_out(h.media),
    )


def _doc_out(doc, include_text: bool = False) -> DocumentOut:
    # 绑定原件的正文不在库里，响应也不附带整篇正文。
    include_body = include_text and doc.source_kind != SOURCE_BOUND
    return DocumentOut(
        doc_id=doc.doc_id,
        title=doc.title,
        source_path=doc.source_path,
        doc_type=doc.doc_type,
        collection=doc.collection,
        metadata=doc.metadata,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        chunk_count=doc.chunk_count,
        media=[_media_out(m) for m in doc.media if m is not None],
        scope=doc.scope,
        status=doc.status,
        source_kind=doc.source_kind,
        summary=doc.summary,
        full_text=doc.full_text if include_body else None,
    )


def create_app(kb: KnowledgeBase | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    kb = kb or KnowledgeBase(settings)

    app = FastAPI(title="kbagent REST API", version="0.1.0")

    def _keys() -> tuple[str, str]:
        # 旧 api_key 视为 admin；未配置 admin 时 admin 端点不可用（除非完全没有 Key）
        admin = settings.api_admin_key or settings.api_key
        read = settings.api_read_key or admin
        return read, admin

    def _check_key(request: Request, admin_required: bool) -> None:
        read_key, admin_key = _keys()
        if not read_key and not admin_key:
            return
        supplied = request.headers.get("X-API-Key", "")
        candidates = (admin_key,) if admin_required else (read_key, admin_key)
        if not any(_key_matches(supplied, k) for k in candidates):
            raise HTTPException(status_code=401, detail="无效的 API Key")

    def auth_read(request: Request) -> None:
        _check_key(request, admin_required=False)

    def auth_admin(request: Request) -> None:
        _check_key(request, admin_required=True)

    # ---- 检索 ----
    @app.post("/v1/search", response_model=SearchResponse, dependencies=[Depends(auth_read)])
    def search(req: QueryRequest):
        hits = kb.search(
            req.query,
            top_k=req.top_k,
            where=req.where,
            collection=req.collection,
        )
        return SearchResponse(hits=[_hit_out(h) for h in hits])

    @app.post("/v1/ask", response_model=AskResponse, dependencies=[Depends(auth_read)])
    def ask(req: QueryRequest):
        ans = kb.ask(
            req.query,
            top_k=req.top_k,
            where=req.where,
            collection=req.collection,
        )
        return AskResponse(
            text=ans.text,
            sources=[
                SourceOut(
                    doc_id=s.doc_id,
                    title=s.title,
                    source_path=s.source_path,
                    score=round(s.score, 4),
                    snippet=s.snippet,
                    section=s.section,
                    media=_media_out(s.media),
                )
                for s in ans.sources
            ],
            meta=ans.meta,
        )

    # ---- 入库（JSON 文本）----
    @app.post("/v1/ingest", response_model=IngestResponse, dependencies=[Depends(auth_admin)])
    def ingest(req: IngestTextRequest):
        if not req.content.strip():
            raise HTTPException(status_code=422, detail="content 不能为空")
        try:
            doc_id = kb.ingest_text(
                req.content,
                title=req.title,
                doc_type=req.doc_type,
                collection=req.collection,
                metadata=req.metadata,
                scope=req.scope,
            )
        except IngestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return IngestResponse(doc_id=doc_id, status="added" if doc_id else "skipped")

    # ---- 入库（文件上传，multipart）----
    @app.post("/v1/ingest/file", response_model=IngestResponse, dependencies=[Depends(auth_admin)])
    async def ingest_file(
        file: UploadFile = File(...),
        collection: str | None = None,
        scope: str | None = None,
    ):
        suffix = Path(file.filename or "").suffix or ".txt"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        try:
            # 上传文件没有可长期绑定的外部路径，临时文件删掉后按 inline 保存正文。
            try:
                raws = load_file(tmp_path)
            except IngestError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if not raws:
                raise HTTPException(status_code=422, detail="没有可入库的内容")
            raw = raws[0]
            meta = dict(raw.get("metadata") or {})
            if file.filename:
                meta["upload_name"] = file.filename
            title = Path(file.filename).stem if file.filename else raw.get("title")
            try:
                doc_id = kb.ingest_text(
                    raw.get("content") or "",
                    title=title,
                    source_path="",
                    doc_type=raw.get("doc_type") or "text",
                    collection=collection,
                    metadata=meta,
                    scope=scope,
                )
            except IngestError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            tmp_path.unlink(missing_ok=True)
        if doc_id:
            return IngestResponse(doc_id=doc_id, status="added", path=file.filename)
        return IngestResponse(doc_id=None, status="skipped", path=file.filename)

    # ---- 文档管理 ----
    @app.get("/v1/documents", response_model=list[DocumentOut], dependencies=[Depends(auth_read)])
    def list_documents(collection: str | None = None):
        return [_doc_out(d) for d in kb.list_documents(collection)]

    @app.get("/v1/documents/{doc_id}", response_model=DocumentOut, dependencies=[Depends(auth_read)])
    def get_document(doc_id: str):
        doc = kb.get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        return _doc_out(doc, include_text=True)

    @app.delete("/v1/documents/{doc_id}", dependencies=[Depends(auth_admin)])
    def delete_document(doc_id: str):
        ok = kb.delete_document(doc_id, actor="api")
        if not ok:
            raise HTTPException(status_code=404, detail="文档不存在")
        return JSONResponse({"deleted": doc_id})

    @app.post("/v1/documents/{doc_id}/hide", dependencies=[Depends(auth_admin)])
    def hide_document(doc_id: str):
        if not kb.hide_document(doc_id, actor="api"):
            raise HTTPException(status_code=404, detail="文档不存在")
        return {"hidden": doc_id}

    @app.post("/v1/documents/{doc_id}/unhide", dependencies=[Depends(auth_admin)])
    def unhide_document(doc_id: str):
        if not kb.unhide_document(doc_id, actor="api"):
            raise HTTPException(status_code=404, detail="文档不存在")
        return {"unhidden": doc_id}

    @app.post("/v1/documents/{doc_id}/refresh", response_model=IngestResponse, dependencies=[Depends(auth_admin)])
    def refresh_document(doc_id: str, req: IngestTextRequest):
        """更新文档内容。新内容入库成功后再移除被替换的旧文档。"""
        try:
            new_id = kb.update_document(doc_id, req.content, title=req.title)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return IngestResponse(doc_id=new_id, status="added")

    # ---- 统计 / 健康 ----
    @app.get("/v1/stats", response_model=StatsOut, dependencies=[Depends(auth_read)])
    def stats():
        s = kb.stats()
        return StatsOut(**s)

    # ---- 运行时开关 ----
    @app.get("/v1/settings/flags", dependencies=[Depends(auth_read)])
    def get_flags():
        return {"flags": kb.get_flags()}

    @app.put("/v1/settings/flags/{name}", dependencies=[Depends(auth_admin)])
    def set_flag(name: str, req: FlagUpdate):
        try:
            enabled = kb.set_flag(name, req.enabled, actor="api")
        except KBError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": name, "enabled": enabled}

    @app.get("/v1/settings/embedding", dependencies=[Depends(auth_read)])
    def get_embedding():
        return {"active": kb.get_embedding_manifest()}

    @app.get("/v1/answer-log", dependencies=[Depends(auth_admin)])
    def get_answer_log(after_id: int = 0, limit: int = 100):
        return {"answers": kb.list_answers(after_id, min(max(limit, 1), 500))}

    @app.get("/v1/audit-log", dependencies=[Depends(auth_admin)])
    def get_audit_log(after_id: int = 0, limit: int = 100):
        return {"entries": kb.list_audit(after_id, min(max(limit, 1), 500))}

    @app.post("/v1/governance/backfill", dependencies=[Depends(auth_admin)])
    def backfill_governance():
        return {"created": kb.backfill_governance(actor="api")}

    @app.post("/v1/governance/migrate-parents", dependencies=[Depends(auth_admin)])
    def migrate_parents():
        """迁出已入库父全文（admin）。源文件缺失的记录标为 orphaned_copy。"""
        return kb.migrate_parent_bodies(actor="api")

    @app.get("/v1/health")
    def health():
        return {"status": "ok"}

    return app
