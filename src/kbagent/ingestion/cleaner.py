"""清洗器：loader 之后的数据清洗管道（归一化、样板去除、去噪、统计）。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# 连续重复的装饰线（--- / *** / ___ 等）
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
# 图片行（markdown 图片保留，供多模态管线使用，不删除）
_IMG_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
# HTML 注释
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass
class CleanResult:
    text: str
    stats: dict = field(default_factory=dict)


class Cleaner:
    """清洗管道，各步骤可独立开关。

    - 换行/空白归一化（\\r\\n → \\n、BOM、零宽字符、控制字符）
    - 去除 HTML 注释与装饰线（保留代码块/表格/图片行）
    - 重复页眉/页脚启发式剔除（跨段落高频重复行）
    - 冗余空行合并、首尾空白清理
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def clean(self, text: str) -> CleanResult:
        before = len(text)
        if not self.enabled:
            return CleanResult(text, {"enabled": False, "chars_before": before, "chars_after": before})

        # 1. 换行归一
        t = text.replace("\r\n", "\n").replace("\r", "\n")
        # 2. BOM / 零宽字符
        for ch in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u200e", "\u200f"):
            t = t.replace(ch, "")
        # 3. 控制字符清理（保留 \n \t）
        t = "".join(ch for ch in t if ch in "\n\t" or ord(ch) >= 32)
        # 4. 行尾空白
        lines = [ln.rstrip() for ln in t.split("\n")]

        # 5. 逐行过滤样板
        kept: list[str] = []
        removed = 0
        for ln in lines:
            s = ln.strip()
            if not s:
                kept.append("")
                continue
            if _COMMENT_RE.fullmatch(s) or _COMMENT_RE.search(s) and s.startswith("<!--"):
                removed += 1
                continue
            if _HR_RE.match(s) and kept and kept[-1].strip():
                # 装饰线：若在文档最前（可能是 yaml front matter 边界）则保留
                removed += 1
                continue
            if _IMG_RE.match(s):
                kept.append(ln)  # 图片行保留（多模态）
                continue
            kept.append(ln)

        # 6. 重复页眉/页脚剔除（启发式：跨 ≥40% 段落的短行）
        t2 = "\n".join(kept)
        blocks = [b for b in re.split(r"\n\s*\n", t2) if b.strip()]
        if len(blocks) >= 5:
            line_counts: Counter = Counter()
            for b in blocks:
                for ln in set(b.split("\n")):
                    line_counts[ln.strip()] += 1
            threshold = max(2, int(len(blocks) * 0.4))
            boilerplate = {
                ln for ln, cnt in line_counts.items()
                if cnt >= threshold and 0 < len(ln) <= 80 and ln
            }
            if boilerplate:
                out_lines: list[str] = []
                for ln in t2.split("\n"):
                    if ln.strip() in boilerplate:
                        removed += 1
                        continue
                    out_lines.append(ln)
                t2 = "\n".join(out_lines)

        # 7. 冗余空行合并（3+ → 2），首尾空行清理
        t3 = re.sub(r"\n{3,}", "\n\n", t2).strip("\n")
        removed_chars = before - len(t3)
        return CleanResult(
            t3,
            {
                "enabled": True,
                "chars_before": before,
                "chars_after": len(t3),
                "removed_chars": removed_chars,
                "removed_lines": removed,
            },
        )
