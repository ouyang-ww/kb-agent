"""同步许可。默认全部拒绝。

远程加密同步和同 Wi-Fi 配对同步是两条通道，许可互不包含。
健康数据进入远程通道要单独许可。原件全文不走应用内同步。
这里只保存许可，不建立连接，也不传输内容。见 PLAN §0.1。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..errors import KBError

CHANNEL_REMOTE = "remote"
CHANNEL_LAN = "lan"
CHANNELS = (CHANNEL_REMOTE, CHANNEL_LAN)

GRANT_REMOTE = "remote"
GRANT_LAN = "lan"
GRANT_HEALTH_REMOTE = "health_remote"
GRANT_NAMES = (GRANT_REMOTE, GRANT_LAN, GRANT_HEALTH_REMOTE)

KIND_METADATA = "metadata"
KIND_SUMMARY = "summary"
KIND_CHUNKS = "chunks"
KIND_EMBEDDINGS = "embeddings"
KIND_STUDY = "study"
KIND_TIMELINE = "timeline"
KIND_HEALTH = "health"
KIND_ORIGINAL = "original"

# 通道打开后，将来可以纳入的索引与档案。不含健康，也不含原件全文。
CHANNEL_KINDS = frozenset(
    {
        KIND_METADATA,
        KIND_SUMMARY,
        KIND_CHUNKS,
        KIND_EMBEDDINGS,
        KIND_STUDY,
        KIND_TIMELINE,
    }
)

_GRANT_ALIASES = {
    "remote": GRANT_REMOTE,
    "lan": GRANT_LAN,
    "wifi": GRANT_LAN,
    "wi-fi": GRANT_LAN,
    "health-remote": GRANT_HEALTH_REMOTE,
    "health_remote": GRANT_HEALTH_REMOTE,
}


def empty_grants() -> dict[str, bool]:
    return {name: False for name in GRANT_NAMES}


def normalize_grant_name(name: str) -> str:
    key = _GRANT_ALIASES.get((name or "").strip().lower())
    if key is None:
        raise KBError("未知的同步许可。可选：remote、lan、health-remote")
    return key


def allows(grants: Mapping[str, bool], channel: str, kind: str) -> bool:
    """未许可、未知种类、原件全文都拒绝。

    健康数据进入远程通道时，远程许可和 health_remote 必须同时为真。
    局域网通道的健康数据随该通道自己的许可，不打开远程通道。
    """
    key = (channel or "").strip().lower()
    if key in {"wifi", "wi-fi"}:
        key = CHANNEL_LAN
    if key not in CHANNELS:
        return False
    kind_name = (kind or "").strip().lower()
    if kind_name == KIND_ORIGINAL:
        return False
    if kind_name == KIND_HEALTH:
        if key == CHANNEL_REMOTE:
            return bool(grants.get(GRANT_REMOTE)) and bool(
                grants.get(GRANT_HEALTH_REMOTE)
            )
        return bool(grants.get(GRANT_LAN))
    if kind_name not in CHANNEL_KINDS:
        return False
    return bool(grants.get(key))
