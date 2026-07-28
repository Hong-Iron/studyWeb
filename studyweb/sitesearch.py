"""Fully-local site search — no external search engine.

Instead of going through Bing, `site_search` hits a target site's *own* search
URL directly and harvests the on-site result links. This never touches a
third-party SERP, so it can't be rate-limited the way Bing scraping can, and it
works great for shopping/reference sites (Danawa, Coupang, Amazon, GitHub, …).

Two ways a site's search URL is found:
  1. a small built-in registry of known sites (with result-link patterns), and
  2. a generic fallback that reads the site's homepage <form> to build the URL.

Best on server-rendered sites; JS-only result pages (some SPAs) will be thin —
the same limitation as any no-JS fetcher.
"""

from __future__ import annotations

import re
import threading
from urllib.parse import (urlsplit, urlunsplit, urlencode, quote_plus,
                          parse_qsl, urljoin)

from lxml import html as LH

from . import net
from .config import settings
from .fetch import fetch_page
from .search import SearchResult

# domain -> (search-URL template with {q}, optional regex a result link must match)
SITES: dict[str, tuple[str, str | None]] = {
    # Korean shopping / price comparison
    "danawa.com": ("https://search.danawa.com/dsearch.php?query={q}", r"prod\.danawa\.com/info"),
    # Compuzone's /search/search.htm is a JS shell — the product list is fetched
    # from this endpoint, which answers a plain GET with server-rendered rows.
    "compuzone.co.kr": (
        "https://www.compuzone.co.kr/search/search_list.php?actype=list"
        "&SearchType=small&SearchText={q}&PreOrder=sale_order"
        "&PageCount=20&StartNum=0&PageNum=1&ListType=0"
        "&BigDivNo=&MediumDivNo=&DivNo=", r"product_detail\.htm"),
    "coupang.com": ("https://www.coupang.com/np/search?q={q}", r"/vp/products/"),
    "gmarket.co.kr": ("https://browse.gmarket.co.kr/search?keyword={q}", r"item\.gmarket\.co\.kr|/Item"),
    "11st.co.kr": ("https://search.11st.co.kr/Search.tmall?kwd={q}", r"/products/"),
    "enuri.com": ("https://www.enuri.com/search.jsp?keyword={q}", r"detail\.jsp"),
    "shopping.naver.com": ("https://search.shopping.naver.com/search/all?query={q}", None),
    "naver.com": ("https://search.naver.com/search.naver?query={q}", None),
    # Global shopping
    "amazon.com": ("https://www.amazon.com/s?k={q}", r"/dp/|/gp/product/"),
    "ebay.com": ("https://www.ebay.com/sch/i.html?_nkw={q}", r"/itm/"),
    "aliexpress.com": ("https://www.aliexpress.com/wholesale?SearchText={q}", r"/item/"),
    # Reference / dev / study
    "wikipedia.org": ("https://en.wikipedia.org/w/index.php?search={q}", r"/wiki/"),
    "github.com": ("https://github.com/search?q={q}&type=repositories", None),
    "stackoverflow.com": ("https://stackoverflow.com/search?q={q}", r"/questions/\d"),
    "reddit.com": ("https://www.reddit.com/search/?q={q}", r"/comments/"),
    "pypi.org": ("https://pypi.org/search/?q={q}", r"/project/"),
    "npmjs.com": ("https://www.npmjs.com/search?q={q}", r"/package/"),
    "youtube.com": ("https://www.youtube.com/results?search_query={q}", r"/watch\?v="),
}

# Query params to drop when de-duplicating result URLs. Covers tracking, the
# echoed search term, pagination and list-position noise — so two links to the
# same product collapse to one, while the real id param (pcode, asin, …) stays.
_TRACKING = {
    "keyword", "query", "q", "search", "searchword", "kwd", "kw", "wd",
    "ref", "referer", "spm", "src", "from", "cate", "category", "cate_c",
    "page", "sort", "order", "list", "view", "tab", "position", "rank",
    "loggroup", "sess", "index", "adid", "acode", "trace", "mc",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "_trksid", "_trkparms", "hash",
}

