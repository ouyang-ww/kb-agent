"""闸 2：引用契约。提示词约束模型，生成后再检查编号是否落在检索集合里。

不拆原子陈述、不做蕴含判断。那是闸 3（``KB_ANSWER_STRICT``，P2）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .answerability import INSUFFICIENT_EVIDENCE, is_refusal_text

CITATION_SYSTEM_PROMPT = (
    "你是一个个人知识库助手。只能使用给定的编号来源，不得使用外部知识或参数记忆。\n"
    "要求：\n"
    "1. 只能使用下面编号的参考资料，不得引入资料之外的事实。\n"
    "2. 每个事实句必须带 [n]，n 是参考资料的序号。\n"
    "3. 资料不足时不要自由发挥，只回答这一句："
    f"{INSUFFICIENT_EVIDENCE}\n"
    "4. 正文结束后另起一行，输出机器可读自检，格式严格为：\n"
    'SELF_CHECK {"used_sources": [1], "confidence": "high|medium|low", '
    '"gaps": [], "unsupported": []}\n'
)

_CITE = re.compile(r"\[(\d{1,2})\]")
_SELF_LINE = re.compile(r"(?m)^SELF_CHECK\s*(\{[^\n]*\})\s*$")
_LEVELS = ("high", "medium", "low")


@dataclass
class CitationCheck:
    text: str
    used_sources: list[int] = field(default_factory=list)
    stripped: list[int] = field(default_factory=list)
    self_check: dict[str, Any] | None = None
    forced_refusal: bool = False
    confidence_steps: int = 0

    def as_meta(self) -> dict[str, Any]:
        return {
            "used_sources": list(self.used_sources),
            "stripped": list(self.stripped),
            "self_check": self.self_check,
            "forced_refusal": self.forced_refusal,
        }


def downgrade_confidence(level: str, steps: int) -> str:
    if level not in _LEVELS:
        level = "low"
    index = _LEVELS.index(level)
    return _LEVELS[min(len(_LEVELS) - 1, index + max(0, steps))]


def apply_citation_contract(text: str, source_count: int) -> CitationCheck:
    """去掉不存在的引用编号。全部编造或一个有效引用都没有时，改成固定拒答。"""
    body = (text or "").strip()
    self_check = None
    match = None
    for match in _SELF_LINE.finditer(body):
        pass
    if match is not None:
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            self_check = parsed
        body = (body[: match.start()] + body[match.end() :]).strip()

    valid = set(range(1, max(0, source_count) + 1))
    found = [int(n) for n in _CITE.findall(body)]
    stripped = [n for n in found if n not in valid]
    used = sorted({n for n in found if n in valid})

    def _keep(item: re.Match[str]) -> str:
        number = int(item.group(1))
        return item.group(0) if number in valid else ""

    cleaned = _CITE.sub(_keep, body)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned).strip()

    already_refusal = is_refusal_text(cleaned)
    forced = False
    steps = 0
    if stripped and not used and not already_refusal:
        forced = True
        cleaned = INSUFFICIENT_EVIDENCE
    elif not used and not already_refusal:
        forced = True
        cleaned = INSUFFICIENT_EVIDENCE
    elif stripped and used:
        steps = 1

    return CitationCheck(
        text=cleaned,
        used_sources=used,
        stripped=stripped,
        self_check=self_check,
        forced_refusal=forced,
        confidence_steps=steps,
    )
