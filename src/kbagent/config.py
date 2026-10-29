"""全局配置（pydantic-settings，env 前缀 KB_，支持 .env）。"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KB_", env_file=".env", extra="ignore"
    )

    @field_validator("storage_dir", "media_dir", mode="before")
    @classmethod
    def _expand_user(cls, v):
        """支持 ~ 开头的路径（.env 中 KB_STORAGE_DIR=~/.kbagent）。"""
        if isinstance(v, str) and v.startswith("~"):
            return Path(v).expanduser()
        return v

    @field_validator("vector_metric")
    @classmethod
    def _check_metric(cls, v: str) -> str:
        metric = v.strip().lower()
        if metric not in {"l2", "cosine", "ip"}:
            raise ValueError("KB_VECTOR_METRIC 必须是 l2 / cosine / ip")
        return metric

    # ---- 存储 ----
    storage_dir: Path = Path.home() / ".kbagent"
    default_collection: str = "personal"
    media_dir: Path | None = None

    # ---- 存储后端（见 ADR 0001）----
    vector_store: str = "chroma"      # chroma | （后续）qdrant / lancedb
    doc_store: str = "sqlite"         # sqlite | （后续）postgres
    vector_metric: str = "l2"         # l2 | cosine | ip，需与嵌入模型一致
    embedding_manifest_id: str = ""   # 为空时由 embedding_model + chunker_version 派生
    chunker_version: str = "c1"       # 切块算法版本，参与 manifest

    # ---- 网络 ----
    ignore_http_proxy: bool = True  # 忽略失效代理，直连 LLM/HF（需要代理时设 false）

    # ---- 嵌入（本地 CPU）----
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_query_instruction: str = "为这个句子生成表示以用于检索相关文章："
    embedding_batch_size: int = 32

    # ---- 切块 ----
    chunk_size: int = 800
    chunk_overlap: int = 100

    # ---- 重排 ----
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-base"
    rerank_candidates: int = 20
    top_k: int = 5

    # ---- 意图识别 / 查询改写 ----
    intent_enabled: bool = True
    query_rewrite_enabled: bool = True
    max_parent_chars: int = 4000
    parent_mode: str = "full"  # full | section

    # ---- LLM（OpenAI 兼容）----
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1024
    llm_timeout: float = 60.0

    # ---- 多模态 ----
    multimodal_enabled: bool = True
    image_caption_model: str = ""
    vision_llm_model: str = ""
    vision_llm_base_url: str = ""
    ocr_enabled: bool = False
    vision_max_images: int = 3

    # ---- 评估 ----
    eval_judge_model: str = ""

    # ---- 服务 ----
    api_host: str = "127.0.0.1"
    api_port: int = 8710
    api_key: str = ""
    ui_port: int = 7860

    # ---- 派生路径 ----
    @property
    def chroma_dir(self) -> Path:
        return self.storage_dir / "chroma"

    @property
    def documents_db(self) -> Path:
        return self.storage_dir / "documents.sqlite3"

    @property
    def study_db(self) -> Path:
        return self.storage_dir / "study.sqlite3"

    @property
    def embedding_manifest(self) -> str:
        """向量版本标识：显式配置优先，否则由模型 + 切块版本派生。"""
        if self.embedding_manifest_id:
            return self.embedding_manifest_id
        slug = re.sub(r"[^a-z0-9]+", "-", self.embedding_model.lower()).strip("-")
        return f"{slug or 'embedding'}@{self.chunker_version}"

    @property
    def media_root(self) -> Path:
        return self.media_dir or (self.storage_dir / "media")

    @property
    def hf_cache_dir(self) -> Path:
        """KB 自管的 HuggingFace 模型缓存（不依赖 ~/.cache，避免只读/权限问题）。"""
        return self.storage_dir / "hf-cache"

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.hf_cache_dir.mkdir(parents=True, exist_ok=True)
