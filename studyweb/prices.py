"""Find what something costs across a fixed list of sites.

This is deliberately *not* a search-engine query with `site:` operators — Bing
and DuckDuckGo return almost nothing for `(site:a OR site:b OR …)`, and what
they do return is a snippet, not a price. Instead each site's own search page is
crawled directly (:mod:`studyweb.sitesearch`) and the price is read off the
product page — from its structured markup (:mod:`studyweb.structured`) where
there is any, from the rendered DOM under a price label where there is none,
and from the search-result row as a last resort.

No API key. No LLM unless you ask for one — Korean shopping sites publish
JSON-LD `Offer.price`, so the exact number is usually right there in the page;
the ones that don't (Compuzone) still print it next to the word 판매가.

    >>> find_prices("AMD 라이젠5 9600X", sites=["danawa.com"])
    {'query': ..., 'quotes': [{'site': 'danawa.com', 'price': 265000, ...}], ...}
"""

from __future__ import annotations

import copy
import logging
import re
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, field

from lxml import html as LH

from . import net, sitesearch
from .config import settings, DEFAULT_PRICE_SITES as DEFAULT_SITES
from .structured import extract_structured

log = logging.getLogger("studyweb.prices")

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
    method: str = ""          # json-ld | microdata | opengraph | dom | listing | naver_api
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


# A search-results row is not prose: it is the whole card flattened into one
# string, so several numbers live in it and the first one is usually wrong.
# Coupang's row reads "369,000원29%259,000원배송비 2,500원…" — the list price
# struck through, the discount, the price you actually pay, then the shipping.
_DISCOUNTED = re.compile(r"\d{1,2}\s*%\s*$")     # "…29%" right before the number
# Korean labels sit on one side of their amount and not the other: "배송비
# 2,500원" leads, "8,967원 적립" trails. Checking both sides for both words
# would throw away the sale price in "259,000원배송비 2,500원", where 배송비
# belongs to the number *after* it.
_LABEL_BEFORE = ("배송비", "쿠폰", "포인트", "마일리지", "적립")
_LABEL_AFTER = ("적립", "쿠폰", "포인트", "마일리지")


def price_from_listing(s: str | None) -> int | None:
    """Pull the price a shopper would pay out of one search-result row.

    Prefers the number that follows a discount percentage over the struck-through
    one before it, and skips the amounts the row quotes for something other than
    the product (shipping, points, coupons).
    """
    if not s:
        return None
    first: int | None = None
    for m in _WON.finditer(s):
        n = _as_won(m.group(1) or m.group(2))
        if n is None:
            continue
        before, after = s[max(0, m.start() - 8):m.start()], s[m.end():m.end() + 4]
        if any(w in before for w in _LABEL_BEFORE) or any(w in after for w in _LABEL_AFTER):
            continue
        if _DISCOUNTED.search(before):
            return n
        if first is None:
            first = n
    return first


# Model codes, for telling "the row we asked for" from the ad next to it.
# Three digits is the threshold that keeps 9600X, 7500F and 5080 while leaving
# out the counts and dates a row is full of ("(11)", "7/31").
_MODEL_CODE = re.compile(r"[a-z0-9]*\d{3,}[a-z0-9]*")
_ROW_NOISE = re.compile(r"[0-9][0-9,]*\s*(?:원|KRW)|\d+\s*%|\d+/\d+")


def model_codes(s: str) -> set[str]:
    """The model codes in a title, with prices, discounts and dates removed
    first — otherwise "216,080원" reads as the model numbers 216 and 080."""
    return set(_MODEL_CODE.findall(_ROW_NOISE.sub(" ", (s or "").lower())))


def names_another_product(title: str, query: str) -> bool:
    """True when the row is plainly about a *different* model than the query.

    Shopping sites pad their results with ads: a search for 9600X returns rows
    for the 7500F and the 5600. Those carry real prices, so nothing downstream
    can tell they are the wrong product — and one of them becomes the minimum.
    A row that names no model at all (danawa's rows are literally "265,000원")
    is left alone; only a stated, conflicting model is grounds for dropping it.
    """
    want, got = model_codes(query), model_codes(title)
    return bool(want and got and not (want & got))


