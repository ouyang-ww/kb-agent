"""清洗器测试：归一化、样板去除、统计、代码块保留。"""

from kbagent.ingestion.cleaner import Cleaner


def test_line_ending_normalization():
    c = Cleaner()
    r = c.clean("a\r\nb\rc")
    assert r.text == "a\nb\nc"


def test_bom_and_zero_width_removed():
    c = Cleaner()
    r = c.clean("\ufeff标题\u200b内容")
    assert "\ufeff" not in r.text
    assert "\u200b" not in r.text


def test_boilerplate_removed_across_blocks():
    c = Cleaner()
    lines = []
    for i in range(10):
        lines.append(f"正文段落 {i} 的内容。")
        lines.append("页脚版权信息 v1.0")
    text = "\n\n".join(lines)
    r = c.clean(text)
    assert "页脚版权信息" not in r.text
    assert "正文段落" in r.text
    assert r.stats["removed_lines"] >= 10


def test_collapse_blank_lines():
    c = Cleaner()
    r = c.clean("a\n\n\n\n\nb")
    assert "\n\n\n" not in r.text


def test_code_block_content_preserved():
    c = Cleaner()
    text = "# 标题\n\n```python\nprint(  'hello'  )\n\n\nprint('world')\n```"
    r = c.clean(text)
    assert "print" in r.text
    assert "```" in r.text


def test_disabled_cleaner_passthrough():
    c = Cleaner(enabled=False)
    r = c.clean("  a\r\n\r\n\r\nb  ")
    assert r.text == "  a\r\n\r\n\r\nb  "
    assert r.stats["enabled"] is False


def test_stats_reported():
    c = Cleaner()
    r = c.clean("  你好  \n" * 10 + "\n\n\n结尾")
    assert r.stats["chars_before"] > 0
    assert r.stats["chars_after"] <= r.stats["chars_before"]
    assert "removed_chars" in r.stats
