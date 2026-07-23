"""Data-cleanse and RAG chunking edge cases."""

from dataclasses import dataclass, field

from studyweb.clean import (normalize, strip_boilerplate, dedupe_lines, clean,
                            quality_score, chunk_text, to_chunks, Chunk,
                            flatten_markdown)


def test_flatten_markdown_and_bare_urls():
    md = ("[Photosynthesis](https://x/y) is a **process** in `plants`.\n"
          "https://en.wikipedia.org/wiki/Protection\n"
          "![img](https://x/i.png)")
    flat = flatten_markdown(md)
    assert "Photosynthesis is a process in plants." in flat
    assert "](http" not in flat and "**" not in flat
    # bare-url line dropped by the RAG cleanse
    cleaned = clean(md, flatten_md=True)
    assert "wiki/Protection" not in cleaned
    assert "Photosynthesis is a process" in cleaned


def test_normalize_hyphenation_and_whitespace():
    assert normalize("exam-\nple") == "example"
    assert normalize("a\r\nb") == "a\nb"
    assert normalize("x    y\t\tz") == "x y z"
    assert normalize("") == ""


def test_strip_boilerplate_en_and_ko():
    text = "We use cookies to improve.\nReal sentence one.\n로그인\nReal sentence two.\nSubscribe now"
    out = strip_boilerplate(text)
    assert "Real sentence one." in out
    assert "Real sentence two." in out
    assert "cookies" not in out
    assert "로그인" not in out
    assert "Subscribe" not in out


def test_dedupe_lines():
    text = "This is a long enough line to dedupe.\nThis is a long enough line to dedupe.\nkeep short"
    out = dedupe_lines(text)
    assert out.count("This is a long enough line to dedupe.") == 1


def test_quality_score_prose_vs_junk():
    prose = "Photosynthesis is the process by which plants convert light into chemical energy. " * 5
    junk = "1 2 3 | | | >> << -- -- 404 500 :: :: ::"
    assert quality_score(prose) > 0.6
    assert quality_score(junk) < 0.4
    assert quality_score("") == 0.0


def test_chunk_text_basic_and_overlap():
    para = "Sentence number {}. ".format
    text = "\n\n".join(" ".join(para(i) for i in range(20)) for _ in range(6))
    chunks = chunk_text(text, chunk_size=300, overlap=50)
    assert len(chunks) >= 2
    assert all(len(c) <= 300 + 60 for c in chunks)  # allow overlap slack


def test_chunk_text_long_single_paragraph_split():
    long_para = "word " * 1000  # one giant paragraph, no blank lines
    chunks = chunk_text(long_para, chunk_size=400, overlap=0)
    assert len(chunks) > 1
    assert all(c.strip() for c in chunks)


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


@dataclass
class FakeDoc:
    url: str = "https://example.com/a"
    final_url: str = "https://example.com/a"
    title: str = "T"
    markdown: str = ""
    text: str = ""
    meta: dict = field(default_factory=dict)


def test_to_chunks_records_and_stable_ids():
    body = ("Photosynthesis converts sunlight into chemical energy stored in glucose. "
            "It occurs in chloroplasts and is vital to life on Earth. ") * 6
    doc = FakeDoc(markdown=body, meta={"host": "example.com"})
    chunks = to_chunks(doc, chunk_size=300, overlap=40)
    assert chunks and all(isinstance(c, Chunk) for c in chunks)
    rec = chunks[0].to_record()
    assert set(rec) == {"id", "text", "metadata"}
    assert rec["metadata"]["source_url"] == doc.url
    assert rec["metadata"]["chunk_index"] == 0
    # deterministic ids
    again = to_chunks(doc, chunk_size=300, overlap=40)
    assert [c.id for c in chunks] == [c.id for c in again]


def test_to_chunks_drops_low_quality():
    doc = FakeDoc(markdown="| | | 1 2 3 >> << nav menu footer ad ad")
    assert to_chunks(doc, min_quality=0.5) == []


def test_chunk_langchain_shape():
    doc = FakeDoc(markdown="Meaningful sentence one here. Meaningful sentence two here. " * 8)
    ch = to_chunks(doc, chunk_size=200)[0]
    lc = ch.to_langchain()
    assert "page_content" in lc and "metadata" in lc and "id" in lc["metadata"]
