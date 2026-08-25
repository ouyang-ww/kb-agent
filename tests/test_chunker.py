"""切块器测试：中英文、markdown 标题、块大小/重叠。"""

from kbagent.ingestion.chunker import Chunker


def test_simple_short_text_single_chunk():
    c = Chunker(chunk_size=800, overlap=100)
    chunks = c.chunk("doc1", "短文本。")
    assert len(chunks) == 1
    assert chunks[0].doc_id == "doc1"
    assert chunks[0].text == "短文本。"


def test_long_text_split_respects_size():
    c = Chunker(chunk_size=100, overlap=10)
    text = "。" .join(["这是第{}句话用于测试切块。".format(i) for i in range(50)])
    chunks = c.chunk("doc1", text)
    assert len(chunks) > 1
    for ch in chunks:
        assert len(ch.text) <= 100 + 10  # 允许 overlap 后缀


def test_markdown_headings_become_sections():
    c = Chunker(chunk_size=800, overlap=0)
    text = "# 第一章\n\n这是第一章内容。\n\n## 小节A\n\n小节A内容。"
    chunks = c.chunk("doc1", text)
    sections = {ch.section for ch in chunks}
    assert "第一章" in sections
    assert "第一章 / 小节A" in sections


def test_chunk_ids_sequential_and_parented():
    c = Chunker(chunk_size=50, overlap=0)
    text = "。".join(["内容" * 20] * 10)
    chunks = c.chunk("doc1", text)
    ids = [ch.chunk_id for ch in chunks]
    assert all(i.startswith("doc1#") for i in ids)
    assert len(set(ids)) == len(ids)


def test_empty_and_whitespace():
    c = Chunker(chunk_size=100, overlap=0)
    assert c.chunk("doc1", "") == []
    assert c.chunk("doc1", "\n\n  \n") == []


def test_english_text():
    c = Chunker(chunk_size=100, overlap=0)
    text = "word " * 200
    chunks = c.chunk("doc1", text)
    assert len(chunks) > 1
    assert all(ch.text.strip() for ch in chunks)
