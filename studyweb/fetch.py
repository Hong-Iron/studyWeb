"""Fetch a single URL and return a clean, structured Document."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from urllib.parse import urlsplit

from . import engines, net
from .config import settings
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


def _fetch_once(url: str, *, use_cache: bool, engine: str | None) -> Document:
    """Fetch and extract through exactly one engine, no escalation."""
    try:
        resp = net.get(url, use_cache=use_cache, engine=engine, escalate=False)
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

    # A site adapter may override extraction to rescue fields the generic
    # pipeline drops (e.g. prices inside a configurator <form>).
    from . import siteadapters
    adapter = siteadapters.adapter_for(urlsplit(resp.url).netloc)
    if adapter is not None:
        try:
            override = adapter.extract(resp.url, resp.content, resp.declared_encoding)
            if override is not None:
                ex = override
        except Exception:  # noqa: BLE001 — never let an adapter break a fetch
            pass

    meta = dict(ex.meta)
    meta["fetch_engine"] = resp.engine
    return Document(
        url=url, final_url=resp.url, status=resp.status,
        title=ex.title, text=ex.text, markdown=ex.markdown,
        passages=ex.passages, links=ex.links, meta=meta,
        content_type=ctype, fetched_at=time.time(),
    )


def _should_escalate(doc: Document) -> bool:
    """True if a stronger engine could plausibly do better on this page.

    Deliberately stricter than :func:`studyweb.dataextract._looks_thin`, which
    runs only when a caller has already asked for structured data and is willing
    to pay for a browser. This one gates *every* fetch, so it fires on evidence
    that the transport failed — a wall, a network error, or an all-but-empty
    body that means a JS shell — and never merely because a page is short. A
    404 is an answer: no browser is going to invent the page.
    """
    if doc.status == 0 or doc.status in engines.BLOCK_STATUSES:
        return True
    if doc.status != 200:
        return False
    return doc.word_count < settings.escalate_thin_words


def fetch_page(url: str, *, use_cache: bool = True, engine: str | None = None,
               escalate: bool | None = None) -> Document:
    """Fetch and extract one URL. Never raises for network/extract issues —
    failures are reported on ``Document.error`` so batch callers keep going.

    Escalation is decided here rather than in :func:`studyweb.net.get` because
    the signal only exists after extraction: a JS shell answers 200 with real
    HTML, and it is the *extracted word count* that gives it away. When a page
    comes back thin, the stronger engines on the ladder are tried in turn and
    the fullest result wins. ``net.get`` still handles the other signal — an
    outright anti-bot wall — on its own.
    """
    doc = _fetch_once(url, use_cache=use_cache, engine=engine)
    if not (settings.fetch_escalate if escalate is None else escalate):
        return doc
    if not _should_escalate(doc):
        return doc

    best = doc
    # Escalate from the engine that actually ran, not the one we asked for: if
    # the configured start was unavailable, net.get already fell forward to a
    # stronger rung and re-trying it here would fetch the same page twice.
    ran = doc.meta.get("fetch_engine") or engine
    for eng in engines.escalation_targets(ran):
        alt = _fetch_once(url, use_cache=use_cache, engine=eng.name)
        if alt.ok and alt.word_count > best.word_count:
            alt.meta["escalated_from"] = doc.meta.get("fetch_engine", "") or "static"
            best = alt
            if not _should_escalate(best):
                break  # good enough; don't pay for a heavier engine
    return best


def fetch_many(urls: list[str], *, use_cache: bool = True,
               max_workers: int | None = None,
               engine: str | None = None) -> list[Document]:
    """Fetch many URLs concurrently (order preserved)."""
    def worker(u: str) -> Document:
        return fetch_page(u, use_cache=use_cache, engine=engine)
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
