"""跨设备稳定 id。

记录的主键在设备之间保持不变。某一台电脑的绝对路径只是本机映射，
不能当作主键。见 ADR 0002。
"""

from __future__ import annotations

import re
import secrets

from ..errors import KBError

KIND_DEVICE = "device"
KIND_VAULT = "vault"
KIND_RECORD = "record"

_PREFIX = {
    KIND_DEVICE: "dev",
    KIND_VAULT: "vlt",
    KIND_RECORD: "rec",
}

_ID = re.compile(r"^(dev|vlt|rec)_([0-9a-f]{32})$")
_KIND_BY_PREFIX = {prefix: kind for kind, prefix in _PREFIX.items()}


def mint_stable_id(kind: str) -> str:
    """生成一次性的稳定 id。不读取路径、主机名或文件内容。"""
    prefix = _PREFIX.get(kind)
    if prefix is None:
        raise KBError(f"未知的稳定 id 类型: {kind}")
    return f"{prefix}_{secrets.token_hex(16)}"


def looks_like_path(value: str) -> bool:
    """绝对路径、盘符路径，或带目录分隔符的字符串。相对记录位置另作校验。"""
    text = (value or "").strip()
    if not text:
        return False
    if text[:1] in {"/", "~", ".", "\\"}:
        return True
    if "/" in text or "\\" in text:
        return True
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def parse_stable_id(value: str, kind: str | None = None) -> str:
    """只接受本模块发出的 id。路径和文档内容哈希都不是稳定 id。"""
    text = (value or "").strip()
    if looks_like_path(text):
        raise KBError("稳定 id 不能是本机路径")
    match = _ID.fullmatch(text)
    if match is None:
        raise KBError(
            "稳定 id 必须是 dev_、vlt_ 或 rec_ 加 32 位十六进制，不能用路径或文档哈希"
        )
    found = _KIND_BY_PREFIX[match.group(1)]
    if kind is not None and found != kind:
        expected = _PREFIX.get(kind)
        if expected is None:
            raise KBError(f"未知的稳定 id 类型: {kind}")
        raise KBError(f"稳定 id 类型不符，需要 {expected}_")
    return text


def stable_kind(value: str) -> str:
    text = parse_stable_id(value)
    return _KIND_BY_PREFIX[text.split("_", 1)[0]]
