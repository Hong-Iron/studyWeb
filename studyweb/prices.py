"""Find what something costs across a fixed list of sites.

This is deliberately *not* a search-engine query with `site:` operators — Bing
and DuckDuckGo return almost nothing for `(site:a OR site:b OR …)`, and what
they do return is a snippet, not a price. Instead each site's own search page is
crawled directly (:mod:`studyweb.sitesearch`), and the price is read off the
product page's structured markup (:mod:`studyweb.structured`), falling back to
the price the search result already displayed.

No API key. No LLM unless you ask for one — Korean shopping sites publish
JSON-LD `Offer.price`, so the exact number is usually right there in the page.

    >>> find_prices("AMD 라이젠5 9600X", sites=["danawa.com"])
    {'query': ..., 'quotes': [{'site': 'danawa.com', 'price': 265000, ...}], ...}
"""

from __future__ import annotations

import logging
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, field

from . import net, sitesearch
from .config import settings
from .structured import extract_structured

log = logging.getLogger("studyweb.prices")

# Korean price-comparison and marketplace sites, in the order a buyer would
# actually check them. Override per call or with STUDYWEB_PRICE_SITES.
DEFAULT_SITES: tuple[str, ...] = (
    "danawa.com", "shopping.naver.com", "11st.co.kr", "coupang.com",
)

# A price in prose must carry its currency: "265,000원", "265000 KRW", "₩265,000".
# A bare number never counts — "라이젠5 9600X" would otherwise sell for 9,600원,
# and a review count would outrank the real price.
_WON = re.compile(r"(?:₩\s*([0-9][0-9,]*)|([0-9][0-9,]*)\s*(?:원|KRW))")
# Structured markup (JSON-LD Offer.price) is already known to be a price, so
# there the digits stand on their own.
_DIGITS = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


@dataclass
class Quote:
    """One price, and enough context to check it."""
    site: str
    title: str
    url: str
    price: int | None = None
    currency: str = "KRW"
    method: str = ""          # json-ld | microdata | opengraph | listing | naver_api
    brand: str = ""
    snippet: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _as_won(raw: str) -> int | None:
    try:
        n = int(float(raw.replace(",", "")))
    except (TypeError, ValueError):
        return None
    # Under 100 won nothing real is on sale; that range is page furniture.
    return n if n >= 100 else None


def price_from_text(s: str | None) -> int | None:
    """Pull a price out of prose — only where the currency says it is one.

    "리뷰 1,234건 · 265,000원" is 265000, and "라이젠5 9600X" is not a price
    at all, which is the whole point of insisting on the marker.
    """
    if not s:
        return None
    m = _WON.search(s)
    return _as_won(m.group(1) or m.group(2)) if m else None


def price_from_field(v) -> int | None:
    """Parse a value that structured markup already labelled as a price."""
    m = _DIGITS.search(str(v or ""))
    return _as_won(m.group(0)) if m else None


def _price_of(url: str) -> tuple[int | None, str, str, str]:
    """(price, method, name, brand) from a product page's structured markup."""
    try:
        resp = net.get(url, use_cache=True)
    except Exception as exc:  # noqa: BLE001 — one dead product page is not fatal
        log.debug("price fetch failed for %s: %s", url, exc)
        return None, "", "", ""
    data = extract_structured(resp.content, url, resp.declared_encoding)
    if not data:
        return None, "", "", ""
    return (price_from_field(data.get("price")),
            str(data.get("source") or "structured"),
            str(data.get("name") or ""), str(data.get("brand") or ""))


def _quote(site: str, r) -> Quote:
    """Turn one search hit into a quote, preferring the product page's own
    structured price over the number the listing happened to render."""
    price, method, name, brand = _price_of(r.url)
    if price is None:
        # Listing pages often show the price and nothing else useful; danawa's
        # result rows are literally "265,000원".
        price = price_from_text(r.title) or price_from_text(r.snippet)
        method = "listing" if price is not None else ""
    return Quote(site=site, title=(name or r.title).strip(), url=r.url,
                 price=price, method=method, brand=brand, snippet=r.snippet)


def _naver_quotes(query: str, n: int) -> list[Quote]:
    """Naver's shopping API answers for the whole marketplace at once — exact
    prices, no page fetches. Used instead of crawling when keys are present."""
    from .search import PROVIDERS
    out = []
    for r in PROVIDERS["naver_shop"](query, n):
        out.append(Quote(site="shopping.naver.com", title=r.title, url=r.url,
                         price=r.extra.get("price_low"), method="naver_api",
                         brand=r.extra.get("brand", ""),
                         snippet=r.extra.get("mall", "")))
    return out


def _site_quotes(site: str, query: str, per_site: int) -> tuple[list[Quote], str]:
    """(quotes, miss-reason). A site that blocks bots yields ([], reason)."""
    use_naver_api = (site.endswith("shopping.naver.com")
                     and settings.naver_client_id and settings.naver_client_secret)
    try:
        if use_naver_api:
            return _naver_quotes(query, per_site), ""
        results = sitesearch.site_search(site, query, max_results=per_site)
    except Exception as exc:  # noqa: BLE001 — report per site, keep the others
        return [], f"{type(exc).__name__}: {exc}"
    if not results:
        # Coupang, 11st and friends serve a JS shell to a plain fetch.
        return [], "no results — the site's search page returned nothing to a static fetch"

    workers = min(len(results), settings.max_workers)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        quotes = list(pool.map(lambda r: _quote(site, r), results))
    priced = [q for q in quotes if q.price is not None]
    if not priced:
        return [], f"{len(quotes)} page(s) found, none published a price"
    return priced, ""


def find_prices(query: str, sites=None, *, per_site: int = 3) -> dict:
    """Look ``query`` up on every site in ``sites`` and return every price found.

    ``sites``     domains to check (default: :data:`DEFAULT_SITES`, or
                  STUDYWEB_PRICE_SITES). Any site works — one that isn't in
                  sitesearch's registry gets its search form auto-discovered.
    ``per_site``  how many results to price per site.

    Returns ``{query, sites, quotes, summary, misses, response_time}``, quotes
    sorted cheapest first. ``summary`` is None when nothing could be priced —
    a median of zero quotes is not a number worth inventing.
    """
    t0 = time.time()
    sites = [s.strip() for s in (sites or settings.price_sites) if s and s.strip()]
    quotes: list[Quote] = []
    misses: list[dict] = []

    with ThreadPoolExecutor(max_workers=max(1, min(len(sites) or 1, settings.max_workers))) as pool:
        for site, (found, reason) in zip(
                sites, pool.map(lambda s: _site_quotes(s, query, per_site), sites)):
            quotes.extend(found)
            if reason:
                misses.append({"site": site, "reason": reason})

    quotes.sort(key=lambda q: q.price or 0)
    prices = [q.price for q in quotes if q.price is not None]
    summary = None
    if prices:
        by_site: dict[str, int] = {}
        for q in quotes:
            if q.price is not None and q.site not in by_site:
                by_site[q.site] = q.price      # cheapest per site (already sorted)
        summary = {
            "count": len(prices), "currency": "KRW",
            "min": min(prices), "max": max(prices),
            "median": int(statistics.median(prices)),
            "cheapest_url": quotes[0].url,
            "by_site": by_site,
        }

    return {
        "query": query, "sites": sites,
        "quotes": [q.to_dict() for q in quotes],
        "summary": summary, "misses": misses,
        "response_time": round(time.time() - t0, 3),
    }
