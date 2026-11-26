"""消歧：规范化精确匹配和别名表自动合并。嵌入相似只记建议。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .extract import ExtractedConcept
from .normalize import normalize_name
from .schema import Ontology


@dataclass
class ResolvedConcept:
    concept_id: str
    name: str
    normalized: str
    domain: str
    definition: str
    aliases: list[str]
    entity_type: str
    evidence: str
    created: bool = False


@dataclass
class ResolveResult:
    concepts: list[ResolvedConcept] = field(default_factory=list)
    suggestions: list[tuple[str, str, float]] = field(default_factory=list)


def concept_id_for(domain: str, normalized: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"{domain}\n{normalized}".encode("utf-8")).hexdigest()[:16]
    return f"c_{digest}"


def resolve_concept(
    extracted: ExtractedConcept,
    *,
    ontology: Ontology,
    store,
    domain: str,
) -> ResolvedConcept | None:
    """第 1–2 步自动复用。本体别名和已经记下的别名都算精确命中。"""
    name, aliases = ontology.canonical_name(extracted.name, extracted.aliases)
    normalized = normalize_name(name)
    if not normalized:
        return None
    existing = existing_concept(
        extracted, ontology=ontology, store=store, domain=domain
    )
    created = existing is None
    if existing is None:
        row = store.insert_concept(
            concept_id=concept_id_for(domain, normalized),
            name=name,
            normalized=normalized,
            domain=domain,
            definition=extracted.definition,
            aliases=aliases,
            entity_type=extracted.entity_type,
        )
    else:
        row = store.add_aliases(existing["id"], [name, *aliases], extracted.definition)
    return ResolvedConcept(
        concept_id=row["id"],
        name=row["name"],
        normalized=row["normalized"],
        domain=row["domain"],
        definition=row["definition"],
        aliases=list(row["aliases"]),
        entity_type=row["entity_type"],
        evidence=extracted.evidence,
        created=created,
    )


def existing_concept(extracted: ExtractedConcept, *, ontology: Ontology, store, domain: str) -> dict | None:
    """按规范名，或按已经存储的别名，找到仍有效的概念。"""
    name, aliases = ontology.canonical_name(extracted.name, extracted.aliases)
    normalized = normalize_name(name)
    if not normalized:
        return None
    found = _follow_merge(store, store.find_concept(concept_id_for(domain, normalized)))
    if found is not None:
        return found
    return _follow_merge(store, _find_by_stored_alias(store, domain, [name, *aliases]))


def _find_by_stored_alias(store, domain: str, names: list[str]) -> dict | None:
    wanted = {normalize_name(item) for item in names}
    wanted.discard("")
    if not wanted:
        return None
    for row in store.list_concepts(status="active"):
        if row.get("domain") != domain:
            continue
        keys = {normalize_name(str(row.get("name") or ""))}
        keys.update(normalize_name(str(item)) for item in row.get("aliases") or [])
        keys.discard("")
        if keys & wanted:
            return row
    return None


def _follow_merge(store, row: dict | None) -> dict | None:
    seen: set[str] = set()
    while row is not None and row.get("status") == "merged" and row.get("merged_into"):
        if row["id"] in seen:
            return None
        seen.add(row["id"])
        row = store.find_concept(row["merged_into"])
    if row is not None and row.get("status") == "merged":
        return None
    return row


def suggest_merges(
    concept_id: str,
    *,
    store,
    embedder,
    threshold: float,
) -> list[tuple[str, str, float]]:
    """相似度超过阈值只记建议。自动合并不到这一步。"""
    if embedder is None or threshold <= 0:
        return []
    current = store.find_concept(concept_id)
    if current is None or current.get("status") != "active":
        return []
    others = [
        row
        for row in store.list_concepts(status="active")
        if row["id"] != concept_id and row.get("entity_type") == current.get("entity_type")
    ]
    if not others:
        return []
    try:
        vectors = embedder.embed_texts(
            [current["name"], *[row["name"] for row in others]]
        )
    except Exception:
        return []
    if not vectors or len(vectors) != len(others) + 1:
        return []
    base = vectors[0]
    suggestions = []
    for row, vector in zip(others, vectors[1:]):
        score = _cosine(base, vector)
        if score > threshold:
            suggestions.append((concept_id, row["id"], score))
    return suggestions


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right)))
