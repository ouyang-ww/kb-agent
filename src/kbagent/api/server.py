"""FastAPI REST 服务（供外部 agent 项目跨语言集成；可选 X-API-Key 鉴权）。"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from ..config import Settings
from ..errors import ConfigError, IngestError, KBError, NotFoundError
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

logger = logging.getLogger(__name__)

_LOOPBACK_NAMES = frozenset({"localhost", "localhost."})

OPEN_AUTH_WARNING = (
    "警告: 未配置 API Key，鉴权已关闭，仅适用于本机 {host} 开发。"
    "入库、删除、隐藏和开关接口对本机开放。"
    "不要把服务暴露到局域网或公网；对外请设置 KB_API_KEY，"
    "或 KB_API_READ_KEY 与 KB_API_ADMIN_KEY。"
)


def is_loopback_host(host: str) -> bool:
    """监听地址是否只对本机回环开放。"""
    name = (host or "").strip().lower()
    if name in _LOOPBACK_NAMES:
        return True
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    name = name.split("%", 1)[0].strip()
    if not name:
        return False
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def configured_api_keys(settings: Settings) -> tuple[str, str]:
    """(read_key, admin_key)。旧 api_key 视为 admin；read 缺省时沿用 admin。"""
    admin = settings.api_admin_key or settings.api_key
    read = settings.api_read_key or admin
    return read, admin


def api_auth_disabled(settings: Settings) -> bool:
    read_key, admin_key = configured_api_keys(settings)
    return not read_key and not admin_key


def ensure_api_bind_allowed(settings: Settings, host: str) -> None:
    """无 Key 时拒绝非回环监听。KB_API_REQUIRE_AUTH 时回环也拒绝。"""
    if not api_auth_disabled(settings):
        return
    if settings.api_require_auth:
        raise ConfigError(
            "已设置 KB_API_REQUIRE_AUTH，但没有配置 API Key，拒绝启动。"
            "请设置 KB_API_KEY，或 KB_API_READ_KEY 与 KB_API_ADMIN_KEY。"
        )
    if not is_loopback_host(host):
        raise ConfigError(
            f"拒绝在 {host} 上无 API Key 启动：该地址不是本机回环，"
            "入库、删除和开关接口会对网络开放。"
            "请设置 KB_API_KEY，或 KB_API_READ_KEY 与 KB_API_ADMIN_KEY。"
            "仅本机开发请使用 127.0.0.1 或 localhost。"
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


def refresh_error_response(doc_id: str, exc: IngestError) -> JSONResponse:
    """刷新失败时的 HTTP 体。输入问题用 422，写坏了需要 repair 的用 500，但都带明文原因。"""
    message = str(exc).strip() or "刷新文档失败"
    needs_repair = any(
        token in message for token in ("kb repair", "待清理", "无法恢复")
    )
    code = "repair_required" if needs_repair else "ingest_error"
    return JSONResponse(
        status_code=500 if needs_repair else 422,
        content={
            "error": code,
            "message": message,
            "doc_id": doc_id,
            "detail": message,
        },
    )


def public_source_path(path: str, expose: bool) -> str:
    """对外响应里的路径。默认去掉绝对路径；相对路径仍可返回。"""
    raw = path or ""
    if not raw or expose:
        return raw
    if Path(raw).is_absolute():
        return ""
    return raw


def _media_out(m: MediaRef | None, expose_paths: bool = False) -> MediaOut | None:
    if m is None:
        return None
    return MediaOut(
        media_type=m.media_type,
        image_path=public_source_path(m.image_path, expose_paths),
        page=m.page,
        caption=m.caption,
    )


def _hit_out(h, expose_paths: bool = False) -> HitOut:
    return HitOut(
        chunk_id=h.chunk_id,
        doc_id=h.doc_id,
        title=h.title,
        source_path=public_source_path(h.source_path, expose_paths),
        section=h.section,
        score=round(h.score, 4),
        snippet=h.text[:300],
        media=_media_out(h.media, expose_paths),
        note=getattr(h, "note", "") or "",
    )


def _doc_out(doc, include_text: bool = False, expose_paths: bool = False) -> DocumentOut:
    # 绑定原件的正文不在库里，响应也不附带整篇正文。
    include_body = include_text and doc.source_kind != SOURCE_BOUND
    return DocumentOut(
        doc_id=doc.doc_id,
        title=doc.title,
        source_path=public_source_path(doc.source_path, expose_paths),
        doc_type=doc.doc_type,
        collection=doc.collection,
        metadata=doc.metadata,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        chunk_count=doc.chunk_count,
        media=[_media_out(m, expose_paths) for m in doc.media if m is not None],
        scope=doc.scope,
        status=doc.status,
        source_kind=doc.source_kind,
        summary=doc.summary,
        full_text=doc.full_text if include_body else None,
    )


def create_app(
    kb: KnowledgeBase | None = None,
    settings: Settings | None = None,
    bind_host: str | None = None,
    *,
    warn_open_auth: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    host = (bind_host or "").strip() or settings.api_host
    # 在构造知识库之前拒绝，避免无 Key 对外监听时还去加载模型。
    ensure_api_bind_allowed(settings, host)
    if warn_open_auth and api_auth_disabled(settings):
        logger.warning(OPEN_AUTH_WARNING.format(host=host))
    kb = kb or KnowledgeBase(settings)

    app = FastAPI(title="kbagent REST API", version="0.1.0")
    upload_slots = asyncio.Semaphore(settings.max_concurrent_uploads)
    upload_chunk = 1024 * 1024

    def _expose_paths() -> bool:
        return bool(settings.expose_source_paths)

    def _upload_too_large() -> HTTPException:
        return HTTPException(
            status_code=413,
            detail=f"上传超过上限 {settings.max_upload_bytes} 字节",
        )

    async def _write_limited(src: UploadFile, dest) -> None:
        limit = settings.max_upload_bytes
        if src.size is not None and src.size > limit:
            raise _upload_too_large()
        total = 0
        while True:
            chunk = await src.read(upload_chunk)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise _upload_too_large()
            dest.write(chunk)

    def _keys() -> tuple[str, str]:
        # 旧 api_key 视为 admin；未配置 admin 时 admin 端点不可用（除非完全没有 Key）
        return configured_api_keys(settings)

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
            include_expired=req.include_expired,
            as_of=req.as_of,
        )
        return SearchResponse(hits=[_hit_out(h, _expose_paths()) for h in hits])

    @app.post("/v1/ask", response_model=AskResponse, dependencies=[Depends(auth_read)])
    def ask(req: QueryRequest):
        ans = kb.ask(
            req.query,
            top_k=req.top_k,
            where=req.where,
            collection=req.collection,
            include_expired=req.include_expired,
            as_of=req.as_of,
        )
        return AskResponse(
            text=ans.text,
            sources=[
                SourceOut(
                    doc_id=s.doc_id,
                    title=s.title,
                    source_path=public_source_path(s.source_path, _expose_paths()),
                    score=round(s.score, 4),
                    snippet=s.snippet,
                    section=s.section,
                    media=_media_out(s.media, _expose_paths()),
                    note=s.note,
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
        limit = settings.max_upload_bytes
        if file.size is not None and file.size > limit:
            raise _upload_too_large()
        suffix = Path(file.filename or "").suffix or ".txt"
        upload_dir = settings.storage_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            async with upload_slots:
                with tempfile.NamedTemporaryFile(
                    suffix=suffix, delete=False, dir=upload_dir
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    await _write_limited(file, tmp)
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
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        if doc_id:
            return IngestResponse(doc_id=doc_id, status="added", path=file.filename)
        return IngestResponse(doc_id=None, status="skipped", path=file.filename)

    # ---- 文档管理 ----
    @app.get("/v1/documents", response_model=list[DocumentOut], dependencies=[Depends(auth_read)])
    def list_documents(collection: str | None = None):
        return [
            _doc_out(d, expose_paths=_expose_paths())
            for d in kb.list_documents(collection)
        ]

    @app.get("/v1/documents/{doc_id}", response_model=DocumentOut, dependencies=[Depends(auth_read)])
    def get_document(doc_id: str, include_text: bool = False):
        """读取文档。默认不含全文；``include_text=true`` 才返回 inline 正文。"""
        doc = kb.get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        return _doc_out(
            doc, include_text=include_text, expose_paths=_expose_paths()
        )

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
        except IngestError as exc:
            # 库函数已经抛了 IngestError。不能再落到未捕获异常的
            # {"detail": "Internal Server Error"}。
            logger.warning("刷新文档 %s 失败: %s", doc_id, exc)
            return refresh_error_response(doc_id, exc)
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
        vision = kb.vision_status()
        # 文本库仍可用。请求了多模态但索引器没起来时，健康检查标成 degraded。
        status = "degraded" if vision.get("enabled") and vision.get("error") else "ok"
        return {"status": status, "vision": vision}

    return app
