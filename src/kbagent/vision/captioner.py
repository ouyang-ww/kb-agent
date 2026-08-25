"""图片描述：视觉 LLM API（OpenAI 兼容 image_url）为主，本地模型可插拔。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..errors import KBError
from .extractor import _is_image

_CAPTION_PROMPT = (
    "请用中文简要描述这张图片/图表的内容。如果是图表，说明其主题、坐标含义和关键数据；"
    "如果包含文字，请完整转述其中的文字信息。控制在 200 字以内。"
)


class Captioner(Protocol):
    def caption(self, image_path: str, hint: str = "") -> str: ...


class VisionAPICaptioner:
    """通过 OpenAI 兼容视觉接口生成图片描述。"""

    def __init__(self, llm, model: str | None = None) -> None:
        self.llm = llm
        self.model = model

    def caption(self, image_path: str, hint: str = "") -> str:
        if not Path(image_path).exists():
            raise KBError(f"图片不存在: {image_path}")
        if not _is_image(image_path):
            raise KBError(f"不支持的图片格式: {image_path}")
        prompt = _CAPTION_PROMPT
        if hint:
            prompt = f"{prompt}\n上下文提示：{hint}"
        return self.llm.vision_chat(prompt, [image_path], model=self.model)


class LocalCaptioner:
    """本地 Florence-2（CPU 可跑，离线）。需 pip install kb-agent[local-vision]。"""

    def __init__(self, model_name: str = "microsoft/Florence-2-base") -> None:
        self.model_name = model_name
        self._pipe = None

    def _lazy(self):
        if self._pipe is None:
            try:
                from transformers import AutoModelForCausalLM, AutoProcessor
                import torch
            except ImportError as exc:  # pragma: no cover
                raise KBError(
                    "缺少 transformers，请安装: pip install kb-agent[local-vision]"
                ) from exc
            self._processor = AutoProcessor.from_pretrained(
                self.model_name, trust_remote_code=True
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                self.model_name, trust_remote_code=True
            )
        return self._model, self._processor

    def caption(self, image_path: str, hint: str = "") -> str:
        import torch
        from PIL import Image

        model, processor = self._lazy()
        image = Image.open(image_path).convert("RGB")
        prompt = "<MORE_DETAILED_CAPTION>"
        inputs = processor(text=prompt, images=image, return_tensors="pt")
        with torch.no_grad():
            generated = model.generate(
                **inputs, max_new_tokens=1024, num_beams=3
            )
        return processor.batch_decode(generated, skip_special_tokens=True)[0]