_INPUT_NAMES = ("q", "query", "search", "keyword", "kwd", "kw", "wd", "k", "s",
                "searchword", "search_query", "SearchText", "_nkw")

# Sites that don't use one of the conventional input names almost always still
# say "search" somewhere — in the input's id (icoda's `stx` sits in
# `id="search_inp"`), in the form's name, or in its action path.
_SEARCHY = re.compile(r"search|keyword|query|kwd|qry|schword|検索|검색", re.I)

_discovered: dict[str, str] = {}
_discover_lock = threading.Lock()


def _norm_site(site: str) -> str:
    s = site.strip().lower()
    s = re.sub(r"^https?://", "", s).split("/", 1)[0]
    return s[4:] if s.startswith("www.") else s


def _registry_key(site_norm: str) -> str | None:
    for domain in SITES:
        if site_norm == domain or site_norm.endswith("." + domain):
            return domain
    return None


def sibling_site(site: str) -> str | None:
    """A registered domain that is plainly the same shop under another TLD.

    For use only *after* the typed domain came back empty. compuzone.com is a
    live host that answers every request with 403, while the shop everyone means
    is compuzone.co.kr — and a model asked about "컴퓨존" has no way to know
    which TLD is the real one. Returns None when the typed domain is registered
    (nothing to recover) or when its label matches more than one registered
    domain, so a genuine sibling like amazon.co.uk — which searches perfectly
    well on its own — is never quietly turned into amazon.com.
    """
    norm = _norm_site(site)
    if _registry_key(norm):
        return None
    label = norm.split(".", 1)[0]
    same = [d for d in SITES if d.split(".", 1)[0] == label]
    return same[0] if len(same) == 1 else None


def _canon(url: str) -> str:
    """Canonical key for de-duplication. Product/result pages are keyed by their
    numeric id param(s) (pcode, cate, itemId, …) so cosmetic deep-links to the
    same item (review/opinion anchors, tracking) collapse to one. Pages without
    a numeric id fall back to their non-tracking params."""
    p = urlsplit(url)
    params = parse_qsl(p.query)
    digit_params = [(k, v) for k, v in params if v.isdigit() and len(v) >= 3]
    kept = digit_params if digit_params else [
        (k, v) for k, v in params if k.lower() not in _TRACKING]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/",
                       urlencode(sorted(kept)), ""))


def _score_form(form, site_norm: str) -> tuple[int, str] | None:
    """(confidence, template) if this <form> looks like the site's search box.

    Scored rather than first-match, because the search form is rarely the first
    one on the page — a shop's homepage leads with login and newsletter forms,
    and picking one of those yields a template that quietly returns nothing.
    """
    best_name, best_input = 0, ""
    for inp in form.xpath(".//input"):
        name = inp.get("name") or ""
        if not name or (inp.get("type") or "").lower() in (
                "hidden", "checkbox", "radio", "submit", "button", "password"):
            continue
        if (inp.get("type") or "").lower() == "search":
            score = 3
        elif name in _INPUT_NAMES:
            score = 2
        elif _SEARCHY.search(f"{name} {inp.get('id') or ''}"):
            score = 1
        else:
            continue
        if score > best_name:
            best_name, best_input = score, name
    if not best_input:
        return None

    action = urljoin(f"https://{site_norm}/", form.get("action") or "/")
    base = action.split("#", 1)[0]
    score = best_name
    if _SEARCHY.search(" ".join(filter(None, (
            form.get("name"), form.get("id"), form.get("class"), urlsplit(base).path)))):
        score += 2
    if (form.get("method") or "get").lower() == "post":
        score -= 2      # we can only build a GET URL; it may or may not answer
    if score < 2:
        return None
    sep = "&" if "?" in base else "?"
    return score, f"{base}{sep}{quote_plus(best_input)}={{q}}"


