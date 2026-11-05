"""加载器：从文件/目录读取原始文本（md/txt/pdf），计算内容哈希。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..errors import IngestError

SUPPORTED_EXTS = {".md", ".markdown", ".txt", ".text", ".pdf"}
_TEXT_EXTS = {".md", ".markdown", ".txt", ".text"}

# 存储和去重用完整 SHA256。旧库只存了前 12 hex（48 bit），碰撞时会误判重复。
HASH_HEX_LEN = 64
LEGACY_HASH_HEX_LEN = 12
_HEX = frozenset("0123456789abcdef")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(content: str) -> str:
    """规范化文本的完整 SHA256（64 hex）。入库、去重和校验都用它。"""
    return sha256_hex(content)


def doc_id_for(source_identity: str) -> str:
    """父文档 doc_id：来源身份的完整 SHA256（64 hex）。"""
    return sha256_hex(source_identity)


def hash_display(value: str) -> str:
    """展示用短哈希（前 12 hex）。不能当作主键或去重键。"""
    text = (value or "").strip().lower()
    if len(text) <= LEGACY_HASH_HEX_LEN:
        return text
    return text[:LEGACY_HASH_HEX_LEN]


def is_legacy_hash(value: str) -> bool:
    """旧库写入的 12 hex 截断。空串和完整 64 hex 都不是。"""
    text = (value or "").strip().lower()
    return len(text) == LEGACY_HASH_HEX_LEN and all(ch in _HEX for ch in text)


def hashes_equal(stored: str, full: str) -> bool:
    """完整哈希相等，或其中一边是另一边的旧 12 hex 前缀。

    只用于核对「这份旧记录是不是这篇正文」。去重跳过必须走完整哈希，
    不能只靠这个前缀判断，否则 48 bit 碰撞仍会漏入库。
    """
    left = (stored or "").strip().lower()
    right = (full or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    if is_legacy_hash(left) and len(right) == HASH_HEX_LEN and right.startswith(left):
        return True
    if is_legacy_hash(right) and len(left) == HASH_HEX_LEN and left.startswith(right):
        return True
    return False


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
