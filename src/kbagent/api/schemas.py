"""REST API 请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str
    top_k: int | None = Field(default=None, ge=1, le=50)
    collection: str | None = None
    where: dict[str, Any] | None = None


class IngestTextRequest(BaseModel):
    content: str
    title: str | None = None
    doc_type: str = "text"
    collection: str | None = None
    metadata: dict[str, Any] | None = None
    scope: str | None = None


class MediaOut(BaseModel):
    media_type: str = "image"
    image_path: str = ""
    page: int | None = None
    caption: str = ""


class HitOut(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    source_path: str = ""
    section: str = ""
    score: float
    snippet: str = ""
    media: MediaOut | None = None


class SourceOut(BaseModel):
    doc_id: str
    title: str = ""
    source_path: str = ""
    score: float = 0.0
    snippet: str = ""
    section: str = ""
    media: MediaOut | None = None


class AskResponse(BaseModel):
    text: str
    sources: list[SourceOut] = []
    meta: dict[str, Any] = {}


class SearchResponse(BaseModel):
    hits: list[HitOut] = []


class DocumentOut(BaseModel):
    doc_id: str
    title: str
    source_path: str = ""
    doc_type: str = ""
    collection: str = ""
    metadata: dict[str, Any] = {}
    created_at: str = ""
    updated_at: str = ""
    chunk_count: int = 0
    media: list[MediaOut] = []
    scope: str = "default"
    status: str = "active"
    full_text: str | None = None


class StatsOut(BaseModel):
    documents: int
    chunks: int
    by_collection: dict[str, int] = {}
    collections: list[str] = []
    chunks_by_collection: dict[str, int] = {}


class IngestResponse(BaseModel):
    doc_id: str | None = None
    status: str = "added"  # added | skipped
    path: str | None = None


class FlagUpdate(BaseModel):
    enabled: bool