def _discover_template(site_norm: str) -> str | None:
    """Read the site's homepage <form> to build a `?name={q}` search template."""
    with _discover_lock:
        if site_norm in _discovered:
            return _discovered[site_norm] or None
    template = None
    try:
        resp = net.get(f"https://{site_norm}/", use_cache=True)
        parser = LH.HTMLParser(encoding=resp.declared_encoding) if resp.declared_encoding else None
        doc = LH.fromstring(resp.content, parser=parser)
        scored = [c for c in (_score_form(f, site_norm) for f in doc.xpath("//form")) if c]
        if scored:
            template = max(scored, key=lambda c: c[0])[1]
    except Exception:
        template = None
    with _discover_lock:
        _discovered[site_norm] = template or ""
    return template


def build_search_url(site: str, query: str) -> str | None:
    """Return the site's own search URL for `query`, or None if unknown."""
    site_norm = _norm_site(site)
    key = _registry_key(site_norm)
    if key:
        template = SITES[key][0]
    else:
        template = _discover_template(site_norm)
    if not template:
        return None
    return template.replace("{q}", quote_plus(query))


def _result_pattern(site_norm: str) -> re.Pattern | None:
    key = _registry_key(site_norm)
    pat = SITES[key][1] if key else None
    return re.compile(pat) if pat else None


def _looks_like_home(url: str, site_norm: str) -> bool:
    p = urlsplit(url)
    return (p.path in ("", "/")) and not p.query


def site_search(site: str, query: str, *, max_results: int = 6,
                market: str | None = None) -> list[SearchResult]:
    """Search *within* a site via its own search page — no external engine.

    Returns on-site result links in the site's own ranked order, de-duplicated.
    """
    query = query.strip()
    if not query:
        return []

    site_norm = _norm_site(site)

    # Sites with a bespoke adapter (no keyword search, or prices hidden in a
    # configurator form) are handled entirely by the adapter.
    from . import siteadapters
    adapter = siteadapters.adapter_for(site_norm)
    if adapter is not None:
        try:
            res = adapter.search(query, max_results)
            if res:
                return res
        except Exception:  # noqa: BLE001 — fall back to the generic path
            pass

    url = build_search_url(site, query)
    if not url:
        return []

    pattern = _result_pattern(site_norm)
    page = fetch_page(url)
    results = _harvest(page, site_norm, pattern, _canon(url), max_results) if page.ok else []

    # Marketplaces (11st, Coupang, Naver shopping) serve a JS shell to a static
    # fetch — the listing only exists after scripts run. Render once if a browser
    # is available; without one the caller gets an honest empty list.
    if not results and settings.render_enabled:
        from . import render
        if render.available():
            html_str = render.render_html(url)
            if html_str:
                from .dataextract import _rendered_document
                results = _harvest(_rendered_document(url, html_str), site_norm,
                                   pattern, _canon(url), max_results)
    return results


def _harvest(page, site_norm: str, pattern, search_key: str,
             max_results: int) -> list[SearchResult]:
    """Pick on-site result links out of a fetched (or rendered) search page."""
    best_title: dict[str, str] = {}
    order: list[tuple[str, str]] = []
    for link in page.links:
        host = urlsplit(link.url).netloc.lower()
        if not (host == site_norm or host.endswith("." + site_norm)):
            continue
        if _looks_like_home(link.url, site_norm):
            continue
        if pattern and not pattern.search(link.url):
            continue
        key = _canon(link.url)
        if key == search_key:
            continue
        title = re.sub(r"\s+", " ", link.text or "").strip()
        if key not in best_title:
            best_title[key] = title
            order.append((key, link.url))
        elif len(title) > len(best_title[key]):
            best_title[key] = title

    results = []
    for key, orig in order[:max_results]:
        results.append(SearchResult(
            title=best_title[key] or urlsplit(orig).netloc,
            url=orig, snippet="", source=f"site:{site_norm}"))
    return results
