"""参照系：导入大纲，按可见出处算覆盖率和缺口。

掌握度覆盖率在没有学习记录时留空，不把「还没学」写成已经测出的 0。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..errors import KBError
from .normalize import normalize_name
from .schema import parse_miniyaml


def load_outline_file(path: str | Path) -> dict:
    target = Path(path)
    if not target.is_file():
        raise KBError("找不到参照系文件")
    data = parse_miniyaml(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "nodes" not in data:
        raise KBError("参照系需要 domain 和 nodes")
    return data


def flatten_nodes(data: dict) -> list[dict]:
    domain = str(data.get("domain") or "general").strip() or "general"
    source = str(data.get("source") or "")
    flat: list[dict] = []

    def walk(nodes, parent_id: str | None) -> None:
        if not isinstance(nodes, list):
            raise KBError("参照系 nodes 必须是列表")
        for node in nodes:
            if isinstance(node, str):
                node = {"name": node}
            if not isinstance(node, dict) or not str(node.get("name") or "").strip():
                raise KBError("参照系节点缺少 name")
            name = str(node["name"]).strip()
            aliases = node.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            if not isinstance(aliases, list):
                raise KBError("参照系别名必须是列表")
            node_id = _node_id(domain, parent_id or "", name)
            flat.append(
                {
                    "id": node_id,
                    "domain": domain,
                    "name": name,
                    "normalized": normalize_name(name),
                    "aliases": [str(item).strip() for item in aliases if str(item).strip()],
                    "parent_id": parent_id,
                    "ord": len(flat),
                    "source": source,
                }
            )
            children = node.get("children") or []
            walk(children, node_id)

    walk(data.get("nodes") or [], None)
    return flat


def coverage_report(
    nodes: list[dict],
    concepts: list[dict],
    *,
    mastery_by_concept: dict[str, float | None],
    threshold: float = 0.6,
) -> dict:
    """有可见 mention 的节点算覆盖。缺口是没有可见 mention 的节点。

    ``mastery_coverage`` 只在确实有学习记录时计算（mention 且 mastery ≥ 阈值）。
    一条记录都没有则为 null。
    """
    if not nodes:
        return {
            "domain": "",
            "source": "",
            "nodes": 0,
            "with_mention": 0,
            "coverage": None,
            "mastery_coverage": None,
            "gaps": [],
            "covered": [],
        }
    index = _concept_index(concepts)
    covered = []
    gaps = []
    mastered = 0
    mastery_known = any(value is not None for value in mastery_by_concept.values())
    for node in nodes:
        match = _match_node(node, index)
        if match is None:
            gaps.append({"name": node["name"], "reason": "无可见出处"})
            continue
        covered.append(
            {
                "name": node["name"],
                "concept_id": match["id"],
                "concept_name": match["name"],
            }
        )
        mastery = mastery_by_concept.get(match["id"])
        if mastery is not None and mastery >= threshold:
            mastered += 1
    total = len(nodes)
    return {
        "domain": nodes[0]["domain"],
        "source": nodes[0].get("source") or "",
        "nodes": total,
        "with_mention": len(covered),
        "coverage": len(covered) / total,
        "mastery_coverage": (mastered / total) if mastery_known else None,
        "gaps": gaps,
        "covered": covered,
    }


def _concept_index(concepts: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for concept in concepts:
        if concept.get("status") not in (None, "active"):
            continue
        if not concept.get("visible", True):
            continue
        domain = str(concept.get("domain") or "general")
        keys = [normalize_name(str(concept.get("name") or ""))]
        keys.extend(normalize_name(str(item)) for item in concept.get("aliases") or [])
        for key in keys:
            if key:
                index.setdefault(f"{domain}:{key}", concept)
                index.setdefault(key, concept)
    return index


def _match_node(node: dict, index: dict[str, dict]) -> dict | None:
    domain = str(node.get("domain") or "general")
    keys = [node.get("normalized") or normalize_name(str(node.get("name") or ""))]
    keys.extend(normalize_name(item) for item in node.get("aliases") or [])
    for key in keys:
        if not key:
            continue
        found = index.get(f"{domain}:{key}") or index.get(key)
        if found is not None:
            return found
    return None


def _node_id(domain: str, parent: str, name: str) -> str:
    digest = hashlib.sha256(f"{domain}\n{parent}\n{name}".encode("utf-8")).hexdigest()[:16]
    return f"o_{digest}"
