"""OCR：PaddleOCR（CPU、中文）可选开启，用于文字型图表/截图。"""

from __future__ import annotations

from ..errors import KBError


class OCREngine:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self._ocr = None

    def _lazy(self):
        if self._ocr is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as exc:  # pragma: no cover
                raise KBError(
                    "缺少 PaddleOCR，请安装: pip install kb-agent[vision]"
                ) from exc
            self._ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        return self._ocr

    def ocr_text(self, image_path: str) -> str:
        if not self.enabled:
            return ""
        try:
            ocr = self._lazy()
            result = ocr.ocr(image_path, cls=True)
            lines: list[str] = []
            for page in result or []:
                for line in page or []:
                    if line and len(line) >= 2:
                        lines.append(line[1][0])
            return "\n".join(lines)
        except Exception:
            return ""  # OCR 失败不影响主流程
