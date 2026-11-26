"""本体-lite：加载白名单类型，并解析参照系用的一小段 YAML。

不引入 PyYAML。只覆盖文档里的本体和大纲写法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..errors import KBError

_PACKAGED = Path(__file__).with_name("default_ontology.yaml")


@dataclass
class RelationSpec:
    name: str
    src_type: str = "Concept"
    dst_type: str = "Concept"


@dataclass
class Ontology:
    domain: str = "general"
    entity_types: tuple[str, ...] = ("Concept",)
    relations: dict[str, RelationSpec] = field(default_factory=dict)
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def relation_names(self) -> tuple[str, ...]:
        return tuple(self.relations)

    def canonical_name(self, name: str, aliases: list[str] | None = None) -> tuple[str, list[str]]:
        """别名表把异名收成规范名。调用方仍要再走规范化精确匹配。"""
        from .normalize import normalize_name

        given = [name, *(aliases or [])]
        norms = {normalize_name(item): item for item in given if normalize_name(item)}
        for key, values in self.aliases.items():
            key_norm = normalize_name(key)
            alias_norms = {normalize_name(item) for item in values}
            if key_norm in norms or alias_norms & set(norms):
                merged: list[str] = []
                for item in (key, *values, *given):
                    text = (item or "").strip()
                    if text and text not in merged and text != key:
                        merged.append(text)
                return key.strip() or name, merged
        extras = []
        for item in given[1:]:
            text = (item or "").strip()
            if text and text not in extras and text != name:
                extras.append(text)
        return name.strip(), extras


def packaged_ontology_path() -> Path:
    return _PACKAGED


def load_ontology(path: str | Path | None = None) -> Ontology:
    target = Path(path) if path else _PACKAGED
    if not str(path or "").strip():
        target = _PACKAGED
    if not target.is_file():
        raise KBError("找不到本体文件")
    data = parse_miniyaml(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise KBError("本体文件必须是映射")
    return ontology_from_mapping(data)


def ontology_from_mapping(data: dict) -> Ontology:
    domain = str(data.get("domain") or "general").strip() or "general"
    raw_types = data.get("entity_types") or ["Concept"]
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    types = tuple(str(item).strip() for item in raw_types if str(item).strip())
    if "Concept" not in types:
        types = ("Concept", *types)
    relations: dict[str, RelationSpec] = {}
    raw_rels = data.get("relation_types") or {}
    if not isinstance(raw_rels, dict):
        raise KBError("relation_types 必须是映射")
    for name, spec in raw_rels.items():
        label = str(name).strip()
        if not label:
            continue
        if isinstance(spec, dict):
            relations[label] = RelationSpec(
                name=label,
                src_type=str(spec.get("from") or "Concept"),
                dst_type=str(spec.get("to") or "Concept"),
            )
        else:
            relations[label] = RelationSpec(name=label)
    aliases: dict[str, tuple[str, ...]] = {}
    raw_aliases = data.get("aliases") or {}
    if raw_aliases and not isinstance(raw_aliases, dict):
        raise KBError("aliases 必须是映射")
    for key, values in raw_aliases.items():
        label = str(key).strip()
        if not label:
            continue
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            raise KBError("别名必须是列表")
        aliases[label] = tuple(str(item).strip() for item in values if str(item).strip())
    return Ontology(domain=domain, entity_types=types, relations=relations, aliases=aliases)


def parse_miniyaml(text: str):
    """缩进映射、列表、单行 flow。够本体和参照系大纲用。"""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("\t"):
            raise KBError("本体或大纲不能用 Tab 缩进")
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip()))
    if not lines:
        raise KBError("本体或大纲是空的")
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise KBError("本体或大纲没有解析完")
    return value


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int):
    if index >= len(lines):
        return {}, index
    if lines[index][1].startswith("- ") and lines[index][0] == indent:
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(lines, index: int, indent: int):
    mapping: dict = {}
    while index < len(lines) and lines[index][0] == indent and not lines[index][1].startswith("- "):
        key, rest = _split_key(lines[index][1])
        index += 1
        if rest == "":
            if index < len(lines) and lines[index][0] > indent:
                child, index = _parse_block(lines, index, lines[index][0])
                mapping[key] = child
            else:
                mapping[key] = ""
            continue
        if rest.startswith("[") and rest.endswith("]"):
            mapping[key] = _parse_flow_list(rest)
            continue
        if rest.startswith("{") and rest.endswith("}"):
            mapping[key] = _parse_flow_map(rest)
            continue
        mapping[key] = _scalar(rest)
    return mapping, index


def _parse_list(lines, index: int, indent: int):
    items = []
    while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
        rest = lines[index][1][2:].strip()
        index += 1
        if rest == "":
            if index < len(lines) and lines[index][0] > indent:
                child, index = _parse_block(lines, index, lines[index][0])
                items.append(child)
            else:
                items.append("")
            continue
        if ":" in rest and not rest.startswith(("'", '"')):
            key, value = _split_key(rest)
            item: dict = {}
            if value == "":
                if index < len(lines) and lines[index][0] > indent:
                    child, index = _parse_block(lines, index, lines[index][0])
                    item[key] = child
                else:
                    item[key] = ""
            elif value.startswith("[") and value.endswith("]"):
                item[key] = _parse_flow_list(value)
            elif value.startswith("{") and value.endswith("}"):
                item[key] = _parse_flow_map(value)
            else:
                item[key] = _scalar(value)
            while index < len(lines) and lines[index][0] > indent and not lines[index][1].startswith("- "):
                extra, index = _parse_map(lines, index, lines[index][0])
                item.update(extra)
                break
            items.append(item)
            continue
        items.append(_scalar(rest))
    return items, index


def _split_key(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise KBError("映射行缺少冒号")
    key, rest = text.split(":", 1)
    key = key.strip()
    if not key:
        raise KBError("映射缺少键")
    return key, rest.strip()


def _parse_flow_list(text: str) -> list:
    body = text[1:-1].strip()
    if not body:
        return []
    return [_scalar(part.strip()) for part in _split_flow(body)]


def _parse_flow_map(text: str) -> dict:
    body = text[1:-1].strip()
    mapping = {}
    if not body:
        return mapping
    for part in _split_flow(body):
        key, value = _split_key(part.strip())
        mapping[key] = _scalar(value)
    return mapping


def _split_flow(body: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in {'"', "'"}:
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _scalar(text: str):
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if value in {"true", "True", "yes"}:
        return True
    if value in {"false", "False", "no"}:
        return False
    if value in {"null", "Null", "~"}:
        return None
    return value
