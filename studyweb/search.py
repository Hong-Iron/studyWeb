"""Pluggable web search.

No-key providers (work out of the box):
    * bing       — scrapes Bing's HTML SERP, decodes redirect URLs
    * wikipedia  — MediaWiki search API (great for study topics)
    * searxng    — any SearXNG instance you point STUDYWEB_SEARXNG_URL at

Key-based providers (used automatically when the env key is present):
    * brave, tavily, serpapi, google_cse

`provider="auto"` walks a sensible order and returns the first non-empty result
set, so the tool always answers even if one backend is rate-limited.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, asdict
from urllib.parse import urlsplit, parse_qs

from lxml import html as LH

from .config import settings
from . import net

log = logging.getLogger("studyweb.search")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""          # which provider produced it
    score: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class SearchError(Exception):
    pass


# --- helpers ----------------------------------------------------------------

def _decode_bing_url(href: str) -> str:
    """Bing wraps results in /ck/a redirects; the real URL is base64 in `u`."""
    if "bing.com/ck/a" not in href:
        return href
    u = parse_qs(urlsplit(href).query).get("u", [""])[0]
    if u.startswith("a1"):
        b = u[2:] + "=" * (-len(u[2:]) % 4)
        try:
            return base64.urlsafe_b64decode(b).decode("utf-8", "replace")
        except Exception:
            return href
    return href


def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
    seen, out = set(), []
    for r in results:
        key = net.normalize_url(r.url)
        if key in seen or not r.url.startswith("http"):
            continue
        seen.add(key)
        out.append(r)
    return out


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    """True if ``host`` is one of ``domains`` or a subdomain of one. Matches on
    a domain boundary so "danawa.com" does NOT match "notdanawa.com"."""
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


# --- providers --------------------------------------------------------------

def _bing(query: str, n: int, market: str | None = None) -> list[SearchResult]:
    params = {"q": query, "count": max(n, 10)}
    mkt = settings.search_market if market is None else market
    if mkt:
        params["mkt"] = mkt
        params["setlang"] = mkt.split("-", 1)[0]
    r = net.get("https://www.bing.com/search", params=params,
                check_robots=False, use_cache=True)
    doc = LH.fromstring(r.text)
    out = []
    for li in doc.xpath('//li[contains(@class,"b_algo")]'):
        a = li.xpath('.//h2/a')
        if not a:
            continue
        a = a[0]
        url = _decode_bing_url(a.get("href", ""))
        snip = li.xpath('.//div[contains(@class,"b_caption")]//p') or li.xpath('.//p')
        out.append(SearchResult(
            title=(a.text_content() or "").strip(),
            url=url,
            snippet=(snip[0].text_content().strip() if snip else ""),
            source="bing",
        ))
        if len(out) >= n:
            break
    if not out and _looks_blocked(r.text):
        raise SearchError("bing returned a consent/CAPTCHA page (no results parsed)")
    return out


# Markers that a SERP is a consent wall or bot check rather than real results.
_BLOCK_MARKERS = ("captcha", "unusual traffic", "verify you are human",
                  "are you a robot", "consent.bing", "/tou/", "before you continue")


def _looks_blocked(html_text: str) -> bool:
    low = (html_text or "")[:20000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


def _decode_ddg_url(href: str) -> str:
    """DuckDuckGo HTML wraps results in /l/?uddg=<url-encoded> redirects."""
    if "uddg=" not in href:
        return href if href.startswith("http") else "https:" + href if href.startswith("//") else href
    from urllib.parse import unquote
    u = parse_qs(urlsplit(href).query).get("uddg", [""])[0]
    return unquote(u) or href


def _duckduckgo(query: str, n: int) -> list[SearchResult]:
    """No-key general provider via DuckDuckGo's HTML endpoint. Gives ``auto`` a
    real fallback when Bing serves a consent wall."""
    r = net.get("https://html.duckduckgo.com/html/", params={"q": query},
                check_robots=False, use_cache=True)
    doc = LH.fromstring(r.text)
    out = []
    for a in doc.xpath('//a[contains(@class,"result__a")]'):
        url = _decode_ddg_url(a.get("href", ""))
        if not url.startswith("http"):
            continue
        snip = a.xpath('ancestor::div[contains(@class,"result")]//a[contains(@class,"result__snippet")]')
        out.append(SearchResult(
            title=(a.text_content() or "").strip(),
            url=url,
            snippet=(snip[0].text_content().strip() if snip else ""),
            source="duckduckgo",
        ))
        if len(out) >= n:
            break
    return out


def _wikipedia(query: str, n: int, lang: str | None = None) -> list[SearchResult]:
    if lang is None:
        # Derive from the configured market, e.g. "ko-KR" -> "ko".
        lang = (settings.search_market or "en").split("-", 1)[0].lower() or "en"
    r = net.get(f"https://{lang}.wikipedia.org/w/api.php", params={
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": n, "srprop": "snippet",
    }, check_robots=False)
    data = json.loads(r.text)
    out = []
    for hit in data.get("query", {}).get("search", []):
        title = hit["title"]
        slug = title.replace(" ", "_")
        snippet = LH.fromstring("<x>" + hit.get("snippet", "") + "</x>").text_content()
        out.append(SearchResult(
            title=title,
            url=f"https://{lang}.wikipedia.org/wiki/{slug}",
            snippet=snippet.strip(), source="wikipedia",
        ))
    return out


def _searxng(query: str, n: int) -> list[SearchResult]:
    if not settings.searxng_url:
        raise SearchError("SEARXNG_URL not configured")
    base = settings.searxng_url.rstrip("/")
    r = net.get(f"{base}/search", params={"q": query, "format": "json"},
                check_robots=False)
    data = json.loads(r.text)
    return [SearchResult(title=it.get("title", ""), url=it.get("url", ""),
                         snippet=it.get("content", ""), source="searxng")
            for it in data.get("results", [])[:n]]


def _brave(query: str, n: int) -> list[SearchResult]:
    if not settings.brave_api_key:
        raise SearchError("BRAVE_API_KEY not set")
    resp = net.session().get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": n},
        headers={"X-Subscription-Token": settings.brave_api_key,
                 "Accept": "application/json"},
        timeout=settings.timeout)
    _raise_for_status(resp, "brave")
    data = resp.json()
    return [SearchResult(title=it.get("title", ""), url=it.get("url", ""),
                         snippet=it.get("description", ""), source="brave")
            for it in data.get("web", {}).get("results", [])[:n]]


def _raise_for_status(resp, provider: str) -> None:
    """Turn a non-2xx keyed-provider response into a clear SearchError instead
    of an empty result set that looks like 'no hits'."""
    if resp.status_code >= 400:
        body = (resp.text or "")[:200]
        raise SearchError(f"{provider} HTTP {resp.status_code}: {body}")


def _tavily(query: str, n: int) -> list[SearchResult]:
    if not settings.tavily_api_key:
        raise SearchError("TAVILY_API_KEY not set")
    resp = net.session().post("https://api.tavily.com/search", json={
        "api_key": settings.tavily_api_key, "query": query, "max_results": n,
    }, timeout=settings.timeout)
    _raise_for_status(resp, "tavily")
    data = resp.json()
    return [SearchResult(title=it.get("title", ""), url=it.get("url", ""),
                         snippet=it.get("content", ""), source="tavily",
                         score=it.get("score"))
            for it in data.get("results", [])[:n]]


def _serpapi(query: str, n: int) -> list[SearchResult]:
    if not settings.serpapi_api_key:
        raise SearchError("SERPAPI_API_KEY not set")
    resp = net.session().get("https://serpapi.com/search", params={
        "q": query, "num": n, "api_key": settings.serpapi_api_key, "engine": "google",
    }, timeout=settings.timeout)
    _raise_for_status(resp, "serpapi")
    data = resp.json()
    return [SearchResult(title=it.get("title", ""), url=it.get("link", ""),
                         snippet=it.get("snippet", ""), source="serpapi")
            for it in data.get("organic_results", [])[:n]]


def _google_cse(query: str, n: int) -> list[SearchResult]:
    if not (settings.google_cse_key and settings.google_cse_cx):
        raise SearchError("GOOGLE_CSE_KEY / GOOGLE_CSE_CX not set")
    resp = net.session().get("https://www.googleapis.com/customsearch/v1", params={
        "q": query, "key": settings.google_cse_key, "cx": settings.google_cse_cx,
        "num": min(n, 10),
    }, timeout=settings.timeout)
    _raise_for_status(resp, "google_cse")
    data = resp.json()
    return [SearchResult(title=it.get("title", ""), url=it.get("link", ""),
                         snippet=it.get("snippet", ""), source="google_cse")
            for it in data.get("items", [])[:n]]


PROVIDERS = {
    "bing": _bing,
    "duckduckgo": _duckduckgo,
    "wikipedia": _wikipedia,
    "searxng": _searxng,
    "brave": _brave,
    "tavily": _tavily,
    "serpapi": _serpapi,
    "google_cse": _google_cse,
}

# Order tried under provider="auto": keyed providers first (higher quality when
# configured), then the reliable no-key fallbacks.
def _auto_order() -> list[str]:
    order = []
    if settings.brave_api_key:
        order.append("brave")
    if settings.serpapi_api_key:
        order.append("serpapi")
    if settings.google_cse_key and settings.google_cse_cx:
        order.append("google_cse")
    if settings.searxng_url:
        order.append("searxng")
    # Two independent no-key general engines so a consent wall on one still
    # yields results, then Wikipedia as a reliable last resort.
    order += ["bing", "duckduckgo", "wikipedia"]
    return order


def _run_providers(query: str, fetch_n: int, order: list[str], market: str | None,
                   errors: list[str]) -> list[SearchResult]:
    for name in order:
        fn = PROVIDERS.get(name)
        if fn is None:
            errors.append(f"{name}: unknown provider")
            continue
        try:
            results = fn(query, fetch_n, market) if name == "bing" else fn(query, fetch_n)
        except Exception as exc:  # noqa: BLE001 — try the next backend
            errors.append(f"{name}: {exc}")
            continue
        if results:
            return results
    return []


def _apply_filters(results, include_domains, exclude_domains):
    results = _dedupe(results)
    if include_domains:
        inc = tuple(d.lower().lstrip(".") for d in include_domains)
        results = [r for r in results if _host_matches(urlsplit(r.url).netloc, inc)]
    if exclude_domains:
        exc = tuple(d.lower().lstrip(".") for d in exclude_domains)
        results = [r for r in results if not _host_matches(urlsplit(r.url).netloc, exc)]
    return results


def search(query: str, *, provider: str | None = None, max_results: int | None = None,
           include_domains: list[str] | None = None,
           exclude_domains: list[str] | None = None,
           market: str | None = None, site: str | None = None) -> list[SearchResult]:
    """Search the web. Returns a de-duplicated, domain-filtered result list.

    When ``site`` is given (e.g. "danawa.com"), searches that site directly via
    its own search page — fully local, no external search engine involved.

    Otherwise: when ``include_domains`` is set we over-fetch and post-filter; if
    that yields nothing we retry once with the domain's brand word appended as a
    plain keyword to boost recall.
    """
    provider = provider or settings.default_provider
    n = max_results or settings.default_max_results
    query = query.strip()
    if not query:
        return []

    # Fully-local path: hit the site's own search, bypassing Bing entirely.
    if site:
        from . import sitesearch  # lazy import avoids a circular dependency
        return sitesearch.site_search(site, query, max_results=n, market=market)

    fetch_n = n if not include_domains else max(20, n * 4)
    order = _auto_order() if provider == "auto" else [provider]
    errors: list[str] = []

    raw = _run_providers(query, fetch_n, order, market, errors)
    filtered = _apply_filters(raw, include_domains, exclude_domains)

    # Recall fallback for domain-scoped searches that came back empty.
    if include_domains and not filtered:
        brand = " ".join(d.lstrip(".").split(".")[0] for d in include_domains)
        raw2 = _run_providers(f"{query} {brand}", fetch_n, order, market, errors)
        filtered = _apply_filters(raw2, include_domains, exclude_domains)

    if errors:
        log.debug("search provider errors for %r: %s", query, "; ".join(errors))
    if not filtered and errors:
        log.warning("search for %r returned nothing; provider errors: %s",
                    query, "; ".join(errors))
    if not filtered and not raw and provider != "auto":
        raise SearchError(f"provider {provider!r} returned nothing: {'; '.join(errors)}")
    return filtered[:n]
