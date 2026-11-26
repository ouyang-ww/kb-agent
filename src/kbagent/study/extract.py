"""概念和关系抽取。

有本地模型或抽取 key 时走 LLM，输出必须能在片段里对上原文。
没有 key、云端也没被显式打开时，用规则降级，不访问网络。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import LLMError
from .normalize import normalize_name
from .schema import Ontology

SNIPPET_MAX = 180
PROMPT_MARK = "概念抽取器"

# 太泛，不能单独出题。抽到这些就丢掉，避免拉低「有意义」比例。
GENERIC = frozenset(
    {
        "检索",
        "优点",
        "缺点",
        "概述",
        "简介",
        "总结",
        "示例",
        "什么",
        "介绍",
        "说明",
        "注意",
        "其他",
        "流程",
        "步骤",
        "组成",
    }
)

_SKIP_HEADING = ("优缺点", "示例", "适用场景", "关键概念", "的组成", "流程")
_ALIAS_SPLIT = re.compile(r"[、,，/]")
_HEADING = re.compile(r"^#{1,6}\s+(?:什么是\s*)?(.+?)\s*$")
_NAME_ALIAS = re.compile(r"^(.{1,40}?)（([^）]{1,80})）\s*$")
_SENT_ALIAS = re.compile(
    r"^(.{1,40}?)（([^）]{1,80})）是(.{8,200})$"
)
_SENT_IS = re.compile(r"^([\w][\w .+\-]{0,30}?)是(.{8,200})$")
_BULLET = re.compile(r"^[-*]\s*(.{2,30}?)（([^）]{1,60})）[：:](.{4,120})$")
_PREREQ = re.compile(r"(.{2,30}?)依赖(.{2,30})")
_PREREQ_OF = re.compile(r"(.{2,30}?)的前置是(.{2,30})")
_PART = re.compile(r"(.{2,30}?)是(.{2,30}?)的一部分")


@dataclass
class ExtractedConcept:
    name: str
    definition: str
    aliases: list[str]
    entity_type: str
    evidence: str


@dataclass
class ExtractedRelation:
    src: str
    dst: str
    type: str
    evidence: str


@dataclass
class Extraction:
    concepts: list[ExtractedConcept] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    extractor: str = "heuristic"
    reason: str = ""


@dataclass
class ExtractTarget:
    """抽取走哪条路。kind=heuristic 时不会创建云端客户端。"""

    kind: str
    reason: str
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""


_PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}


def resolve_extract_target(settings) -> ExtractTarget:
    """本地模型优先。没有本地模型时，提供商默认 DeepSeek，但没有 key 就不调用。

    ``study_extract=heuristic`` 或 ``off`` 可以关掉云端，即使已经配置了 key。
    """
    mode = str(getattr(settings, "study_extract", "auto") or "auto").strip().lower()
    provider = str(getattr(settings, "study_extract_provider", "deepseek") or "deepseek").strip().lower()
    if provider not in _PROVIDERS:
        provider = "deepseek"
    local_model = str(getattr(settings, "study_local_model", "") or "").strip()
    local_url = str(
        getattr(settings, "study_local_base_url", "http://127.0.0.1:11434/v1")
        or "http://127.0.0.1:11434/v1"
    ).strip()
    extract_key = str(getattr(settings, "study_extract_api_key", "") or "").strip()
    extract_model = str(getattr(settings, "study_extract_model", "") or "").strip()
    extract_url = str(getattr(settings, "study_extract_base_url", "") or "").strip()
    default_url, default_model = _PROVIDERS[provider]

    if mode == "off":
        return ExtractTarget(kind="off", reason="extract_off", provider=provider)
    if mode == "heuristic":
        return ExtractTarget(kind="heuristic", reason="forced_heuristic", provider=provider)
    if local_model:
        return ExtractTarget(
            kind="llm",
            reason="local_model",
            provider="local",
            base_url=local_url,
            model=local_model,
            api_key=str(getattr(settings, "study_local_api_key", "") or "local"),
        )
    if mode == "llm" and not extract_key:
        return ExtractTarget(
            kind="heuristic",
            reason="no_llm_key",
            provider=provider,
        )
    if extract_key:
        return ExtractTarget(
            kind="llm",
            reason="cloud",
            provider=provider,
            base_url=extract_url or default_url,
            model=extract_model or default_model,
            api_key=extract_key,
        )
    return ExtractTarget(kind="heuristic", reason="no_llm_key", provider=provider)


def clip_evidence(evidence: str, chunk: str, name: str, limit: int = SNIPPET_MAX) -> str | None:
    """出处必须是片段里的连续子串，并且短于整篇上限。"""
    text = (evidence or "").strip()
    if not text or text not in chunk:
        return None
    if len(text) <= limit:
        return text
    pivot = text.find(name) if name and name in text else 0
    if pivot < 0:
        pivot = 0
    start = max(0, pivot - 12)
    clipped = text[start : start + limit]
    if clipped not in chunk:
        return None
    return clipped


class HeuristicExtractor:
    """没有 LLM 时的降级。只收片段里写明了的定义，不补外部知识。"""

    name = "heuristic"

    def __init__(self, ontology: Ontology, *, reason: str = "no_llm_key", limit: int = 3) -> None:
        self.ontology = ontology
        self.reason = reason
        self.limit = limit

    def extract(self, text: str) -> Extraction:
        return extract_heuristic(text, self.ontology, limit=self.limit, reason=self.reason)


class NullExtractor:
    name = "off"

    def __init__(self) -> None:
        self.reason = "extract_off"

    def extract(self, text: str) -> Extraction:
        return Extraction(extractor="off", reason=self.reason)


class LLMExtractor:
    """严格 JSON。对不上原文的概念和关系丢掉。调用失败时改走规则，不抛给入库。"""

    name = "llm"

    def __init__(self, client, ontology: Ontology, *, fallback: HeuristicExtractor, limit: int = 3) -> None:
        self.client = client
        self.ontology = ontology
        self.fallback = fallback
        self.limit = limit
        self.reason = "llm"

    def extract(self, text: str) -> Extraction:
        try:
            payload = self.client.chat_json(
                [
                    {"role": "system", "content": _prompt(self.ontology)},
                    {"role": "user", "content": text},
                ]
            )
        except (LLMError, Exception):
            fallen = self.fallback.extract(text)
            fallen.reason = "llm_failed"
            return fallen
        parsed = _from_payload(payload, text, self.ontology, self.limit)
        if parsed is None:
            fallen = self.fallback.extract(text)
            fallen.reason = "llm_invalid"
            return fallen
        parsed.extractor = "llm"
        parsed.reason = "llm"
        return parsed


def extract_heuristic(
    text: str,
    ontology: Ontology,
    *,
    limit: int = 3,
    reason: str = "no_llm_key",
) -> Extraction:
    found: dict[str, ExtractedConcept] = {}
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        if not line or line.startswith("#"):
            _maybe_heading(lines, index, text, found, limit)
            continue
        bullet = _BULLET.match(line)
        if bullet:
            _add(
                found,
                bullet.group(1),
                bullet.group(3),
                _split_aliases(bullet.group(2)),
                line,
                text,
                limit,
            )
            continue
        for sentence in _sentences(line):
            alias = _SENT_ALIAS.match(sentence)
            if alias:
                _add(
                    found,
                    alias.group(1),
                    sentence,
                    _split_aliases(alias.group(2)),
                    sentence,
                    text,
                    limit,
                )
                continue
            plain = _SENT_IS.match(sentence)
            if plain and "的" not in plain.group(1) and "，" not in plain.group(1):
                _add(found, plain.group(1), sentence, [], sentence, text, limit)
    relations = _relations(text, list(found.values()), ontology)
    return Extraction(
        concepts=list(found.values())[:limit],
        relations=relations,
        extractor="heuristic",
        reason=reason,
    )


def _maybe_heading(lines: list[str], index: int, chunk: str, found: dict, limit: int) -> None:
    raw = lines[index]
    if raw.startswith("# ") and not raw.startswith("##"):
        return
    match = _HEADING.match(raw)
    if not match:
        return
    title = match.group(1).strip()
    if any(token in title for token in _SKIP_HEADING):
        return
    aliases: list[str] = []
    named = _NAME_ALIAS.match(title)
    if named:
        title = named.group(1).strip()
        aliases = _split_aliases(named.group(2))
    if title.startswith("什么是"):
        title = title.removeprefix("什么是").strip()
    nxt = ""
    for follow in lines[index + 1 :]:
        if follow and not follow.startswith("#"):
            nxt = follow
            break
    if not nxt:
        return
    if title not in nxt and "是" not in nxt and "结合" not in nxt:
        return
    _add(found, title, nxt, aliases, nxt, chunk, limit)


def _add(
    found: dict[str, ExtractedConcept],
    name: str,
    definition: str,
    aliases: list[str],
    evidence: str,
    chunk: str,
    limit: int,
) -> None:
    if len(found) >= limit and normalize_name(name) not in found:
        return
    cleaned = _clean_name(name)
    norm = normalize_name(cleaned)
    if not cleaned or not norm or norm in GENERIC:
        return
    if len(cleaned) < 2 or len(cleaned) > 40:
        return
    definition = (definition or "").strip()
    if len(definition) < 8:
        return
    snippet = clip_evidence(evidence, chunk, cleaned)
    if snippet is None:
        return
    if cleaned not in snippet and definition[:8] not in snippet:
        return
    current = found.get(norm)
    if current is None:
        found[norm] = ExtractedConcept(
            name=cleaned,
            definition=definition[:SNIPPET_MAX],
            aliases=_dedupe(aliases, cleaned),
            entity_type="Concept",
            evidence=snippet,
        )
        return
    for alias in aliases:
        if alias and alias not in current.aliases and alias != current.name:
            current.aliases.append(alias)


def _relations(text: str, concepts: list[ExtractedConcept], ontology: Ontology) -> list[ExtractedRelation]:
    by_name = {}
    for concept in concepts:
        by_name[normalize_name(concept.name)] = concept.name
        for alias in concept.aliases:
            by_name.setdefault(normalize_name(alias), concept.name)
    found: list[ExtractedRelation] = []
    seen: set[tuple[str, str, str]] = set()
    allowed = set(ontology.relation_names())
    for sentence in _sentences(text):
        pairs = (
            ("prerequisite", _PREREQ.search(sentence)),
            ("prerequisite", _PREREQ_OF.search(sentence)),
            ("part_of", _PART.search(sentence)),
        )
        for rel_type, match in pairs:
            if match is None or rel_type not in allowed:
                continue
            src = by_name.get(normalize_name(match.group(1)))
            dst = by_name.get(normalize_name(match.group(2)))
            if not src or not dst or src == dst:
                continue
            evidence = clip_evidence(sentence, text, src)
            if evidence is None or src not in evidence or dst not in evidence:
                continue
            key = (src, dst, rel_type)
            if key in seen:
                continue
            seen.add(key)
            found.append(ExtractedRelation(src=src, dst=dst, type=rel_type, evidence=evidence))
    return found


def _from_payload(payload, chunk: str, ontology: Ontology, limit: int) -> Extraction | None:
    if not isinstance(payload, dict):
        return None
    concepts_raw = payload.get("concepts", [])
    relations_raw = payload.get("relations", [])
    if not isinstance(concepts_raw, list) or not isinstance(relations_raw, list):
        return None
    types = set(ontology.entity_types)
    concepts: list[ExtractedConcept] = []
    names: dict[str, str] = {}
    for item in concepts_raw:
        if len(concepts) >= limit:
            break
        if not isinstance(item, dict):
            continue
        name = _clean_name(str(item.get("name") or ""))
        evidence = str(item.get("evidence") or "")
        snippet = clip_evidence(evidence, chunk, name)
        if not name or snippet is None or normalize_name(name) in GENERIC:
            continue
        if name not in snippet:
            continue
        entity = str(item.get("entity_type") or "Concept")
        if entity not in types:
            continue
        aliases = item.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, list):
            continue
        definition = str(item.get("definition") or snippet).strip()[:SNIPPET_MAX]
        if len(definition) < 8:
            continue
        concepts.append(
            ExtractedConcept(
                name=name,
                definition=definition,
                aliases=_dedupe([str(a) for a in aliases], name),
                entity_type=entity,
                evidence=snippet,
            )
        )
        names[normalize_name(name)] = name
        for alias in concepts[-1].aliases:
            names.setdefault(normalize_name(alias), name)
    relations: list[ExtractedRelation] = []
    allowed = set(ontology.relation_names())
    for item in relations_raw:
        if not isinstance(item, dict):
            continue
        rel_type = str(item.get("type") or "")
        if rel_type not in allowed:
            continue
        src = names.get(normalize_name(str(item.get("src") or "")))
        dst = names.get(normalize_name(str(item.get("dst") or "")))
        evidence = clip_evidence(str(item.get("evidence") or ""), chunk, src or "")
        if not src or not dst or src == dst or evidence is None:
            continue
        if src not in evidence or dst not in evidence:
            continue
        relations.append(ExtractedRelation(src=src, dst=dst, type=rel_type, evidence=evidence))
    return Extraction(concepts=concepts, relations=relations)


def _prompt(ontology: Ontology) -> str:
    types = "、".join(ontology.entity_types)
    rels = "、".join(ontology.relation_names())
    return (
        f"你是{PROMPT_MARK}。只根据片段里的连续原文抽取，不要引入片段外的知识。"
        "严格输出 JSON 对象："
        '{"concepts":[{"name":"","definition":"","aliases":[],"entity_type":"Concept","evidence":"片段中的连续原文"}],'
        '"relations":[{"src":"","dst":"","type":"related","evidence":"片段中的连续原文"}]}'
        f"每个片段最多 3 个概念。entity_type 只能是：{types}。"
        f"关系 type 只能是：{rels}。"
        "evidence 必须是片段中的连续子串，对不上就不要输出该条。"
        "概念必须是能单独出一道题的最小单元。没有把握就返回空数组。"
    )


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in "。！？\n":
            sentence = "".join(buf).strip()
            if sentence:
                parts.append(sentence.strip("。！？"))
            buf = []
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _split_aliases(text: str) -> list[str]:
    return [part.strip() for part in _ALIAS_SPLIT.split(text) if part.strip()]


def _clean_name(name: str) -> str:
    text = (name or "").strip().strip("*`「」\"'“”")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ：:，,。.")


def _dedupe(items: list[str], name: str) -> list[str]:
    found: list[str] = []
    for item in items:
        text = _clean_name(item)
        if text and text != name and text not in found:
            found.append(text)
    return found


def is_meaningful(name: str, definition: str, snippets: list[str]) -> bool:
    """自动规则，不是 20 篇笔记的人工抽检。

    名称 2–40 字、不在泛词表、定义非空，并且名称或定义能在出处片段里对上。
    """
    cleaned = _clean_name(name)
    if len(cleaned) < 2 or len(cleaned) > 40:
        return False
    if normalize_name(cleaned) in GENERIC:
        return False
    body = (definition or "").strip()
    if len(body) < 8:
        return False
    if not snippets:
        return False
    needle = body[:8]
    grounded = False
    for snippet in snippets:
        if not snippet or len(snippet) > SNIPPET_MAX:
            return False
        if cleaned in snippet or needle in snippet:
            grounded = True
    return grounded


def duplicate_rate(rows: list[dict]) -> float:
    """消歧之后，仍共享规范化名或别名的多余概念占比。0 表示没有残留重复。"""
    active = [row for row in rows if row.get("status", "active") == "active"]
    count = len(active)
    if count == 0:
        return 0.0
    parent = {row["id"]: row["id"] for row in active}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    owners: dict[tuple[str, str], str] = {}
    for row in active:
        keys = [normalize_name(str(row.get("name") or ""))]
        keys.extend(normalize_name(str(item)) for item in row.get("aliases") or [])
        domain = str(row.get("domain") or "general")
        for key in keys:
            if not key:
                continue
            slot = (domain, key)
            other = owners.get(slot)
            if other is None:
                owners[slot] = row["id"]
            else:
                union(other, row["id"])
    clusters = {find(row["id"]) for row in active}
    return (count - len(clusters)) / count


def meaningful_ratio(rows: list[dict], snippets: dict[str, list[str]]) -> float | None:
    active = [row for row in rows if row.get("status", "active") == "active"]
    if not active:
        return None
    good = 0
    for row in active:
        if is_meaningful(str(row.get("name") or ""), str(row.get("definition") or ""), snippets.get(row["id"], [])):
            good += 1
    return good / len(active)
