"""概念名规范化。自动合并只认这一步加上精确别名，见 mvp §7.3。"""

from __future__ import annotations

import re
import unicodedata

_MARK = re.compile(r"[^\w]+", re.UNICODE)


def normalize_name(name: str) -> str:
    """NFKC、去空白、去标点、小写。

    中文没有大小写。空白和标点去掉之后，「检索增强生成」和带顿号的别名能对上。
    """
    text = unicodedata.normalize("NFKC", name or "")
    text = text.strip().casefold()
    text = _MARK.sub("", text)
    return text
