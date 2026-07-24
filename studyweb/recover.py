"""URL recovery — find the *intended* page when a requested URL 404s or fails.

Small models frequently hallucinate URLs (wrong path, made-up product id,
guessed slug). Instead of handing the model a bare 404, we:

  1. try a couple of cheap URL variants (www/scheme/trailing-slash typos), then
  2. derive keywords from the dead URL and search the SAME site (or the web) for
     real pages that match the intent, and
  3. open the best real candidate — returning it plus the alternatives.

This lets a hallucinated link still resolve to real content in one tool call,
while staying honest (the result is clearly labelled as recovered).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

log = logging.getLogger("studyweb.recover")

# Path/query tokens that carry no search intent (CMS scaffolding, TLDs, verbs).
_STOP_SEG = {
    "www", "http", "https", "view", "views", "list", "lists", "index", "page",
    "pages", "product", "products", "prod", "goods", "good", "item", "items",
    "detail", "details", "info", "category", "categories", "cate", "search",
    "php", "html", "htm", "asp", "aspx", "jsp", "do", "shop", "store", "mall",
    "kr", "com", "net", "org", "co", "go", "or", "id", "no", "idx", "uid",
    "seq", "num", "pcode", "code",
}


def url_keywords(url: str) -> list[str]:
    """Meaningful search tokens from a URL's path + query.

    Keeps model-code-like tokens ('esc4000') and words; drops pure numbers
    (hallucinated ids), TLDs, and CMS scaffolding words."""
    parts = urlsplit(url)
    raw = f"{parts.path} {parts.query}"
    out: list[str] = []
    seen: set[str] = set()
    for tok in re.split(r"[^0-9A-Za-z가-힣]+", raw):
        if not tok:
            continue
        low = tok.lower()
        if low in _STOP_SEG or low in seen:
            continue
        if tok.isdigit():            # pure numbers = ids, useless as query terms
            continue
        if len(tok) < 2:
            continue
        seen.add(low)
        out.append(tok)
    return out


def url_variants(url: str) -> list[str]:
    """Cheap near-miss variants: toggle www, toggle trailing slash, https."""
    parts = urlsplit(url)
    host, path = parts.netloc, parts.path
    cands: list[str] = []

    if parts.scheme == "http":
        cands.append(parts._replace(scheme="https").geturl())
    if host.startswith("www."):
        cands.append(parts._replace(netloc=host[4:]).geturl())
    elif host:
        cands.append(parts._replace(netloc="www." + host).geturl())
    last = path.rsplit("/", 1)[-1]
    if path.endswith("/") and len(path) > 1:
        cands.append(parts._replace(path=path.rstrip("/")).geturl())
    elif path and "." not in last:
        cands.append(parts._replace(path=path + "/").geturl())

    seen = {url}
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def recover_candidates(url: str, *, max_candidates: int = 5) -> list:
    """Real candidate pages that likely match a dead URL's intent.

    Searches the same site first (site_search — works for catalogs/adapters),
    then the web scoped to the domain, then the open web. Returns SearchResults.
    """
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    keywords = url_keywords(url)
    query = " ".join(keywords)
    if not query:
        return []  # nothing to go on (e.g. a pure-id URL) — caller should search

    # 1) within the same site
    if host:
        try:
            from .sitesearch import site_search
            res = site_search(host, query, max_results=max_candidates)
            if res:
                return res
        except Exception as exc:  # noqa: BLE001
            log.debug("site_search recovery failed for %s: %s", url, exc)

    # 2) web search scoped to the domain
    if host:
        try:
            from .search import search as _search
            res = _search(query, include_domains=[host], max_results=max_candidates)
            if res:
                return res
        except Exception as exc:  # noqa: BLE001
            log.debug("domain-scoped recovery failed for %s: %s", url, exc)

    # 3) open web (domain itself may be hallucinated) — bias with the brand word
    try:
        from .search import search as _search
        brand = host.split(".")[0] if host else ""
        q = f"{query} {brand}".strip()
        return _search(q, max_results=max_candidates)
    except Exception as exc:  # noqa: BLE001
        log.debug("open-web recovery failed for %s: %s", url, exc)
        return []


def open_best(url: str, *, max_candidates: int = 5):
    """Resolve a failing URL to a working Document.

    Returns ``(document | None, opened_url | None, candidates)``:
      * document is a fetched, ``.ok`` page (a variant hit or the best candidate)
      * candidates is the full recovery list (for offering alternatives)
    """
    from .fetch import fetch_page

    # 1) cheap variants (www/scheme/slash typos)
    for v in url_variants(url):
        d = fetch_page(v)
        if d.ok:
            return d, v, []

    # 2) search-based recovery
    candidates = recover_candidates(url, max_candidates=max_candidates)
    for c in candidates:
        d = fetch_page(c.url)
        if d.ok:
            return d, c.url, candidates
    return None, None, candidates
