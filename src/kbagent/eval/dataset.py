"""评测集：JSONL 读写、schema 校验、入库存在性校验。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class EvalItem:
    query: str
    expected_answer: str = ""
    relevant_doc_ids: list[str] = field(default_factory=list)
    category: str = ""
    image_required: bool = False
    should_refuse: bool = False
    # P2 评测集自带笔记和关系。空列表表示这条不需要临时入库。
    documents: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    missing_points: list[str] = field(default_factory=list)
    expired_index: int | None = None


def load_dataset(path: str | Path) -> list[EvalItem]:
    p = Path(path)
    items: list[EvalItem] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            items.append(
                EvalItem(
                    query=obj.get("query", ""),
                    expected_answer=obj.get("expected_answer", ""),
                    relevant_doc_ids=list(obj.get("relevant_doc_ids", [])),
                    category=obj.get("category", ""),
                    image_required=bool(obj.get("image_required", False)),
                    should_refuse=bool(obj.get("should_refuse", False)),
                    documents=list(obj.get("documents") or []),
                    relations=list(obj.get("relations") or []),
                    missing_points=list(obj.get("missing_points") or []),
                    expired_index=obj.get("expired_index"),
                )
            )
    return items


def save_dataset(items: list[EvalItem], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")


def validate(dataset: list[EvalItem], doc_ids_in_kb: set[str]) -> list[str]:
    """校验：查询非空、relevant_doc_ids 全部存在于知识库。返回问题列表。"""
    problems: list[str] = []
    for i, item in enumerate(dataset):
        if not item.query.strip():
            problems.append(f"第 {i + 1} 条: query 为空")
        for did in item.relevant_doc_ids:
            if did not in doc_ids_in_kb:
                problems.append(
                    f"第 {i + 1} 条: relevant_doc_id {did} 不存在于知识库"
                )
    return problems
