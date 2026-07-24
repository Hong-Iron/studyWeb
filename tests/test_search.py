"""Search provider glue: URL decoding, de-dup, domain filtering, auto-fallback.
All offline — providers/network are monkeypatched."""

import base64

import pytest

from studyweb.search import (SearchResult, _decode_bing_url, _decode_ddg_url,
                             _dedupe, search, SearchError, PROVIDERS)


def test_decode_bing_redirect_url():
    target = "https://en.wikipedia.org/wiki/Photosynthesis"
    b = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    wrapped = f"https://www.bing.com/ck/a?!&&p=abc&u=a1{b}&ntb=1"
    assert _decode_bing_url(wrapped) == target
    # non-wrapped passes through
    assert _decode_bing_url(target) == target


def test_dedupe_normalizes_urls():
    rs = [SearchResult("a", "https://X.com/p"),
          SearchResult("b", "https://x.com/p"),    # same but host-case differs
          SearchResult("c", "http://y.com/q")]
    out = _dedupe(rs)
    urls = [r.url for r in out]
    assert len(out) == 2 and "http://y.com/q" in urls


def _fake_provider(results):
    def fn(query, n, *rest):
        return results[:n]
    return fn


def test_include_exclude_domain_filter(monkeypatch):
    fixed = [SearchResult("A", "https://danawa.com/a", source="bing"),
             SearchResult("B", "https://samsung.com/b", source="bing"),
             SearchResult("C", "https://naver.com/c", source="bing")]
    monkeypatch.setitem(PROVIDERS, "bing", _fake_provider(fixed))
    inc = search("x", provider="bing", include_domains=["danawa.com"], max_results=5)
    assert [r.url for r in inc] == ["https://danawa.com/a"]
    exc = search("x", provider="bing", exclude_domains=["naver.com"], max_results=5)
    assert all("naver.com" not in r.url for r in exc) and len(exc) == 2


def test_auto_fallback_when_first_provider_fails(monkeypatch):
    def boom(query, n, *rest):
        raise RuntimeError("rate limited")
    good = _fake_provider([SearchResult("ok", "https://ok.com/1", source="wikipedia")])
    # with no API keys set (test env), auto order is [bing, duckduckgo, wikipedia];
    # fail the two general engines and the last resort should answer.
    monkeypatch.setitem(PROVIDERS, "bing", boom)
    monkeypatch.setitem(PROVIDERS, "duckduckgo", boom)
    monkeypatch.setitem(PROVIDERS, "wikipedia", good)
    out = search("x", provider="auto", max_results=3)
    assert out and out[0].url == "https://ok.com/1"


def test_explicit_provider_error_when_empty(monkeypatch):
    def boom(query, n, *rest):
        raise RuntimeError("nope")
    monkeypatch.setitem(PROVIDERS, "bing", boom)
    with pytest.raises(SearchError):
        search("x", provider="bing")


def test_decode_ddg_redirect_url():
    from urllib.parse import quote
    target = "https://en.wikipedia.org/wiki/Photosynthesis"
    wrapped = f"//duckduckgo.com/l/?uddg={quote(target, safe='')}&rut=abc"
    assert _decode_ddg_url(wrapped) == target
    # protocol-relative direct link is normalised to https
    assert _decode_ddg_url("//example.com/x") == "https://example.com/x"
    assert _decode_ddg_url(target) == target


def test_blank_query_returns_empty():
    assert search("   ") == []
