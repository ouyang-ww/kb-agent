"""加载器：从文件/目录读取原始文本（md/txt/pdf），计算内容哈希。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..errors import IngestError

SUPPORTED_EXTS = {".md", ".markdown", ".txt", ".text", ".pdf"}
_TEXT_EXTS = {".md", ".markdown", ".txt", ".text"}


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def doc_id_for(source_identity: str) -> str:
    """父文档 doc_id：基于来源身份（source_path 或 title）的稳定哈希。"""
    return hashlib.sha256(source_identity.encode("utf-8")).hexdigest()[:12]


def scan_path(path: Path, recursive: bool = True) -> list[Path]:
    path = Path(path)
    if path.is_file():
        return [path]
    if not path.exists():
        raise IngestError(f"路径不存在: {path}")
    files: list[Path] = []
    it = path.rglob("*") if recursive else path.glob("*")
    for p in sorted(it):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            files.append(p)
    return files


def load_file(path: Path) -> list[dict[str, Any]]:
    """返回 raw 文档列表：[{title, source_path, doc_type, content, metadata}]。"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in _TEXT_EXTS:
        text = path.read_text(encoding="utf-8", errors="replace")
        return [
            {
                "title": path.stem,
                "source_path": str(path),
                "doc_type": "text",
                "content": text,
                "metadata": {},
            }
        ]
    if ext == ".pdf":
        return _load_pdf(path)
    raise IngestError(f"不支持的文件类型: {ext}")


def _load_pdf(path: Path) -> list[dict[str, Any]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise IngestError("缺少 pypdf，请先安装: pip install pypdf") from exc
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise IngestError(f"PDF 解析失败 {path}: {exc}") from exc
    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            pages.append(f"[第{i + 1}页]\n{text}")
    if not pages:
        raise IngestError(f"PDF 无法提取文本（可能为扫描件）: {path}")
    return [
        {
            "title": path.stem,
            "source_path": str(path),
            "doc_type": "pdf",
            "content": "\n\n".join(pages),
            "metadata": {"pages": len(reader.pages)},
        }
    ]