# A shop that walls off its product pages still returns 200 with a title like
# "Access Denied" — which then becomes the quote's name, hiding the fact that
# nothing about the product was verified.
_BLOCK_PAGE = re.compile(
    r"access denied|forbidden|attention required|just a moment|are you a robot|"
    r"page not found|error \d{3}|접근이 (?:거부|차단)|잘못된 (?:접근|요청)", re.I)


def _looks_blocked(name: str) -> bool:
    return bool(name) and len(name) <= 60 and bool(_BLOCK_PAGE.search(name))


# --------------------------------------------------------------------------- #
#  Reading a price off the page itself                                         #
# --------------------------------------------------------------------------- #
# Plenty of shops publish no schema.org markup at all — Compuzone is one — so
# the number has to come out of the rendered DOM. Two rules keep that honest.
#
# 1. Hidden nodes go first. Korean shops routinely plant a decoy: Compuzone
#    ships `<div style="display:none">256,000</div>259,000<span>원</span>`, so a
#    naive read returns the decoy, or glues the two into "256,000259,000".
#    Dropping the node has to keep its *tail* — the real digits live there —
#    which is exactly what lxml's drop_tree() does and remove() does not.
# 2. A candidate must sit under a visible price label. Without that rule a promo
#    banner ("래플 … 99% 100원") outranks the product the page is about.
_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0")
# Best label first: 판매가 is unambiguous, a bare 가격 may be a spec-table header.
_PRICE_LABELS = ("판매가", "최저가", "할인가", "판매 가격", "가격",
                 "sale price", "our price", "price")
# Beyond this a "container" is a page section, not a price box, and the amount
# it holds is as likely to be a coupon or a shipping threshold as the price.
_LABEL_CONTEXT = 120


def parse_html(raw: bytes, encoding: str | None):
    """Parse page bytes into a tree.

    The encoding default matters: told nothing, lxml falls back to latin-1 and
    every Korean label on the page turns to mojibake — at which point 판매가
    never matches and a perfectly readable price looks absent. Undeclared HTML
    is UTF-8 far more often than it is anything else.
    """
    return LH.fromstring(raw, parser=LH.HTMLParser(encoding=encoding or "utf-8"))


def _prune(doc):
    """Drop the parts of a page that must not contribute a price."""
    for el in doc.xpath("//script|//style|//noscript|//template|//del|//s|//strike"):
        if el.getparent() is not None:
            el.drop_tree()
    for el in doc.xpath("//*[@style]"):
        # getparent() is None once an enclosing hidden node took this one with it.
        if _HIDDEN.search(el.get("style") or "") and el.getparent() is not None:
            el.drop_tree()
    return doc


def price_from_dom(doc) -> int | None:
    """The selling price shown on a product page, or None if it isn't labelled.

    Returning None is the right answer far more often than guessing: a wrong
    price is worse than a reported miss.
    """
    doc = _prune(copy.deepcopy(doc))
    best: tuple[int, int, int] | None = None      # (label rank, distance, price)
    for el in doc.iter():
        label = re.sub(r"\s+", " ", (el.text or "")).strip().lower()
        # A label is a short caption ("판매가"), never a sentence that mentions one.
        if not label or len(label) > 24:
            continue
        rank = next((i for i, lab in enumerate(_PRICE_LABELS) if lab in label), None)
        if rank is None:
            continue
        node = el
        for distance in range(3):
            node = node.getparent()
            if node is None:
                break
            body = re.sub(r"\s+", " ", node.text_content()).strip()
            if len(body) > _LABEL_CONTEXT:
                break                      # climbed out of the price box
            m = _WON.search(body)
            if m:
                price = _as_won(m.group(1) or m.group(2))
                if price is not None and (best is None or (rank, distance) < best[:2]):
                    best = (rank, distance, price)
                break
    return best[2] if best else None


def _page_name(doc) -> str:
    """The product's name, for pages where a search listing only had spec text."""
    for xp in ('//meta[@property="og:title"]/@content',
               "//h1//text()", "//title/text()"):
        for raw in doc.xpath(xp):
            name = re.sub(r"\s+", " ", str(raw)).strip()
            # Sites suffix their own name onto <title>/og:title ("… : 컴퓨존");
            # the product is the part before it.
            name = re.split(r"\s+[:|｜]\s+", name)[0].strip()
            if name:
                return name[:200]
    return ""


