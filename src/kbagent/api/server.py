"""FastAPI REST 服务（供外部 agent 项目跨语言集成；可选 X-API-Key 鉴权）。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from ..config import Settings
from ..errors import IngestError, KBError, NotFoundError
from ..kb import KnowledgeBase
from ..models import MediaRef
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
        full_text=doc.full_text if include_text else None,
    )


def create_app(kb: KnowledgeBase | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    kb = kb or KnowledgeBase(settings)

    app = FastAPI(title="kbagent REST API", version="0.1.0")

    def auth(request: Request) -> None:
        if settings.api_key:
            key = request.headers.get("X-API-Key", "")
            if key != settings.api_key:
                raise HTTPException(status_code=401, detail="无效的 API Key")

    # ---- 检索 ----
    @app.post("/v1/search", response_model=SearchResponse, dependencies=[Depends(auth)])
    def search(req: QueryRequest):
        hits = kb.search(
            req.query,
            top_k=req.top_k,
            where=req.where,
            collection=req.collection,
        )
        return SearchResponse(hits=[_hit_out(h) for h in hits])

    @app.post("/v1/ask", response_model=AskResponse, dependencies=[Depends(auth)])
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
    @app.post("/v1/ingest", response_model=IngestResponse, dependencies=[Depends(auth)])
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
            )
        except IngestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return IngestResponse(doc_id=doc_id, status="added" if doc_id else "skipped")

    # ---- 入库（文件上传，multipart）----
    @app.post("/v1/ingest/file", response_model=IngestResponse, dependencies=[Depends(auth)])
    async def ingest_file(
        file: UploadFile = File(...),
        collection: str | None = None,
    ):
        suffix = Path(file.filename or "").suffix or ".txt"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name
        try:
            report = kb.ingest(tmp_path, collection=collection)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        if report.added:
            return IngestResponse(doc_id=report.added[0], status="added", path=file.filename)
        if report.failed:
            raise HTTPException(status_code=422, detail=str(report.failed[0]["error"]))
        return IngestResponse(doc_id=None, status="skipped", path=file.filename)

    # ---- 文档管理 ----
    @app.get("/v1/documents", response_model=list[DocumentOut], dependencies=[Depends(auth)])
    def list_documents(collection: str | None = None):
        return [_doc_out(d) for d in kb.list_documents(collection)]

    @app.get("/v1/documents/{doc_id}", response_model=DocumentOut, dependencies=[Depends(auth)])
    def get_document(doc_id: str):
        doc = kb.get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        return _doc_out(doc, include_text=True)

    @app.delete("/v1/documents/{doc_id}", dependencies=[Depends(auth)])
    def delete_document(doc_id: str):
        ok = kb.delete_document(doc_id)
        if not ok:
            raise HTTPException(status_code=404, detail="文档不存在")
        return JSONResponse({"deleted": doc_id})

    @app.post("/v1/documents/{doc_id}/refresh", response_model=IngestResponse, dependencies=[Depends(auth)])
    def refresh_document(doc_id: str, req: IngestTextRequest):
        """更新文档内容（= 删除旧文档 + 重新入库，返回新 doc_id）。"""
        try:
            new_id = kb.update_document(doc_id, req.content, title=req.title)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return IngestResponse(doc_id=new_id, status="added")

    # ---- 统计 / 健康 ----
    @app.get("/v1/stats", response_model=StatsOut, dependencies=[Depends(auth)])
    def stats():
        s = kb.stats()
        return StatsOut(**s)

    # ---- 运行时开关 ----
    @app.get("/v1/settings/flags", dependencies=[Depends(auth)])
    def get_flags():
        return {"flags": kb.get_flags()}

    @app.put("/v1/settings/flags/{name}", dependencies=[Depends(auth)])
    def set_flag(name: str, req: FlagUpdate):
        try:
            enabled = kb.set_flag(name, req.enabled)
        except KBError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": name, "enabled": enabled}

    @app.get("/v1/health")
    def health():
        return {"status": "ok"}

    return app
