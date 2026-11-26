"""把抽取、消歧、出处和概念页接到 study 库。不写源文件，也不写原件全文。"""

from __future__ import annotations

from pathlib import Path

from ..errors import KBError, NotFoundError
from ..ingestion.loaders import content_hash
from .extract import (
    Extraction,
    HeuristicExtractor,
    LLMExtractor,
    NullExtractor,
    duplicate_rate,
    meaningful_ratio,
    resolve_extract_target,
)
from .normalize import normalize_name
from .outline import coverage_report, flatten_nodes, load_outline_file
from .pages import remove_page, render_page, write_page
from .resolve import existing_concept, resolve_concept, suggest_merges
from .schema import Ontology, load_ontology

MEANINGFUL_RULE = (
    "名称 2–40 字、不在泛词表、定义至少 8 字，并且名称或定义能在出处片段里对上。"
    "这是自动规则的比例，不是 20 篇真实笔记的人工抽检。"
)
DUPLICATE_LIMIT = 0.10
MEANINGFUL_LIMIT = 0.90
NEW_PER_DOC = 7


class ConceptIndex:
    def __init__(
        self,
        store,
        settings,
        *,
        chunks_for,
        doc_allowed,
        doc_label,
        embedder,
        pages_dir: Path,
        llm=None,
        extractor=None,
        ontology: Ontology | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.chunks_for = chunks_for
        self.doc_allowed = doc_allowed
        self.doc_label = doc_label
        self.embedder = embedder
        self.pages_dir = Path(pages_dir)
        self.ontology = ontology or _ontology(settings)
        self.prompt_version = str(getattr(settings, "study_prompt_version", "r1") or "r1")
        self.per_doc = int(getattr(settings, "study_max_new_concepts_per_doc", NEW_PER_DOC) or NEW_PER_DOC)
        self.threshold = float(getattr(settings, "study_merge_similarity", 0.88) or 0.88)
        self.extractor = extractor or build_extractor(settings, llm, self.ontology)
        self.domain = self.ontology.domain or "general"

    def sync_doc(self, doc_id: str) -> dict:
        """按当前切块重抽。不可见的文档不送去抽取，也不写出处。"""
        self.store.set_meta("extract_mode", self.extractor.name)
        self.store.set_meta("extract_reason", getattr(self.extractor, "reason", ""))
        if not doc_id or not self.doc_allowed(doc_id):
            return {"doc_id": doc_id, "skipped": "not_visible", "concepts": 0}
        chunks = list(self.chunks_for(doc_id) or [])
        self.store.delete_mentions_outside(doc_id, {chunk["id"] for chunk in chunks})
        created = 0
        written = 0
        for chunk in chunks:
            text = str(chunk.get("text") or "")
            chunk_id = str(chunk["id"])
            if not text.strip():
                continue
            digest = content_hash(text)
            if self.store.has_extraction(chunk_id, digest, self.prompt_version):
                continue
            extraction = self.extractor.extract(text)
            self.store.set_meta("extract_mode", extraction.extractor)
            self.store.set_meta("extract_reason", extraction.reason)
            self.store.delete_chunk_mentions(doc_id, chunk_id)
            resolved = []
            for concept in extraction.concepts:
                if not self._already_known(concept) and created >= self.per_doc:
                    continue
                item = resolve_concept(
                    concept,
                    ontology=self.ontology,
                    store=self.store,
                    domain=self.domain,
                )
                if item is None:
                    continue
                if item.created:
                    if created >= self.per_doc:
                        continue
                    created += 1
                    for src, dst, score in suggest_merges(
                        item.concept_id,
                        store=self.store,
                        embedder=self.embedder,
                        threshold=self.threshold,
                    ):
                        self.store.add_suggestion(src, dst, score)
                if not item.evidence or len(item.evidence) > 180:
                    continue
                if item.evidence not in text:
                    continue
                self.store.insert_mention(
                    concept_id=item.concept_id,
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    snippet=item.evidence,
                )
                resolved.append((concept, item))
                written += 1
            self._write_relations(doc_id, chunk_id, extraction, resolved)
            self.store.save_extraction(
                chunk_id=chunk_id,
                doc_id=doc_id,
                chunk_hash=digest,
                prompt_version=self.prompt_version,
                extractor=extraction.extractor,
            )
        return {"doc_id": doc_id, "skipped": "", "concepts": written}

    def forget_doc(self, doc_id: str) -> None:
        self.store.delete_doc_derived(doc_id)

    def list_visible(self) -> list[dict]:
        rows = []
        for concept in self.store.list_concepts(status="active"):
            mentions = self._visible_mentions(concept["id"])
            if not mentions:
                continue
            item = dict(concept)
            item["mentions"] = mentions
            item["mastery"] = None
            item["visible"] = True
            rows.append(item)
        return rows

    def page(self, key: str) -> dict:
        concept = self._visible_by_key(key)
        mentions = concept["mentions"]
        edges = self._visible_edges(concept["id"])
        related = _related_notes(mentions, edges, self)
        body = render_page(
            concept,
            mentions=mentions,
            related_notes=related,
            edges=edges,
        )
        ref = write_page(self.pages_dir, concept["id"], body)
        return {
            "id": concept["id"],
            "name": concept["name"],
            "definition": concept["definition"],
            "aliases": concept["aliases"],
            "domain": concept["domain"],
            "mastery": None,
            "mentions": [_public_mention(item) for item in mentions],
            "related_notes": related,
            "edges": edges,
            "page": body,
            "page_ref": ref,
        }

    def rebuild_pages(self) -> int:
        visible = self.list_visible()
        keep = set()
        for concept in visible:
            edges = self._visible_edges(concept["id"])
            related = _related_notes(concept["mentions"], edges, self)
            body = render_page(
                concept,
                mentions=concept["mentions"],
                related_notes=related,
                edges=edges,
            )
            write_page(self.pages_dir, concept["id"], body)
            keep.add(concept["id"])
        if self.pages_dir.is_dir():
            for path in self.pages_dir.glob("c_*.md"):
                if path.stem not in keep:
                    remove_page(self.pages_dir, path.stem)
        return len(keep)

    def audit(self) -> dict:
        visible = self.list_visible()
        snippets = {
            row["id"]: [mention["snippet"] for mention in row["mentions"]]
            for row in visible
        }
        ratio = meaningful_ratio(visible, snippets)
        suggestions = self.visible_suggestions()
        return {
            "concepts": len(visible),
            "duplicate_rate": duplicate_rate(visible),
            "duplicate_limit": DUPLICATE_LIMIT,
            "meaningful_ratio": ratio,
            "meaningful_limit": MEANINGFUL_LIMIT,
            "meaningful_rule": MEANINGFUL_RULE,
            "suggestions": len(suggestions),
            "merge_similarity": self.threshold,
            "extractor": self.store.get_meta_value("extract_mode") or self.extractor.name,
            "extract_reason": self.store.get_meta_value("extract_reason")
            or getattr(self.extractor, "reason", ""),
        }

    def visible_suggestions(self) -> list[dict]:
        visible = {row["id"] for row in self.list_visible()}
        found = []
        for row in self.store.list_suggestions("pending"):
            if row["src_id"] in visible and row["dst_id"] in visible:
                found.append(
                    {
                        "src_id": row["src_id"],
                        "dst_id": row["dst_id"],
                        "score": row["score"],
                    }
                )
        return found

    def merge(self, left: str, right: str, *, dry_run: bool = False) -> dict:
        keep = self._visible_by_key(left)
        drop = self._visible_by_key(right)
        plan = {
            "keep_id": keep["id"],
            "keep_name": keep["name"],
            "drop_id": drop["id"],
            "drop_name": drop["name"],
            "applied": False,
        }
        if dry_run:
            return plan
        self.store.merge_concepts(keep["id"], drop["id"])
        plan["applied"] = True
        self.rebuild_pages()
        return plan

    def import_outline(self, path: str | Path) -> dict:
        data = load_outline_file(path)
        nodes = flatten_nodes(data)
        if not nodes:
            raise KBError("参照系没有节点")
        self.store.replace_outline(nodes[0]["domain"], nodes)
        return {"domain": nodes[0]["domain"], "nodes": len(nodes)}

    def coverage(self, domain: str | None = None) -> dict:
        nodes = self.store.list_outline(domain)
        if domain and not nodes:
            raise NotFoundError("参照系不存在")
        concepts = self.list_visible()
        report = coverage_report(nodes, concepts, mastery_by_concept={})
        return report

    def _visible_mentions(self, concept_id: str) -> list[dict]:
        found = []
        for mention in self.store.list_mentions(concept_id=concept_id):
            if not self.doc_allowed(mention["doc_id"]):
                continue
            label = self.doc_label(mention["doc_id"])
            if "source_path" in label:
                label = {key: value for key, value in label.items() if key != "source_path"}
            found.append({**mention, "title": label.get("title") or mention["doc_id"]})
        return found

    def _visible_edges(self, concept_id: str) -> list[dict]:
        edges = []
        for edge in self.store.list_edges(concept_id):
            if not self._visible_mentions(edge["dst_id"]):
                continue
            dst = self.store.find_concept(edge["dst_id"])
            edges.append(
                {
                    "src_id": edge["src_id"],
                    "dst_id": edge["dst_id"],
                    "dst_name": None if dst is None else dst["name"],
                    "type": edge["type"],
                    "evidence": edge["evidence"],
                }
            )
        return edges

    def _already_known(self, concept) -> bool:
        row = existing_concept(
            concept,
            ontology=self.ontology,
            store=self.store,
            domain=self.domain,
        )
        return row is not None and row.get("status") == "active"

    def _visible_by_key(self, key: str) -> dict:
        wanted = (key or "").strip()
        if not wanted:
            raise KBError("概念不能为空")
        visible = self.list_visible()
        norm = normalize_name(wanted)
        for concept in visible:
            if concept["id"] == wanted or concept["name"] == wanted or concept["normalized"] == norm:
                return concept
        raise NotFoundError("概念不存在")

    def _write_relations(self, doc_id: str, chunk_id: str, extraction: Extraction, resolved) -> None:
        names = {}
        for concept, item in resolved:
            names[normalize_name(concept.name)] = item.concept_id
            names[normalize_name(item.name)] = item.concept_id
            for alias in item.aliases:
                names.setdefault(normalize_name(alias), item.concept_id)
        for relation in extraction.relations:
            src = names.get(normalize_name(relation.src))
            dst = names.get(normalize_name(relation.dst))
            if not src or not dst or src == dst:
                continue
            if relation.type not in self.ontology.relations:
                continue
            if not relation.evidence:
                continue
            self.store.upsert_edge(
                src_id=src,
                dst_id=dst,
                rel_type=relation.type,
                provenance=f"{doc_id}/{chunk_id}",
                evidence=relation.evidence,
            )


def build_extractor(settings, llm, ontology: Ontology):
    target = resolve_extract_target(settings)
    limit = int(getattr(settings, "study_max_concepts_per_chunk", 3) or 3)
    heuristic = HeuristicExtractor(ontology, reason=target.reason, limit=limit)
    if target.kind == "off":
        return NullExtractor()
    if target.kind != "llm":
        return heuristic
    from ..generation.llm import LLMClient

    client = llm
    if client is None or not _client_matches(client, target):
        client = LLMClient(
            base_url=target.base_url,
            api_key=target.api_key,
            model=target.model,
            timeout=float(getattr(settings, "study_extract_timeout", 20.0) or 20.0),
        )
    return LLMExtractor(client, ontology, fallback=heuristic, limit=limit)


def _client_matches(client, target) -> bool:
    """地址、模型和密钥都相同才复用。只对上 DeepSeek 的 URL 不能借用问答密钥。"""
    return (
        getattr(client, "base_url", None) == target.base_url
        and getattr(client, "model", None) == target.model
        and getattr(client, "api_key", None) == target.api_key
    )


def _ontology(settings) -> Ontology:
    path = str(getattr(settings, "study_ontology_path", "") or "").strip()
    return load_ontology(path or None)


def _related_notes(mentions: list[dict], edges: list[dict], index: ConceptIndex) -> list[dict]:
    notes = []
    seen = set()
    for mention in mentions:
        if mention["doc_id"] in seen:
            continue
        seen.add(mention["doc_id"])
        notes.append({"doc_id": mention["doc_id"], "title": mention.get("title") or mention["doc_id"]})
    for edge in edges:
        for mention in index._visible_mentions(edge["dst_id"]):
            if mention["doc_id"] in seen:
                continue
            seen.add(mention["doc_id"])
            notes.append({"doc_id": mention["doc_id"], "title": mention.get("title") or mention["doc_id"]})
    return notes


def _public_mention(mention: dict) -> dict:
    return {
        "doc_id": mention["doc_id"],
        "title": mention.get("title") or mention["doc_id"],
        "chunk_id": mention.get("chunk_id") or "",
        "snippet": mention.get("snippet") or "",
    }
