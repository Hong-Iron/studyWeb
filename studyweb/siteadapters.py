"""Per-site adapters for pages the generic pipeline can't handle well.

Some sites have no keyword search and/or hide the thing you actually want
(prices) inside a configurator ``<form>`` that :mod:`studyweb.extract` strips as
chrome. An adapter teaches studyweb about one such site:

* ``search(query, n)`` — turn a query into on-site result links, using whatever
  the site *does* have (a category catalog, a sitemap, …) instead of a
  ``?q=`` search endpoint.
* ``extract(url, raw, encoding)`` — a site-specific override that pulls the
  fields the generic extractor misses (model name, price), returning an
  :class:`studyweb.extract.Extracted` or ``None`` to fall back to generic.

Register an adapter in ``ADAPTERS``; :mod:`studyweb.sitesearch` and
:mod:`studyweb.fetch` consult :func:`adapter_for` by host.

The first adapter (Itmaya, a Korean server-build/quote shop) is also the
reference for adding more: it's a catalog site with server-rendered price
ranges but no search box.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from lxml import html as LH

from . import adaptive, net
from .extract import Extracted
from .rank import tokenize, BM25
from .search import SearchResult

log = logging.getLogger("studyweb.siteadapters")


def _parse(raw: bytes, encoding: str | None):
    parser = LH.HTMLParser(encoding=encoding) if encoding else LH.HTMLParser()
    return LH.fromstring(raw, parser=parser)


@dataclass
class _PageCtx:
    """The original bytes behind an already-parsed lxml doc.

    Adaptive relocation reparses with Scrapling, so it needs the source rather
    than the tree. Carried as one object so adapter methods keep a single
    optional parameter instead of three, and stay callable without it.
    """

    raw: bytes
    url: str
    encoding: str | None = None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _label(s: str) -> str:
    """Normalise a definition-term label: collapse whitespace and drop a
    trailing colon (dt text on itmaya reads e.g. '예상딜리버리 :')."""
    return _clean(s).rstrip(" :：·").strip()


def _won(s: str) -> int:
    """Parse a KRW amount ('6,910,000' / '6910000원') to an int (0 if none)."""
    digits = re.sub(r"[^0-9]", "", s or "")
    return int(digits) if digits else 0


def _won_str(start: int, top: int) -> str:
    """Format a price (or price range) as e.g. '6,910,000원' / '3,613,000~3,733,000원'."""
    if not start and not top:
        return "가격문의"
    if top and top != start:
        return f"{start:,}~{top:,}원"
    return f"{start:,}원"


class SiteAdapter:
    """Base class. Subclasses set ``domains`` and override what they support."""

    domains: tuple[str, ...] = ()

    def handles(self, host: str) -> bool:
        host = host.lower().removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        raise NotImplementedError

    def extract(self, url: str, raw: bytes, encoding: str | None) -> Extracted | None:
        return None


# --------------------------------------------------------------------------- #
#  Itmaya — itmaya.co.kr                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class _Cat:
    label: str      # e.g. "GPU서버 4GPU Server ESC4000-E11 (5th INTEL)"
    url: str        # server_list.php?category_s=N


class ItmayaAdapter(SiteAdapter):
    """Adapter for Itmaya (server build-to-order / quote shop).

    No keyword search exists, so search ranks the homepage mega-menu catalog
    (category + model names -> ``server_list.php`` pages) against the query,
    drills into the best categories to collect ``server_view.php`` product
    pages, and reads each product's server-rendered estimated-price range.
    """

    domains = ("itmaya.co.kr", "itmaya.com")
    HOME = "https://itmaya.co.kr/"

    # -- catalog ---------------------------------------------------------- #
    def _catalog(self) -> list[_Cat]:
        """Parse the mega-menu into (rich label -> category URL) pairs."""
        try:
            resp = net.get(self.HOME, check_robots=False, use_cache=True)
        except net.FetchError:
            return []
        doc = _parse(resp.content, resp.declared_encoding)
        out: list[_Cat] = []
        seen: set[str] = set()
        for a in doc.xpath('//a[contains(@href,"server_list.php")]'):
            href = urljoin(self.HOME, a.get("href", ""))
            key = urlsplit(href).query
            if not key or key in seen:
                continue
            # Build a label from the anchor plus ancestor category titles, so a
            # query can match on either the Korean category or the model code.
            parts = [_clean(a.text_content())]
            for anc in a.xpath('ancestor::*[contains(@class,"tit")]'):
                t = _clean(anc.text_content())
                if t and t not in parts:
                    parts.append(t)
            for anc in a.xpath('ancestor::div[contains(@class,"sub_set")][1]'
                               '//div[contains(@class,"menu_tit")]//*[contains(@class,"tit")]'):
                t = _clean(anc.text_content())
                if t and t not in parts:
                    parts.append(t)
            label = " ".join(p for p in parts if p)
            if label:
                seen.add(key)
                out.append(_Cat(label=label, url=href))
        return out

    def _rank_categories(self, query: str, cats: list[_Cat]) -> list[_Cat]:
        if not cats:
            return []
        bm = BM25([tokenize(c.label) for c in cats])
        ranked = [(i, s) for i, s in bm.rank(query) if s > 0]
        if not ranked:
            return []
        return [cats[i] for i, _ in ranked]

    def _detail_links(self, category_url: str) -> list[str]:
        """server_view.php product links found on a category list page."""
        try:
            resp = net.get(category_url, check_robots=False, use_cache=True)
        except net.FetchError:
            return []
        doc = _parse(resp.content, resp.declared_encoding)
        out, seen = [], set()
        for a in doc.xpath('//a[contains(@href,"server_view.php")]'):
            href = urljoin(category_url, a.get("href", ""))
            idx = urlsplit(href).query
            if idx and idx not in seen:
                seen.add(idx)
                out.append(href)
        return out

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []
        cats = self._rank_categories(query, self._catalog())
        if not cats:
            return []

        # Collect product detail links from the best-matching categories until
        # we have enough, remembering which category each came from.
        detail: list[tuple[str, str]] = []   # (detail_url, category_label)
        seen: set[str] = set()
        for cat in cats:
            for du in self._detail_links(cat.url):
                q = urlsplit(du).query
                if q not in seen:
                    seen.add(q)
                    detail.append((du, cat.label))
            if len(detail) >= max_results:
                break

        # If a category has no drill-in products, fall back to returning the
        # category pages themselves so the caller still gets something useful.
        if not detail:
            return [SearchResult(title=c.label, url=c.url, snippet="구성별 견적",
                                 source="site:itmaya.co.kr") for c in cats[:max_results]]

        results: list[SearchResult] = []
        for du, cat_label in detail[:max_results]:
            try:
                resp = net.get(du, check_robots=False, use_cache=True)
                info = self._parse_product(
                    _parse(resp.content, resp.declared_encoding),
                    _PageCtx(raw=resp.content, url=du, encoding=resp.declared_encoding))
            except net.FetchError:
                info = {}
            title = info.get("model") or "Itmaya 서버"
            # Snippet carries the real base price; full per-component prices are
            # on the detail page (open_url).
            cat = cat_label.split()[-1] if cat_label else "서버"
            snippet = f"[{cat}] {self._price_note(info)}"
            tel = info.get("tel")
            if tel:
                snippet += f" · ☎{tel}"
            results.append(SearchResult(title=title, url=du, snippet=snippet,
                                        source="site:itmaya.co.kr"))
        return results

    # -- product parsing -------------------------------------------------- #
    # The real prices ARE in the page: estimate.js computes the total as
    # (system base) + Σ(selected component .pay × qty). The .total_price /
    # .sum_price spans are only JS-overwritten placeholders (identical on every
    # product), so we read the SOURCE fields instead — .price_system_start/_top
    # for the base config, and each component's .agree_txt/.pay_start/.pay_top.
    # This yields real, per-product, per-component prices.
    _SUMMARY_KEYS = ("견적예상가", "예상딜리버리", "예상견적시간")
    _CAVEAT = "예상가(VAT 별도), 최종 견적은 구성 확정 후 엔지니어 검토"

    @staticmethod
    def _num_cls(doc, cls: str) -> int:
        n = doc.xpath(f'//*[contains(concat(" ",normalize-space(@class)," ")," {cls} ")]')
        return _won(n[0].text_content()) if n else 0

    def _num_cls_adaptive(self, doc, cls: str, ctx: _PageCtx | None) -> int:
        """``_num_cls``, but if the class name has stopped matching, ask the
        adaptive layer to relocate the element it fingerprinted last time.

        Only the scalar prices get this treatment. Relocation finds *an
        element*; it cannot rebuild the component table's nested shape, so
        pretending otherwise there would invent numbers rather than recover
        them — and a wrong price is worse than a missing one.
        """
        val = self._num_cls(doc, cls)
        if val or ctx is None:
            return val
        text = adaptive.first_text(ctx.raw, ctx.url, f".{cls}",
                                   identifier=f"itmaya:{cls}", encoding=ctx.encoding)
        if not text:
            return 0
        log.info("itmaya: relocated .%s adaptively on %s", cls, ctx.url)
        return _won(text)

    def _parse_components(self, doc, ctx: _PageCtx | None = None):
        """Return (base_start, base_top, groups) where groups is a list of
        (category, required, min, max, [(option_name, price_start, price_top)])."""
        base_start = self._num_cls_adaptive(doc, "price_system_start", ctx)
        base_top = self._num_cls_adaptive(doc, "price_system_top", ctx)
        groups = []
        for g in doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," group_component ")]'):
            nm = g.xpath('./*[contains(concat(" ",normalize-space(@class)," ")," name ")]')
            name = _clean(nm[0].text_content()) if nm else ""
            opts = []
            for inp in g.xpath('.//input[@data-idx]'):
                label = inp.getparent()
                a = label.xpath('.//*[contains(@class,"agree_txt")]')
                oname = re.sub(r"^x\s+", "", _clean(a[0].text_content())) if a else ""
                ps = label.xpath('.//*[contains(@class,"pay_start")]')
                pt = label.xpath('.//*[contains(@class,"pay_top")]')
                p_start = _won(ps[0].text_content()) if ps else 0
                p_top = _won(pt[0].text_content()) if pt else 0
                if oname:
                    opts.append((oname, p_start, p_top))
            if name and opts:
                groups.append((name, g.get("data-required"), g.get("data-min"),
                               g.get("data-max"), opts))
        return base_start, base_top, groups

    def _parse_product(self, doc, ctx: _PageCtx | None = None) -> dict:
        info: dict = {}
        # Model name (incl. the config variant, e.g. "…24Core, 128GB, RTX…").
        for xp in ('//div[contains(@class,"r_box_fixed")]//div[contains(@class,"title")]',
                   '//div[contains(@class,"r_box")]//div[contains(@class,"title")]'):
            t = doc.xpath(xp)
            if t:
                info["model"] = _clean(t[0].text_content())
                break
        if not info.get("model") and ctx is not None:
            alt = adaptive.first_text(ctx.raw, ctx.url, ".r_box_fixed .title",
                                      identifier="itmaya:model", encoding=ctx.encoding)
            if alt:
                log.info("itmaya: relocated model title adaptively on %s", ctx.url)
                info["model"] = _clean(alt)
        # Sales contact (a genuinely useful, stable field for a quote site).
        tel = doc.xpath('//*[contains(@class,"tel")]//*[contains(@class,"num")]')
        if tel:
            info["tel"] = _clean(tel[0].text_content())
        # Delivery / quote-time hints from the dt/dd summary (these ARE real).
        # dt labels carry a trailing " :", so strip it before matching.
        summary: dict[str, str] = {}
        for dt in doc.xpath("//dt"):
            key = _label(dt.text_content())
            dd = dt.xpath("following-sibling::dd[1]")
            if key in ("예상딜리버리", "예상견적시간") and dd:
                summary[key] = _clean(dd[0].text_content())
        info["summary"] = summary
        # Base system price + itemised component price table.
        bs, bt, comps = self._parse_components(doc, ctx)
        info["base_start"], info["base_top"], info["components"] = bs, bt, comps
        return info

    def _price_note(self, info: dict) -> str:
        """One-line price summary (base config price) for a search snippet."""
        bs, bt = info.get("base_start", 0), info.get("base_top", 0)
        if bs:
            return f"시스템 기본가 {_won_str(bs, bt)}~ (구성요소 추가 시 합산, {self._CAVEAT})"
        return f"구성별 견적 ({self._CAVEAT})"

    # -- extraction override ---------------------------------------------- #
    def extract(self, url: str, raw: bytes, encoding: str | None) -> Extracted | None:
        path = urlsplit(url).path
        if "server_view.php" not in path:
            return None  # only product detail pages need the override
        try:
            doc = _parse(raw, encoding)
        except Exception:  # noqa: BLE001
            return None
        info = self._parse_product(doc, _PageCtx(raw=raw, url=url, encoding=encoding))
        model = info.get("model") or "Itmaya 서버"
        summary = info.get("summary", {})
        tel = info.get("tel", "")
        bs, bt = info.get("base_start", 0), info.get("base_top", 0)
        comps = info.get("components", [])

        lines = [f"# {model}", ""]
        if bs:
            lines.append(f"**시스템 기본가:** {_won_str(bs, bt)}  ({self._CAVEAT})")
        else:
            lines.append(f"**가격:** 구성별 견적 — {self._CAVEAT}")
        if tel:
            lines.append(f"**견적 문의:** {tel}")
        for k in ("예상딜리버리", "예상견적시간"):
            if summary.get(k):
                lines.append(f"**{k}:** {summary[k]}")

        # Real per-component price table (the point of this adapter).
        if comps:
            lines.append("")
            lines.append("## 구성요소별 가격 (기본가에 합산)")
            for name, req, _mn, mx, opts in comps:
                cons = []
                if req == "y":
                    cons.append("필수")
                if mx and mx not in ("0", None):
                    cons.append(f"최대 {mx}개")
                tag = f" ({', '.join(cons)})" if cons else ""
                lines.append(f"### {name}{tag}")
                for oname, ps, pt in opts:
                    lines.append(f"- {oname}: {_won_str(ps, pt)}")
        markdown = "\n".join(lines).strip()
        text = re.sub(r"[#*]", "", markdown)

        # Passages: a lead (model + base price) plus one per component group so
        # BM25 (site_search) surfaces the relevant group for e.g. "GPU 가격".
        lead = (f"{model} — 시스템 기본가 {_won_str(bs, bt)} ({self._CAVEAT})"
                if bs else f"{model} — 구성별 견적")
        if tel:
            lead += f" · 문의 {tel}"
        passages = [lead]
        for k in ("예상딜리버리", "예상견적시간"):
            if summary.get(k):
                passages.append(f"{k}: {summary[k]}")
        for name, _req, _mn, _mx, opts in comps:
            items = "; ".join(f"{o} {_won_str(ps, pt)}" for o, ps, pt in opts[:15])
            passages.append(f"{name} 가격: {items}")
        pricing = "itemized" if comps or bs else "quote-based"
        return Extracted(url=url, title=model, text=text, markdown=markdown,
                         passages=passages, links=[], meta={"host": "itmaya.co.kr",
                                                             "pricing": pricing})


# --------------------------------------------------------------------------- #

ADAPTERS: list[SiteAdapter] = [ItmayaAdapter()]


def adapter_for(host: str) -> SiteAdapter | None:
    host = (host or "").lower()
    for ad in ADAPTERS:
        if ad.handles(host):
            return ad
    return None
