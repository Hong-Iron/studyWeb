"""Offline tests for the general structured-extraction stack:
structured-data parsing, headless-render degradation, and the orchestrator
cascade (with the LLM and browser mocked)."""

import pytest

from studyweb import structured, render, dataextract
from studyweb.config import settings


# --- Layer 1: structured markup --------------------------------------------

JSONLD = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"RTX 4090 PC",
 "brand":{"@type":"Brand","name":"ACME"},
 "offers":{"@type":"Offer","price":"3290000","priceCurrency":"KRW",
           "availability":"https://schema.org/InStock"}}
</script></head><body><h1>PC</h1></body></html>
"""

JSONLD_GRAPH = """
<html><head>
<script type="application/ld+json">
{"@graph":[{"@type":"BreadcrumbList"},
 {"@type":"Product","name":"Widget","offers":[
    {"@type":"Offer","price":"10.00","priceCurrency":"USD"},
    {"@type":"Offer","price":"12.00","priceCurrency":"USD"}]}]}
</script></head><body></body></html>
"""

MICRODATA = """
<html><body>
<div itemscope itemtype="https://schema.org/Product">
  <span itemprop="name">Cable</span>
  <span itemprop="price" content="4900">4,900</span>
  <meta itemprop="priceCurrency" content="KRW">
</div></body></html>
"""

OPENGRAPH = """
<html><head>
<meta property="og:type" content="product">
<meta property="og:title" content="Headset">
<meta property="product:price:amount" content="59000">
<meta property="product:price:currency" content="KRW">
</head><body></body></html>
"""


def test_jsonld_product():
    d = structured.extract_structured(JSONLD.encode("utf-8"), "https://x/p")
    assert d and d["source"] == "json-ld"
    assert d["name"] == "RTX 4090 PC" and d["price"] == "3290000"
    assert d["currency"] == "KRW" and d["availability"] == "InStock"
    assert d["brand"] == "ACME" and d["url"] == "https://x/p"


def test_jsonld_graph_and_multiple_offers():
    d = structured.extract_structured(JSONLD_GRAPH.encode("utf-8"))
    assert d and d["name"] == "Widget"
    assert d["price"] == "10.00" and len(d["offers"]) == 2


def test_microdata_product():
    d = structured.extract_structured(MICRODATA.encode("utf-8"))
    assert d and d["source"] == "microdata"
    assert d["name"] == "Cable" and d["price"] == "4900"


def test_opengraph_product():
    d = structured.extract_structured(OPENGRAPH.encode("utf-8"))
    assert d and d["source"] == "opengraph"
    assert d["name"] == "Headset" and d["price"] == "59000"


def test_structured_none_when_absent():
    assert structured.extract_structured(b"<html><body>hi</body></html>") is None


# --- headless render: graceful degradation ---------------------------------

def test_render_disabled(monkeypatch):
    monkeypatch.setattr(settings, "render_enabled", False)
    assert render.render_html("https://example.com") is None
    assert render.available() is False


def test_render_no_binary(monkeypatch):
    monkeypatch.setattr(settings, "render_enabled", True)
    monkeypatch.setattr(render, "chrome_binary", lambda: None)
    assert render.available() is False
    assert render.render_html("https://example.com") is None


def test_render_rejects_bad_scheme(monkeypatch):
    monkeypatch.setattr(settings, "render_enabled", True)
    monkeypatch.setattr(render, "chrome_binary", lambda: "/usr/bin/google-chrome")
    assert render.render_html("ftp://example.com/x") is None


# --- orchestrator cascade ---------------------------------------------------

class _Doc:
    def __init__(self, ok=True, wc=500, md="content here"):
        self.ok, self._wc, self.markdown, self.text = ok, wc, md, md
    @property
    def word_count(self):
        return self._wc


def test_cascade_uses_structured_when_present(monkeypatch):
    # static fetch returns a page; structured parse finds a product -> no LLM.
    monkeypatch.setattr(dataextract, "fetch_page", lambda u: _Doc())
    monkeypatch.setattr(dataextract.net, "get", lambda u, **k: _Resp(JSONLD))
    monkeypatch.setattr(dataextract, "extract_structured",
                        lambda *a, **k: {"source": "json-ld", "name": "X", "price": "100"})
    called = {"llm": False}
    monkeypatch.setattr(dataextract, "llm_extract",
                        lambda *a, **k: called.__setitem__("llm", True) or {})
    out = dataextract.extract_data("https://x/p", render_mode="never")
    assert out["method"] == "structured:json-ld"
    assert out["data"]["name"] == "X" and called["llm"] is False


def test_cascade_falls_back_to_llm(monkeypatch):
    monkeypatch.setattr(dataextract, "fetch_page", lambda u: _Doc(md="Price: 5000 KRW"))
    monkeypatch.setattr(dataextract.net, "get", lambda u, **k: _Resp("<html></html>"))
    monkeypatch.setattr(dataextract, "extract_structured", lambda *a, **k: None)
    monkeypatch.setattr(dataextract, "llm_extract",
                        lambda content, schema, **k: {"price": "5000", "currency": "KRW"})
    out = dataextract.extract_data("https://x/p", schema=["price"], render_mode="never")
    assert out["method"] == "llm" and out["data"]["price"] == "5000"


def test_cascade_warns_when_render_wanted_but_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "recover_urls", False)  # isolate the render path
    monkeypatch.setattr(dataextract, "fetch_page", lambda u: _Doc(ok=False, wc=0, md=""))
    monkeypatch.setattr(dataextract.net, "get", lambda u, **k: _Resp("<html></html>"))
    monkeypatch.setattr(dataextract, "extract_structured", lambda *a, **k: None)
    monkeypatch.setattr(dataextract.render, "available", lambda: False)
    out = dataextract.extract_data("https://x/p", render_mode="always", use_llm=False)
    assert any("headless browser" in w for w in out["warnings"])
    assert out["method"] == "none"


class _Resp:
    def __init__(self, html):
        self.content = html.encode("utf-8")
        self.declared_encoding = None
