"""从源文件读取父文档窗口。

``KB_MAX_PARENT_CHARS`` 限制这次读入并返回的窗口，而不是 SQLite 里的父全文。
切块定位是清洗后文本中的字符区间，与摄入时使用同一套加载器和清洗器。
"""

from __future__ import annotations

from pathlib import Path

from ..ingestion.source_text import read_cleaned_source


def probe_window(full_text: str, chunk_text: str, max_chars: int) -> str:
    """没有定位时，用命中片段在正文中找窗口。"""
    if max_chars <= 0:
        return ""
    probe = (chunk_text or "")[:50].strip()
    idx = full_text.find(probe) if probe else -1
    if idx == -1:
        return full_text[:max_chars]
    half = max_chars // 2
    start = max(0, idx - half)
    return full_text[start : start + max_chars]


def slice_parent_window(
    text: str,
    *,
    start: int | None,
    end: int | None,
    max_chars: int,
    probe: str = "",
) -> str:
    """按切块定位截取，返回长度不超过 ``max_chars``。"""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if (
        start is not None
        and end is not None
        and 0 <= start < len(text)
        and end > start
    ):
        if end - start >= max_chars:
            return text[start : start + max_chars]
        half = max_chars // 2
        mid = (start + min(end, len(text))) // 2
        win = max(0, mid - half)
        if win + max_chars > len(text):
            win = max(0, len(text) - max_chars)
        if start < win:
            win = start
        if win + max_chars > len(text):
            win = max(0, len(text) - max_chars)
        return text[win : win + max_chars]
    return probe_window(text, probe, max_chars)


def read_parent_window(
    path: str | Path,
    *,
    start: int | None,
    end: int | None,
    max_chars: int,
    probe: str = "",
) -> str:
    """回读源文件并截取窗口。调用方不要把返回值写回父全文列。"""
    cleaned = read_cleaned_source(path)
    return slice_parent_window(
        cleaned,
        start=start,
        end=end,
        max_chars=max_chars,
        probe=probe,
    )
