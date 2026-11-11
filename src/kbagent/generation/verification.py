"""闸 3：把答案拆成原子陈述，只按所引片段判断能否推出。

只在 ``KB_ANSWER_STRICT=true`` 时跑，默认关闭。没有 LLM key、也没有注入的
校验器时，跳过这一闸，不改闸 1 / 闸 2 的结果，也不抛错。

推不出的陈述从答案里删掉，并写入 gaps。失败率高于
``KB_ANSWER_STRICT_MAX_FAIL_RATE``（默认 0.5，用严格大于比较），
或一条都留不下来时，整题改成和闸 1 相同的固定拒答句。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..errors import LLMError
from .answerability import INSUFFICIENT_EVIDENCE
from .citations import _CITE

_SPLIT = re.compile(r"[^。！？!?；;\n]+[。！？!?；;\n]?")


@dataclass
class Claim:
    text: str
    source_ids: list[int]


@dataclass
class Verification:
    text: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    failure_rate: float | None = None
    downgraded: bool = False
    skipped: str | None = None

    def as_meta(self) -> dict[str, Any]:
        return {
            "enabled": self.skipped != "disabled",
            "skipped": self.skipped,
            "failure_rate": self.failure_rate,
            "downgraded": self.downgraded,
            "claims": list(self.claims),
            "gaps": list(self.gaps),
        }


class ClaimVerifier(Protocol):
    def verify_many(self, claims: list[Claim], snippets: list[str]) -> list[str]:
        """每条返回 entailed 或 not_entailed。无法判断时抛 LLMError，由调用方跳过整闸。"""


class SkipVerification(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LLMClaimVerifier:
    """一次调用判断全部陈述。输出认不出时跳过，不把答案清空。"""

    def __init__(self, llm) -> None:
        self.llm = llm

    def verify_many(self, claims: list[Claim], snippets: list[str]) -> list[str]:
        numbered = "\n\n".join(
            f"[{index + 1}] {text}" for index, text in enumerate(snippets)
        )
        listing = "\n".join(
            f"{index}. {claim.text}（引用 {claim.source_ids or '无'}）"
            for index, claim in enumerate(claims)
        )
        prompt = (
            "只根据下面的编号片段，判断每条陈述能否被它自己引用的片段推出。"
            "不能使用片段之外的知识。推不出就写 not_entailed。\n"
            "只输出 JSON："
            '{"results": [{"index": 0, "verdict": "entailed"|"not_entailed"}]}\n\n'
            f"片段：\n{numbered}\n\n陈述：\n{listing}"
        )
        try:
            payload = self.llm.chat_json(
                [
                    {
                        "role": "system",
                        "content": "你是陈述校验器。只输出 JSON，不要解释。",
                    },
                    {"role": "user", "content": prompt},
                ]
            )
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"声明校验失败: {exc}") from exc
        return _verdicts_from_payload(payload, len(claims))


def split_claims(text: str) -> list[Claim]:
    claims: list[Claim] = []
    for match in _SPLIT.finditer(text or ""):
        raw = match.group(0).strip()
        if not raw:
            continue
        ids: list[int] = []
        for number in _CITE.findall(raw):
            value = int(number)
            if value not in ids:
                ids.append(value)
        claims.append(Claim(text=raw, source_ids=ids))
    return claims


def verify_answer(
    text: str,
    snippets: list[str],
    verifier: ClaimVerifier | None,
    *,
    max_fail_rate: float,
) -> Verification:
    """``verifier`` 为空时由调用方先决定要不要跑。这里假定已经决定要跑。"""
    claims = split_claims(text)
    if not claims:
        return Verification(text=text, skipped="no_claims", failure_rate=0.0)

    verdicts: list[str] = []
    pending: list[Claim] = []
    pending_at: list[int] = []
    for index, claim in enumerate(claims):
        valid = [n for n in claim.source_ids if 1 <= n <= len(snippets)]
        if not valid:
            verdicts.append("not_entailed")
            continue
        claim.source_ids = valid
        verdicts.append("")
        pending.append(claim)
        pending_at.append(index)

    if pending:
        if verifier is None:
            return Verification(text=text, skipped="no_verifier")
        try:
            judged = list(verifier.verify_many(pending, snippets))
        except SkipVerification as exc:
            return Verification(text=text, skipped=exc.reason)
        except LLMError:
            return Verification(text=text, skipped="verifier_error")
        if len(judged) != len(pending):
            return Verification(text=text, skipped="verifier_error")
        for index, verdict in zip(pending_at, judged):
            verdicts[index] = "entailed" if verdict == "entailed" else "not_entailed"

    rows: list[dict[str, Any]] = []
    kept: list[str] = []
    gaps: list[str] = []
    failed = 0
    for claim, verdict in zip(claims, verdicts):
        rows.append(
            {"claim": claim.text, "source_ids": list(claim.source_ids), "verdict": verdict}
        )
        if verdict == "entailed":
            kept.append(claim.text)
        else:
            failed += 1
            gaps.append(claim.text)
    rate = failed / len(claims)
    downgrade = not kept or rate > float(max_fail_rate)
    if downgrade:
        return Verification(
            text=INSUFFICIENT_EVIDENCE,
            claims=rows,
            gaps=gaps,
            failure_rate=round(rate, 4),
            downgraded=True,
        )
    return Verification(
        text="\n".join(kept).strip(),
        claims=rows,
        gaps=gaps,
        failure_rate=round(rate, 4),
        downgraded=False,
    )


def _verdicts_from_payload(payload: Any, count: int) -> list[str]:
    rows = None
    if isinstance(payload, dict):
        rows = payload.get("results")
        if rows is None:
            rows = payload.get("verdicts")
    if not isinstance(rows, list):
        raise SkipVerification("unrecognized_verifier_output")
    verdicts = ["not_entailed"] * count
    seen = False
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= count:
            continue
        raw = str(item.get("verdict") or "").strip().lower()
        if raw not in {"entailed", "not_entailed"}:
            continue
        verdicts[index] = raw
        seen = True
    if not seen:
        raise SkipVerification("unrecognized_verifier_output")
    return verdicts
