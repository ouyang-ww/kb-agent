"""3 道硬编码题，原文来自 examples/sample_notes/01_rag基础.md。

不是 LLM 出题，也不是 S1 的题库生成。已落盘的题目不会被改写。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

PROMPT_VERSION = "hardcoded-r0"

_DROP = dict.fromkeys(
    map(
        ord,
        " \t\r\n\u3000-‐‑‒–—―－，,。．.、；;：:！!？?（）()【】[]《》<>「」『』“”\"'`·…",
    ),
    None,
)


def normalize_answer(text: str) -> str:
    """去掉空白和标点、忽略大小写，用来做精确判分。"""
    return text.strip().casefold().translate(_DROP)


def is_correct(answer: str, accept: tuple[str, ...], response: str) -> bool:
    got = normalize_answer(response)
    if not got:
        return False
    allowed = {normalize_answer(answer)}
    allowed.update(normalize_answer(item) for item in accept)
    allowed.discard("")
    return got in allowed


@dataclass(frozen=True)
class Card:
    id: str
    type: str
    stem: str
    answer: str
    accept: tuple[str, ...]
    source_label: str
    source_span: str

    def content_hash(self) -> str:
        payload = f"{PROMPT_VERSION}\n{self.stem}\n{self.answer}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# 出处片段必须是示例笔记里的连续原文。改笔记时要一起改这里。
HARDCODED_CARDS: tuple[Card, ...] = (
    Card(
        id="rag-fullname",
        type="short",
        stem="RAG 的英文全称是什么？",
        answer="Retrieval-Augmented Generation",
        accept=("Retrieval Augmented Generation",),
        source_label="examples/sample_notes/01_rag基础.md",
        source_span=(
            "RAG（Retrieval-Augmented Generation，检索增强生成）"
            "是一种将信息检索与大语言模型生成相结合的架构。"
        ),
    ),
    Card(
        id="rag-index-sink",
        type="short",
        stem="笔记里，RAG 索引阶段的最后一步是什么？",
        answer="写入向量库",
        accept=(),
        source_label="examples/sample_notes/01_rag基础.md",
        source_span="1. 索引阶段：文档加载 → 清洗 → 切块 → 向量化 → 写入向量库",
    ),
    Card(
        id="rag-quality-cap",
        type="short",
        stem="笔记认为，什么决定了 RAG 答案质量的上限？",
        answer="检索质量",
        accept=("检索质量决定上限",),
        source_label="examples/sample_notes/01_rag基础.md",
        source_span="缺点：检索质量决定上限，需要维护索引，多一跳延迟。",
    ),
)
