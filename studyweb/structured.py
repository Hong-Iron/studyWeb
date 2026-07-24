"""Deterministic structured-data extraction — Layer 1 of the general extractor.

Reads the machine-readable product/price data that a large fraction of real
e-commerce sites already embed for Google rich results:

  * JSON-LD   (``<script type="application/ld+json">`` schema.org/Product)
  * Microdata (``itemtype=".../Product"``, ``itemprop="price"``)
  * OpenGraph / meta (``product:price:amount``, ``og:price:amount``)

No LLM, no network — pure parsing. Returns a normalized dict or None.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from lxml import html as LH


def _parse(source, encoding: str | None):
    if hasattr(source, "xpath"):
        return source
    if isinstance(source, str):
        source = source.encode("utf-8", "replace")
    parser = LH.HTMLParser(encoding=encoding) if encoding else LH.HTMLParser()
    return LH.fromstring(source, parser=parser)


def _walk(node, out: list):
    """Collect every dict in a nested JSON-LD structure (objects, arrays, @graph)."""
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def _types(obj: dict) -> set[str]:
    t = obj.get("@type") or obj.get("type") or ""
    if isinstance(t, list):
        return {str(x).lower() for x in t}
    return {str(t).lower()}


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _text(v) -> str:
    v = _first(v)
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or v.get("url") or ""
    return str(v).strip() if v is not None else ""


def _offers(obj: dict) -> list[dict]:
    raw = obj.get("offers")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for off in items:
        if not isinstance(off, dict):
            continue
        spec = off.get("priceSpecification") if isinstance(off.get("priceSpecification"), dict) else {}
        price = off.get("price") or off.get("lowPrice") or spec.get("price") or ""
        high = off.get("highPrice") or ""
        out.append({
            "price": str(price).strip() if price != "" else "",
            "high_price": str(high).strip() if high != "" else "",
            "currency": _text(off.get("priceCurrency")),
            "availability": _text(off.get("availability")).split("/")[-1],
        })
    return out


def _from_jsonld(doc) -> dict | None:
    nodes: list = []
    for s in doc.xpath('//script[@type="application/ld+json"]/text()'):
        txt = (s or "").strip()
        if not txt:
            continue
        try:
            nodes.append(json.loads(txt))
        except json.JSONDecodeError:
            # some sites concatenate objects or trail commas; try a lenient pass
            try:
                nodes.append(json.loads(re.sub(r",\s*([}\]])", r"\1", txt)))
            except json.JSONDecodeError:
                continue
    flat: list = []
    for n in nodes:
        _walk(n, flat)

    products = [o for o in flat if "product" in _types(o)
                or (isinstance(o, dict) and o.get("offers"))]
    if not products:
        return None
    p = max(products, key=lambda o: len(_offers(o)) * 10 + (1 if o.get("name") else 0))
    offers = _offers(p)
    top = offers[0] if offers else {}
    return {
        "source": "json-ld",
        "type": next(iter(_types(p) & {"product"}), "product"),
        "name": _text(p.get("name")),
        "price": top.get("price", ""),
        "high_price": top.get("high_price", ""),
        "currency": top.get("currency", ""),
        "availability": top.get("availability", ""),
        "brand": _text(p.get("brand")),
        "sku": _text(p.get("sku") or p.get("mpn") or p.get("gtin13")),
        "description": _text(p.get("description"))[:500],
        "offers": offers,
    }


def _from_microdata(doc) -> dict | None:
    scopes = doc.xpath('//*[contains(@itemtype,"schema.org/Product")]')
    if not scopes:
        return None
    scope = scopes[0]

    def prop(name):
        els = scope.xpath(f'.//*[@itemprop="{name}"]')
        for el in els:
            v = (el.get("content") or el.get("value")
                 or el.text_content()).strip()
            if v:
                return v
        return ""

    price = prop("price") or prop("lowPrice")
    if not (price or prop("name")):
        return None
    return {
        "source": "microdata",
        "type": "product",
        "name": prop("name"),
        "price": price,
        "high_price": prop("highPrice"),
        "currency": prop("priceCurrency"),
        "availability": prop("availability").split("/")[-1],
        "brand": prop("brand"),
        "sku": prop("sku"),
        "description": prop("description")[:500],
        "offers": [{"price": price, "currency": prop("priceCurrency")}] if price else [],
    }


def _meta(doc, *names) -> str:
    for name in names:
        for xp in (f'//meta[@property="{name}"]/@content',
                   f'//meta[@name="{name}"]/@content',
                   f'//meta[@itemprop="{name}"]/@content'):
            v = doc.xpath(xp)
            if v and v[0].strip():
                return v[0].strip()
    return ""


def _from_opengraph(doc) -> dict | None:
    price = _meta(doc, "product:price:amount", "og:price:amount", "price:amount")
    name = _meta(doc, "og:title")
    if not (price or (name and _meta(doc, "og:type").lower().startswith("product"))):
        return None
    if not price and "product" not in _meta(doc, "og:type").lower():
        return None
    return {
        "source": "opengraph",
        "type": "product",
        "name": name,
        "price": price,
        "high_price": "",
        "currency": _meta(doc, "product:price:currency", "og:price:currency"),
        "availability": _meta(doc, "product:availability", "og:availability"),
        "brand": _meta(doc, "product:brand", "og:brand"),
        "sku": "",
        "description": _meta(doc, "og:description")[:500],
        "offers": [{"price": price}] if price else [],
    }


def extract_structured(source, url: str = "", encoding: str | None = None) -> dict | None:
    """Best structured product/price record from a page, or None if absent.

    Tries JSON-LD, then microdata, then OpenGraph — richest first.
    """
    try:
        doc = _parse(source, encoding)
    except Exception:  # noqa: BLE001
        return None
    for fn in (_from_jsonld, _from_microdata, _from_opengraph):
        data = fn(doc)
        if data and (data.get("name") or data.get("price")):
            if url:
                data["url"] = url
            return data
    return None
