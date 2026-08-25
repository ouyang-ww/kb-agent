"""LLM 客户端：任意 OpenAI 兼容服务（chat / chat_json / 多模态消息）。"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

from ..errors import LLMError


def image_to_data_url(image_path: str | Path) -> str:
    """本地图片 → base64 data URL（供 OpenAI 兼容 vision 接口使用）。"""
    p = Path(image_path)
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    data = base64.b64encode(p.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{data}"


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "deepseek-chat",
        temperature: float = 0.2,
        max_tokens: int = 1024,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        try:
            import httpx
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("缺少 openai SDK，请先安装: pip install openai") from exc
        # trust_env=False：忽略 http_proxy/https_proxy 环境变量，直连 API
        # （代理转发 LLM 流量常导致连接卡死；如确需代理，可自行注入 http_client）
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key or "not-set",
            timeout=timeout,
            http_client=httpx.Client(trust_env=False, timeout=timeout),
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=(
                    temperature if temperature is not None else self.temperature
                ),
                max_tokens=max_tokens or self.max_tokens,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
    ) -> dict[str, Any]:
        text = self.chat(messages, temperature=temperature if temperature is not None else 0.0)
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        t = text.strip()
        if t.startswith("```"):
            t = t.strip("`")
            if t.startswith("json"):
                t = t[4:].strip()
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end == -1:
            raise LLMError(f"LLM 未返回 JSON: {text[:200]}")
        try:
            obj = json.loads(t[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM JSON 解析失败: {exc}") from exc
        if not isinstance(obj, dict):
            raise LLMError("LLM JSON 不是对象")
        return obj

    def vision_chat(
        self,
        text: str,
        image_paths: list[str | Path],
        model: str | None = None,
    ) -> str:
        """多模态消息：文本 + 本地图片（base64 data URL）。"""
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for p in image_paths:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_to_data_url(p)},
                }
            )
        try:
            resp = self._client.chat.completions.create(
                model=model or self.model,
                messages=[{"role": "user", "content": content}],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            raise LLMError(f"视觉 LLM 调用失败: {exc}") from exc
