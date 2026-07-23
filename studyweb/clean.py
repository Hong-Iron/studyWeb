"""Data-cleanse + chunking: turn fetched pages into RAG-ready records.

Pipeline stage names match the request:  strip -> clean -> chunk -> output.

    strip   : boilerplate/nav/cookie lines removed (extract.py already dropped
              the HTML chrome; here we scrub what leaks into text)
    clean   : Unicode NFKC, whitespace/hyphenation fixes, de-duplication
    chunk   : boundary-aware splitting with overlap + metadata
    output  : RAG records  {id, text, metadata}  /  LangChain {page_content, metadata}
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

# Lines that are almost always site chrome rather than study content.
_BOILERPLATE_RE = re.compile(
    r"^\s*("
    r"(accept|manage)\s+(all\s+)?cookies?|we\s+use\s+cookies|cookie\s+(policy|settings)|"
    r"subscribe( now)?|sign\s?up|log\s?in|sign\s?in|create an account|"
    r"share (this|on)|follow us|read more|related articles?|"
    r"advertisement|sponsored|©\s?\d{4}|all rights reserved|"
    r"skip to (main )?content|back to top|print this page|"
    r"이 사이트는 쿠키|쿠키를 사용|로그인|회원가입|구독|공유하기|광고|더 보기"
    r")\b.*$",
    re.I,
)
_NAV_LINE_RE = re.compile(r"^[\s>|•·\-–—*]+$")
_MULTISPACE = re.compile(r"[ \t ​‌‍﻿]+")


@dataclass
class Chunk:
    id: str
    text: str
    index: int
    source_url: str
    title: str = ""
    n_chars: int = 0
    n_tokens_est: int = 0
    meta: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        """Generic RAG record: {id, text, metadata}."""
        return {
            "id": self.id,
            "text": self.text,
            "metadata": {
                "source_url": self.source_url,
                "title": self.title,
                "chunk_index": self.index,
                "n_chars": self.n_chars,
                "n_tokens_est": self.n_tokens_est,
                **self.meta,
            },
        }

    def to_langchain(self) -> dict:
        """LangChain / LlamaIndex-style {page_content, metadata}."""
        rec = self.to_record()
        return {"page_content": self.text, "metadata": {"id": self.id, **rec["metadata"]}}


# --- clean ------------------------------------------------------------------

_BARE_URL_LINE = re.compile(r"^\s*<?https?://\S+>?\s*$")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_MARKS = re.compile(r"[*_`#]")


def flatten_markdown(text: str) -> str:
    """Markdown -> plain prose: drop images, turn ``[text](url)`` into ``text``,
    strip emphasis/heading marks. Keeps RAG chunks free of link soup."""
    text = _MD_IMG.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = re.sub(r"^>\s?", "", text, flags=re.M)   # blockquote markers
    return _MD_MARKS.sub("", text)


def normalize(text: str) -> str:
    """Unicode-normalise and repair whitespace/hyphenation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # join words split across a line break with a hyphen:  exam-\nple -> example
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = _MULTISPACE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_boilerplate(text: str) -> str:
    """Drop nav/cookie/social boilerplate and empty separator lines."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            out.append("")
            continue
        if _NAV_LINE_RE.match(s) or _BOILERPLATE_RE.match(s) or _BARE_URL_LINE.match(s):
            continue
        # very short, punctuation-heavy lines are usually menu items
        if len(s) < 3:
            continue
        out.append(line)
    return "\n".join(out)


def dedupe_lines(text: str) -> str:
    """Remove exact-duplicate non-trivial lines (keeps first occurrence)."""
    seen, out = set(), []
    for line in text.split("\n"):
        key = _MULTISPACE.sub(" ", line.strip().lower())
        if len(key) > 25:
            if key in seen:
                continue
            seen.add(key)
        out.append(line)
    return "\n".join(out)


def clean(text: str, *, flatten_md: bool = False) -> str:
    """Full cleanse: normalize -> (flatten markdown) -> strip boilerplate -> dedupe.

    Set ``flatten_md=True`` for RAG text so ``[label](url)`` link soup becomes
    plain prose before chunking and embedding."""
    t = normalize(text)
    if flatten_md:
        t = flatten_markdown(t)
    return re.sub(r"\n{3,}", "\n\n", dedupe_lines(strip_boilerplate(t))).strip()


def quality_score(text: str) -> float:
    """0..1 heuristic — low for menu/link soup, high for prose. Lets callers
    drop junk documents before embedding."""
    if not text:
        return 0.0
    letters = sum(c.isalpha() for c in text)
    ratio = letters / max(len(text), 1)
    words = text.split()
    avg_wlen = sum(len(w) for w in words) / max(len(words), 1)
    long_enough = min(len(words) / 200.0, 1.0)
    return round(min(ratio, 1.0) * 0.5 + long_enough * 0.4 + min(avg_wlen / 6, 1.0) * 0.1, 3)


# --- chunk ------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")


def _est_tokens(s: str) -> int:
    # cheap, model-agnostic estimate (~4 chars/token for mixed en/ko)
    return max(1, len(s) // 4)


def _split_long(paragraph: str, size: int) -> list[str]:
    if len(paragraph) <= size:
        return [paragraph]
    parts, cur = [], ""
    for sent in _SENT_SPLIT.split(paragraph):
        if cur and len(cur) + len(sent) + 1 > size:
            parts.append(cur.strip())
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if len(cur) > size:  # a single monster sentence: hard wrap
        parts.extend(cur[i:i + size] for i in range(0, len(cur), size))
    elif cur:
        parts.append(cur)
    return parts


def chunk_text(text: str, *, chunk_size: int = 1000, overlap: int = 150
               ) -> list[str]:
    """Boundary-aware chunking. Packs paragraphs up to ``chunk_size`` chars and
    carries ``overlap`` chars of tail context into the next chunk."""
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    pieces: list[str] = []
    for p in paras:
        pieces.extend(_split_long(p, chunk_size))

    chunks, cur = [], ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 2 > chunk_size:
            chunks.append(cur.strip())
            tail = cur[-overlap:] if overlap else ""
            cur = (tail + "\n\n" + piece) if tail else piece
        else:
            cur = (cur + "\n\n" + piece).strip() if cur else piece
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


# --- output -----------------------------------------------------------------

def to_chunks(doc, *, chunk_size: int = 1000, overlap: int = 150,
              min_quality: float = 0.25, use_markdown: bool = True) -> list[Chunk]:
    """Clean a fetched Document and split it into RAG-ready Chunks.

    ``doc`` is a studyweb.fetch.Document (or anything with .markdown/.text/.url/
    .title/.meta). Returns [] if the cleaned content is below ``min_quality``.
    """
    use_md = use_markdown and bool(getattr(doc, "markdown", ""))
    raw = doc.markdown if use_md else doc.text
    cleaned = clean(raw, flatten_md=use_md)
    if quality_score(cleaned) < min_quality:
        return []

    base_meta = dict(getattr(doc, "meta", {}) or {})
    base_meta.setdefault("host", base_meta.get("host", ""))
    if getattr(doc, "final_url", ""):
        base_meta["final_url"] = doc.final_url

    url = getattr(doc, "url", "")
    title = getattr(doc, "title", "")
    out = []
    for i, ctext in enumerate(chunk_text(cleaned, chunk_size=chunk_size, overlap=overlap)):
        cid = hashlib.sha1(f"{url}#{i}:{ctext[:64]}".encode("utf-8")).hexdigest()[:16]
        out.append(Chunk(
            id=cid, text=ctext, index=i, source_url=url, title=title,
            n_chars=len(ctext), n_tokens_est=_est_tokens(ctext),
            meta={"quality": quality_score(ctext), **base_meta},
        ))
    return out


def clean_document(doc, **kw) -> dict:
    """One-call cleanse for a single Document -> serialisable dict with chunks."""
    chunks = to_chunks(doc, **kw)
    return {
        "url": getattr(doc, "url", ""),
        "title": getattr(doc, "title", ""),
        "n_chunks": len(chunks),
        "chunks": [c.to_record() for c in chunks],
    }
