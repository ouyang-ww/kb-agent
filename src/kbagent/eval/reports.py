"""评估报告：JSON / Markdown 格式化输出。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runner import EvalResult


def result_to_dict(result: EvalResult) -> dict[str, Any]:
    return {
        "mode": result.mode,
        "config": result.config,
        "metrics": result.metrics,
        "warnings": result.warnings,
        "items": result.per_item,
    }


def save_json(result: EvalResult, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(result_to_dict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def compare_to_markdown(results: list[EvalResult]) -> str:
    lines = ["# RAG 评估对比报告\n"]
    headers = ["配置"]
    rows: list[list[str]] = []
    all_metrics = sorted(
        {m for r in results for m in r.metrics if not m.endswith("_ms")}
    )
    headers += all_metrics
    for r in results:
        cfg = "; ".join(f"{k}={v}" for k, v in r.config.items())
        rows.append([cfg] + [str(r.metrics.get(m, "-")) for m in all_metrics])
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    lines.append("| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |")
    lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in rows:
        lines.append("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(row)) + " |")
    for r in results:
        if r.warnings:
            lines.append(f"\n### 配置 {r.config} 告警")
            lines.extend(f"- {w}" for w in r.warnings)
    return "\n".join(lines)


def save_markdown(results: list[EvalResult], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(compare_to_markdown(results), encoding="utf-8")


def single_to_markdown(result: EvalResult) -> str:
    lines = ["# RAG 评估报告\n"]
    lines.append(f"- 模式：{result.mode}")
    lines.append(f"- 配置：{result.config}")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    for k, v in result.metrics.items():
        lines.append(f"| {k} | {v} |")
    if result.warnings:
        lines.append("\n## 告警")
        lines.extend(f"- {w}" for w in result.warnings)
    return "\n".join(lines)
