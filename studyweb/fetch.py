"""Fetch a single URL and return a clean, structured Document."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

from . import net
from .extract import extract, Link


@dataclass
class Document:
    url: str
    final_url: str
    status: int
    title: str = ""
    text: str = ""
    markdown: str = ""
    passages: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    content_type: str = ""
    fetched_at: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and self.status == 200 and bool(self.text)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self, *, include_links: bool = True) -> dict:
        d = asdict(self)
        if not include_links:
            d.pop("links", None)
        else:
            d["links"] = [asdict(l) for l in self.links]
        return d


def fetch_page(url: str, *, use_cache: bool = True) -> Document:
    """Fetch and extract one URL. Never raises for network/extract issues —
    failures are reported on ``Document.error`` so batch callers keep going."""
    try:
        resp = net.get(url, use_cache=use_cache)
    except net.FetchError as exc:
        return Document(url=url, final_url=url, status=0, error=str(exc),
                        fetched_at=time.time())

    ctype = resp.content_type
    if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
        # Non-HTML (pdf, image, binary): don't try to parse as a web page.
        return Document(url=url, final_url=resp.url, status=resp.status,
                        content_type=ctype, error=f"unsupported content-type: {ctype}",
                        fetched_at=time.time())

    ex = extract(resp.content, resp.url, encoding=resp.declared_encoding)
    return Document(
        url=url, final_url=resp.url, status=resp.status,
        title=ex.title, text=ex.text, markdown=ex.markdown,
        passages=ex.passages, links=ex.links, meta=ex.meta,
        content_type=ctype, fetched_at=time.time(),
    )


def fetch_many(urls: list[str], *, use_cache: bool = True,
               max_workers: int | None = None) -> list[Document]:
    """Fetch many URLs concurrently (order preserved)."""
    def worker(u: str) -> Document:
        return fetch_page(u, use_cache=use_cache)
    out = net.get_many(urls, worker=worker, max_workers=max_workers)
    # get_many captures exceptions as values; normalise any stragglers.
    docs = []
    for u, r in zip(urls, out):
        if isinstance(r, Document):
            docs.append(r)
        else:
            docs.append(Document(url=u, final_url=u, status=0,
                                 error=f"{type(r).__name__}: {r}", fetched_at=time.time()))
    return docs
