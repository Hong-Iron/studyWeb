"""Breadth-first crawler with domain scoping and hard limits.

Follows links from a seed URL, staying on-domain by default, capped by both
depth and page count so it can never run away."""

from __future__ import annotations

from collections import deque
from urllib.parse import urlsplit

from .fetch import fetch_page, Document


def _same_site(a: str, b: str) -> bool:
    ha, hb = urlsplit(a).netloc.lower(), urlsplit(b).netloc.lower()
    # treat www. and bare host as equal
    return ha.removeprefix("www.") == hb.removeprefix("www.")


def _host_in(host: str, allow: tuple[str, ...]) -> bool:
    """Domain-boundary match: 'danawa.com' matches 'danawa.com' and
    'x.danawa.com' but not 'notdanawa.com'."""
    host = host.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in allow)


def crawl(seed: str, *, depth: int = 1, max_pages: int = 20,
          same_domain: bool = True, allow_domains: list[str] | None = None,
          use_cache: bool = True) -> list[Document]:
    """Crawl outward from ``seed``.

    depth=0 fetches only the seed; depth=1 also fetches pages it links to, etc.
    Returns the fetched Documents in discovery order (seed first).
    """
    allow = tuple(d.lower().lstrip(".") for d in (allow_domains or []))
    seen: set[str] = set()
    docs: list[Document] = []
    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    seen.add(_norm(seed))

    while queue and len(docs) < max_pages:
        url, d = queue.popleft()
        doc = fetch_page(url, use_cache=use_cache)
        docs.append(doc)
        if not doc.ok or d >= depth:
            continue
        for link in doc.links:
            nu = _norm(link.url)
            if nu in seen:
                continue
            if allow:
                if not _host_in(urlsplit(link.url).netloc, allow):
                    continue
            elif same_domain and not _same_site(seed, link.url):
                continue
            seen.add(nu)
            queue.append((link.url, d + 1))
            if len(seen) >= max_pages * 4:  # keep the frontier bounded
                break
    return docs


def _norm(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc.lower()}{p.path.rstrip('/') or '/'}?{p.query}"