@dataclass
class _Read:
    """What one product page gave up. ``problem`` explains an empty price."""
    price: int | None = None
    method: str = ""
    name: str = ""
    brand: str = ""
    problem: str = ""


def _price_of(url: str) -> _Read:
    """Read the price off one product page."""
    try:
        resp = net.get(url, use_cache=True)
    except Exception as exc:  # noqa: BLE001 — one dead product page is not fatal
        log.debug("price fetch failed for %s: %s", url, exc)
        # net.FetchError puts the URL after the colon; the cause is the half
        # worth repeating back ("blocked by robots.txt", "HTTP 403", …).
        return _Read(problem=(str(exc).split(":", 1)[0] or type(exc).__name__)[:60])
    try:
        doc = parse_html(resp.content, resp.declared_encoding)
    except Exception as exc:  # noqa: BLE001 — an unparseable page is a miss
        log.debug("price parse failed for %s: %s", url, exc)
        return _Read(problem="the page could not be parsed")

    data = extract_structured(doc, url) or {}
    price = price_from_field(data.get("price"))
    method = str(data.get("source") or "") if price is not None else ""
    if price is None:
        price = price_from_dom(doc)
        method = "dom" if price is not None else ""
    return _Read(price=price, method=method,
                 name=str(data.get("name") or "") or _page_name(doc),
                 brand=str(data.get("brand") or ""),
                 problem="" if price is not None else "no price in the page")


def _quote(site: str, r) -> tuple[Quote, str]:
    """Turn one search hit into a quote, preferring the product page's own
    structured price over the number the listing happened to render."""
    read = _price_of(r.url)
    price, method, problem = read.price, read.method, read.problem
    if price is None:
        # Listing pages often show the price and nothing else useful; danawa's
        # result rows are literally "265,000원".
        price = price_from_listing(r.title) or price_from_listing(r.snippet)
        if price is not None:
            method, problem = "listing", ""
    # The page's own name is better than the row's — unless the "page" was the
    # shop's bot wall, whose title says nothing about what we just priced.
    name = "" if _looks_blocked(read.name) else read.name
    return Quote(site=site, title=(name or r.title).strip(), url=r.url,
                 price=price, method=method, brand=read.brand,
                 snippet=r.snippet), problem


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
        if not results:
            # A shop named in a question arrives as a guess at its domain, and
            # the guess is usually the .com. Try the one we know sells things.
            alt = sitesearch.sibling_site(site)
            if alt and (found := sitesearch.site_search(alt, query, max_results=per_site)):
                log.info("%s returned nothing; %s answered instead", site, alt)
                results, site = found, alt
    except Exception as exc:  # noqa: BLE001 — report per site, keep the others
        return [], f"{type(exc).__name__}: {exc}"
    if not results:
        # Coupang, 11st and friends serve a JS shell to a plain fetch.
        return [], "no results — the site's search page returned nothing to a static fetch"

    workers = min(len(results), settings.max_workers)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        read = list(pool.map(lambda r: _quote(site, r), results))
    priced = [q for q, _ in read if q.price is not None]
    # An unverified row that names a different model is an ad, not an offer.
    # (A page we actually read is left in: a PC *containing* the 9600X is a real
    # answer to the site's own ranking, and the caller is told to check titles.)
    kept = [q for q in priced
            if not (q.method == "listing" and names_another_product(q.title, query))]
    if len(kept) < len(priced):
        log.debug("%s: dropped %d row(s) for another model", site, len(priced) - len(kept))
        if not kept:
            return [], (f"{len(priced)} result(s) found, all for other products — "
                        "the site answered with ads, not this model")
    priced = kept
    if not priced:
        # Say *why*. "none published a price" and "every page was blocked by
        # robots.txt" call for completely different fixes.
        why = Counter(p for _, p in read if p).most_common(1)
        return [], f"{len(read)} page(s) found, none priced" + (
            f" — {why[0][0]}" if why else "")
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
