"""全局配置（pydantic-settings，env 前缀 KB_，支持 .env）。"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import field_validator, model_validator
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

    @field_validator("max_upload_bytes")
    @classmethod
    def _check_upload_limit(cls, v: int) -> int:
        if int(v) <= 0:
            raise ValueError("KB_MAX_UPLOAD_BYTES 必须大于 0")
        return int(v)

    @field_validator("max_concurrent_uploads")
    @classmethod
    def _check_upload_concurrency(cls, v: int) -> int:
        if int(v) <= 0:
            raise ValueError("KB_MAX_CONCURRENT_UPLOADS 必须大于 0")
        return int(v)

    @field_validator("answer_tau_low", "answer_tau_high")
    @classmethod
    def _check_tau(cls, v: float) -> float:
        number = float(v)
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("可答性阈值必须是有限数")
        return number

    @field_validator("answer_rerank_tau_low", "answer_rerank_tau_high")
    @classmethod
    def _check_rerank_tau(cls, v: float) -> float:
        number = float(v)
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("重排可答性阈值必须是有限数")
        return number

    @model_validator(mode="after")
    def _tau_order(self):
        if self.answer_tau_low > self.answer_tau_high:
            raise ValueError("KB_ANSWER_TAU_LOW 不能高于 KB_ANSWER_TAU_HIGH")
        if self.answer_rerank_tau_low > self.answer_rerank_tau_high:
            raise ValueError(
                "KB_ANSWER_RERANK_TAU_LOW 不能高于 KB_ANSWER_RERANK_TAU_HIGH"
            )
        if not 0.0 <= self.answer_strict_max_fail_rate <= 1.0:
            raise ValueError("KB_ANSWER_STRICT_MAX_FAIL_RATE 必须在 0 到 1 之间")
        for name, value in (
            ("KB_NEAR_DUP_COSINE", self.near_dup_cosine),
            ("KB_RELATION_NEIGHBOR_COSINE", self.relation_neighbor_cosine),
        ):
            if not 0.0 < float(value) <= 1.0:
                raise ValueError(f"{name} 必须在 (0, 1] 内")
        if self.near_dup_hamming < 0 or self.near_dup_cosine_hamming < 0:
            raise ValueError("KB_NEAR_DUP_HAMMING 和 KB_NEAR_DUP_COSINE_HAMMING 不能为负")
        if not 0.0 <= self.mmr_lambda <= 1.0:
            raise ValueError("KB_MMR_LAMBDA 必须在 0 到 1 之间")
        return self

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

    # ---- 闸 1：可答性 ----
    # 2026-11-10，语料 examples/sample_notes 四篇，嵌入 BAAI/bge-small-zh-v1.5，
    # 向量分是 L2 经 1/(1+d)。库外 32 题 top1 最高 0.4959，基线五题最低 0.5440。
    # τ_low=0.52 卡在这两端之间。τ_high=0.54，基线最低分仍算正常回答。
    # 重排是 BAAI/bge-reranker-base。sentence-transformers 的 predict 已是 Sigmoid
    # 概率：库外最高 0.3319，基线最低 0.9406。τ 用 0.50 / 0.90，不再对概率做第二次 sigmoid。
    # 换嵌入、度量、重排模型或语料后必须重标，见 docs/status.md。
    answerability_enabled: bool = True
    answer_tau_low: float = 0.52
    answer_tau_high: float = 0.54
    answer_rerank_tau_low: float = 0.50
    answer_rerank_tau_high: float = 0.90

    # ---- 闸 3：声明校验。默认关。没有 LLM key 时跳过，不改闸 1 / 闸 2 ----
    answer_strict: bool = False
    # 失败条数 / 陈述条数 严格大于该值，或一条都留不下来时，整题改成固定拒答。
    answer_strict_max_fail_rate: float = 0.5

    # ---- 近似去重 / MMR / 邻居关系 ----
    # 这些是文档化默认值，不是这次语料上重标的结果。余弦针对 L2 归一化向量
    # （默认嵌入 BAAI/bge-small-zh-v1.5）。换模型、度量或语料后必须重标。
    # 见 governance/near_dup.py 的 RETUNE_NOTE。
    near_dup_cosine: float = 0.95
    near_dup_hamming: int = 3
    # 余弦已经 >= near_dup_cosine 时，SimHash 距离还要 <= 这个值才当成副本。
    # 默认与 KB_NEAR_DUP_HAMMING 相同：只改结论、汉明距离更大的文档不会被吞掉。
    near_dup_cosine_hamming: int = 3
    mmr_lambda: float = 0.7
    relation_neighbor_cosine: float = 0.80

    # ---- 意图识别 / 查询改写 ----
    intent_enabled: bool = True
    query_rewrite_enabled: bool = True
    # 父文档扩写窗口：限制从源文件读入的长度，不截断 SQLite 里的 inline 正文
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

    # ---- 学习扩展默认值（运行时以 runtime_flags 为准）----
    study_enabled: bool = False
    study_concepts: bool = True
    study_items: bool = True
    study_queue: bool = True
    study_weekly: bool = True
    study_dashboard: bool = True
    study_notifications: bool = False
    default_scope: str = "default"

    # ---- 服务 ----
    api_host: str = "127.0.0.1"
    api_port: int = 8710
    api_key: str = ""          # 兼容旧配置：等同 admin key
    api_read_key: str = ""     # 只读：search / ask / 文档读取
    api_admin_key: str = ""    # 管理：ingest / delete / flags / 审计
    # 为 true 时，即使只监听回环地址也必须配置 Key，否则拒绝启动。
    api_require_auth: bool = False
    # 单次上传上限（字节）。超过返回 413，并删除未写完的临时文件。
    max_upload_bytes: int = 20 * 1024 * 1024
    # 同时处理的上传数。多余请求排队，避免并发把磁盘打满。
    max_concurrent_uploads: int = 4
    # 对外 REST 是否带绝对 source_path / image_path。默认关，见 ADR 0002。
    expose_source_paths: bool = False
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
