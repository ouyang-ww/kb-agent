"""近似重复：SimHash 与切块余弦。精确哈希去重仍走原来的 content_hash。

默认阈值写在 Settings 里，不是这次语料上重标出来的：

- 文档 / 切块余弦 ``>= 0.95``（向量需先 L2 归一化；换嵌入模型必须重标）
- SimHash 64 位，汉字 3-gram，汉明距离 ``<= 3``
- 检索 MMR ``λ = 0.7``
- 邻居关系建议的余弦 ``>= 0.80``（低于去重阈值，只产生 pending 的 related / supersedes）

只跳过「真正的第二份索引」。调用方用 metadata 声明了 supersedes / contradicts /
merged_into / related，或 ``allow_near_duplicate=true`` 时，不把新文档当成冗余副本，
两篇都入库，关系仍要确认后才生效。自动合并只发生在原有的精确哈希跳过。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

# 与 Settings 默认值保持一致，方便测试和文档引用。改默认值时两处一起改。
DEFAULT_COSINE = 0.95
DEFAULT_HAMMING = 3
# 余弦达到去重线时，SimHash 还得落在这个距离内，才当成同一份副本。
# 只改了一个数字或一句结论时，汉明距离通常更大，不能因此丢掉未声明的冲突。
DEFAULT_COSINE_HAMMING = 3
DEFAULT_MMR_LAMBDA = 0.7
DEFAULT_NEIGHBOR_COSINE = 0.80

RETUNE_NOTE = (
    "余弦 0.95、SimHash 汉明距离 3、MMR λ 0.7、邻居余弦 0.80 是文档化默认值，"
    "按 L2 归一化后的向量理解（当前默认嵌入是 BAAI/bge-small-zh-v1.5）。"
    "余弦很高但 3-gram 汉明距离大于 3 时不折叠、不跳过入库，避免误伤未声明的冲突或复述。"
    "这次没有用标注重复集重标。换嵌入模型、度量或语料后，这些数都会失效，必须重标。"
)

RELATION_TYPES = ("supersedes", "merged_into", "contradicts", "related")
_RELATION_INTENT = frozenset(RELATION_TYPES)


def cosine(left: list[float], right: list[float]) -> float:
    """两个向量的余弦。不假设已经归一化。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    denom = math.sqrt(left_norm) * math.sqrt(right_norm)
    if denom <= 0:
        return 0.0
    return dot / denom


def mean_pool(vectors: list[list[float]]) -> list[float] | None:
    usable = [list(row) for row in vectors if row]
    if not usable:
        return None
    width = len(usable[0])
    acc = [0.0] * width
    count = 0
    for row in usable:
        if len(row) != width:
            continue
        for i, value in enumerate(row):
            acc[i] += float(value)
        count += 1
    if count == 0:
        return None
    return [value / count for value in acc]


def simhash64(text: str) -> int:
    """64 位 SimHash。中文按连续 3 字切，没有足够长度时退回整段。"""
    raw = text or ""
    grams = [raw[i : i + 3] for i in range(max(0, len(raw) - 2))]
    if not grams:
        grams = [raw] if raw else [" "]
    bits = 64
    acc = [0] * bits
    for gram in grams:
        digest = hashlib.md5(gram.encode("utf-8")).digest()
        value = int.from_bytes(digest[:8], "big")
        for i in range(bits):
            acc[i] += 1 if (value >> i) & 1 else -1
    out = 0
    for i, weight in enumerate(acc):
        if weight > 0:
            out |= 1 << i
    return out


def simhash_hex(text: str) -> str:
    return f"{simhash64(text):016x}"


def parse_simhash(value: object) -> int | None:
    text = str(value or "").strip().lower()
    if len(text) != 16:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def relation_intent(metadata: dict | None) -> bool:
    """调用方明确说这不是一份冗余副本。"""
    meta = metadata or {}
    if meta.get("allow_near_duplicate") in (True, "true", "1", 1):
        return True
    for key in _RELATION_INTENT:
        if _target_ids(meta.get(key)):
            return True
    return False


def explicit_relations(metadata: dict | None) -> list[tuple[str, str]]:
    """metadata 里点名的关系。(dst_id, type)。仍然只作为 pending 建议。"""
    meta = metadata or {}
    found: list[tuple[str, str]] = []
    for key in RELATION_TYPES:
        for dst in _target_ids(meta.get(key)):
            found.append((dst, key))
    return found


def _target_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def hook_relation(
    *,
    explicit: str | None,
    cosine_score: float,
    neighbor_threshold: float,
    neighbor_expired: bool,
    new_verified: str | None,
    old_verified: str | None,
) -> str | None:
    """没有 LLM 时的确定性关系。只返回建议类型，不直接生效。"""
    if explicit in _RELATION_INTENT:
        return explicit
    if cosine_score < neighbor_threshold:
        return None
    if neighbor_expired:
        return "supersedes"
    if new_verified and old_verified and new_verified > old_verified:
        return "supersedes"
    return "related"


def parse_llm_relation(payload: Any) -> tuple[str | None, float]:
    """只接受四种关系名。别的 JSON 当成没判断出来。"""
    if not isinstance(payload, dict):
        return None, 0.0
    raw = payload.get("type") or payload.get("relation")
    if not isinstance(raw, str) or raw.strip() not in _RELATION_INTENT:
        return None, 0.0
    try:
        confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    if confidence != confidence:  # NaN
        confidence = 0.5
    return raw.strip(), max(0.0, min(1.0, confidence))
