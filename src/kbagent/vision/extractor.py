"""图片提取：markdown 图片引用 与 PDF 内嵌图片（PyMuPDF）。"""

from __future__ import annotations

import re
from pathlib import Path

_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _IMG_EXT


def extract_images_from_markdown(
    text: str, base_dir: str | Path
) -> list[dict]:
    """从 markdown 文本提取图片引用，解析为本地路径。返回 [{path, alt}]。"""
    out: list[dict] = []
    base = Path(base_dir)
    for m in _MD_IMG_RE.finditer(text):
        alt, raw = m.group(1), m.group(2).strip()
        if raw.startswith(("http://", "https://")):
            continue  # 远程图片暂不处理
        candidate = base / raw
        if not candidate.exists():
            # 尝试去掉 query/锚点
            candidate = base / raw.split("?")[0].split("#")[0]
        if candidate.exists() and _is_image(candidate):
            out.append({"path": str(candidate), "alt": alt, "page": None})
    return out


def extract_images_from_pdf(
    pdf_path: str | Path, out_dir: str | Path
) -> list[dict]:
    """提取 PDF 内嵌图片到 out_dir，返回 [{path, page}]。"""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError("缺少 PyMuPDF，请安装: pip install kb-agent[vision]") from exc

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    results: list[dict] = []
    seen: set[tuple] = set()
    for page_no in range(len(doc)):
        page = doc[page_no]
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in {s[0] for s in seen}:
                continue
            seen.add((xref,))
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha > 3:  # CMYK → RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                fname = out / f"page_{page_no + 1:03d}_img_{len(results):03d}.png"
                pix.save(str(fname))
                pix = None
                if fname.exists() and fname.stat().st_size > 0:
                    results.append({"path": str(fname), "page": page_no + 1})
            except Exception:
                continue
    doc.close()
    return results
