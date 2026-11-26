"""概念页。派生 Markdown，不写回源目录，也不写绝对路径。"""

from __future__ import annotations

from pathlib import Path


def render_page(concept: dict, *, mentions: list[dict], related_notes: list[dict], edges: list[dict]) -> str:
    mastery = concept.get("mastery")
    mastery_line = "尚无学习记录" if mastery is None else f"{float(mastery):.2f}"
    lines = [
        f"# {concept['name']}",
        "",
        f"领域：{concept.get('domain') or 'general'}",
        "",
        "## 定义",
        "",
        (concept.get("definition") or "").strip() or "（无）",
        "",
        "## 掌握度",
        "",
        mastery_line,
        "",
        "## 出处",
        "",
    ]
    if not mentions:
        lines.append("（无可见出处）")
    else:
        for mention in mentions:
            title = mention.get("title") or mention["doc_id"]
            lines.append(f"- 《{title}》（{mention['doc_id']}）")
            snippet = (mention.get("snippet") or "").replace("\n", " ")
            lines.append(f"  > {snippet}")
    lines.extend(["", "## 相关笔记", ""])
    if not related_notes:
        lines.append("（无）")
    else:
        for note in related_notes:
            title = note.get("title") or note["doc_id"]
            lines.append(f"- 《{title}》（{note['doc_id']}）")
    lines.extend(["", "## 关系", ""])
    if not edges:
        lines.append("（无）")
    else:
        for edge in edges:
            lines.append(
                f"- {edge['type']} → {edge.get('dst_name') or edge['dst_id']}（{edge['dst_id']}）"
            )
    lines.append("")
    return "\n".join(lines)


def write_page(directory: Path, concept_id: str, body: str) -> str:
    """写入存储目录下的派生文件。返回相对名字，不返回绝对路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{concept_id}.md"
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    if existing != body:
        path.write_text(body, encoding="utf-8")
    return f"concept_pages/{concept_id}.md"


def remove_page(directory: Path, concept_id: str) -> None:
    path = directory / f"{concept_id}.md"
    if path.is_file():
        path.unlink()
