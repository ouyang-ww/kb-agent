"""网络环境准备：剥离无效代理、禁用 HF xet、缓存命中时强制离线（网络加固）。

本机常见场景：shell 里残留 http_proxy/https_proxy 指向已失效的本地代理，
导致 LLM 与模型下载请求挂起。此模块在加载模型前清理这些变量；
模型已完整缓存在本地时直接进入离线模式，杜绝一切联网等待。
"""

from __future__ import annotations

import os
from pathlib import Path

_PROXY_KEYS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


def strip_proxy_env() -> None:
    """从进程环境移除代理变量（避免挂死的死代理影响直连服务）。"""
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)


def _hf_hub_dir() -> Path:
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    cache = os.environ.get("HF_HUB_CACHE")
    if cache:
        return Path(cache)
    return Path.home() / ".cache" / "huggingface" / "hub"


def model_cached(model_name: str) -> bool:
    """模型快照（refs + snapshots）是否已完整缓存。"""
    dir_name = "models--" + model_name.replace("/", "--")
    root = _hf_hub_dir() / dir_name
    return (root / "refs").exists() and (root / "snapshots").exists()


def prepare_hf_env(model_name: str | None = None) -> None:
    """准备 HF 环境：剥离代理、禁用 xet；模型已缓存则强制离线。

    注意：进程级修改（os.environ），调用后影响后续所有 HF 操作。
    """
    strip_proxy_env()
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    if model_name and model_cached(model_name):
        os.environ["HF_HUB_OFFLINE"] = "1"
