"""Fully-local site search: URL building, form discovery, link harvesting."""

import studyweb.sitesearch as ss
from studyweb.sitesearch import build_search_url, _norm_site, _canon, site_search
from studyweb.extract import Link


def test_norm_site():
    assert _norm_site("https://www.Danawa.com/foo") == "danawa.com"
    assert _norm_site("DANAWA.COM") == "danawa.com"


def test_build_search_url_known_site():
    url = build_search_url("danawa.com", "갤럭시 탭 S11")
    assert url.startswith("https://search.danawa.com/dsearch.php?query=")
    assert "%EA%B0%A4" in url  # query is URL-encoded
    # subdomain still resolves to the registry entry
    assert build_search_url("prod.danawa.com", "x").startswith("https://search.danawa.com")


def test_build_search_url_encodes_amazon():
    url = build_search_url("amazon.com", "usb c cable")
    assert url == "https://www.amazon.com/s?k=usb+c+cable"


def test_canon_dedups_same_product_variants():
    # The plain link and two review/opinion deep-links are the same product.
    a = _canon("https://prod.danawa.com/info/?pcode=96985325&keyword=abc&cate=122577")
    b = _canon("https://prod.danawa.com/info/?pcode=96985325&keyword=zzz&cate=122577&bookmark=cm_opinion&companyReviewYN=N")
    c = _canon("https://prod.danawa.com/info/?pcode=96985325&keyword=q&cate=122577&bookmark=cm_opinion&companyReviewYN=Y")
    assert a == b == c
    assert "pcode=96985325" in a
    # a different product must NOT collapse into it
    assert _canon("https://prod.danawa.com/info/?pcode=96985328&cate=122577") != a


def test_discover_template_from_form(monkeypatch):
    html = """<html><body>
      <form action="/results" method="get">
        <input type="text" name="search_query" />
      </form></body></html>"""
    class R:  # minimal net.Response stand-in
        text = html
    monkeypatch.setattr(ss.net, "get", lambda url, **k: R())
    ss._discovered.clear()
    tmpl = ss._discover_template("example.com")
    assert tmpl == "https://example.com/results?search_query={q}"
    assert build_search_url("example.com", "hi") == "https://example.com/results?search_query=hi"


def test_site_search_harvests_and_dedupes(monkeypatch):
    # Fake a Danawa results page: two products (one duplicated), plus off-site
    # and homepage links that must be filtered out.
    class FakeDoc:
        ok = True
        links = [
            Link("https://prod.danawa.com/info/?pcode=1&keyword=a", "Product One"),
            Link("https://prod.danawa.com/info/?pcode=1&keyword=b", "Product One review"),
            Link("https://prod.danawa.com/info/?pcode=2&keyword=a", "Product Two"),
            Link("https://www.danawa.com/", "home"),          # homepage -> skip
            Link("https://ad.doubleclick.net/x", "ad"),        # off-site -> skip
            Link("https://event.danawa.com/promo", "promo"),   # no /info -> skip (pattern)
        ]
    monkeypatch.setattr(ss, "fetch_page", lambda url: FakeDoc())
    out = site_search("danawa.com", "galaxy", max_results=5)
    urls = [r.url for r in out]
    assert len(out) == 2                                   # pcode 1 deduped
    assert any("pcode=1" in u for u in urls)
    assert any("pcode=2" in u for u in urls)
    assert all("danawa.com" in u for u in urls)
    assert out[0].source == "site:danawa.com"


def test_site_search_unknown_site_no_template(monkeypatch):
    monkeypatch.setattr(ss, "_discover_template", lambda s: None)
    assert site_search("no-such-site.invalid", "x") == []
