"""Search provider glue: URL decoding, de-dup, domain filtering, auto-fallback.
All offline — providers/network are monkeypatched."""

import base64
import json

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


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _fake_naver(monkeypatch, payload, status=200):
    from studyweb import net
    from studyweb.config import settings
    monkeypatch.setattr(settings, "naver_client_id", "id")
    monkeypatch.setattr(settings, "naver_client_secret", "secret")
    seen = {}

    class S:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.update(url=url, params=params, headers=headers)
            return _FakeResp(payload, status)

    monkeypatch.setattr(net, "session", lambda: S())
    return seen


def test_naver_strips_the_markup_it_wraps_matches_in(monkeypatch):
    seen = _fake_naver(monkeypatch, {"items": [
        {"title": "<b>AMD</b> Ryzen 5 9600X &amp; 메인보드",
         "link": "https://example.com/a",
         "description": "<b>9600X</b> 가격 비교"}]})
    [r] = PROVIDERS["naver"]("9600X", 5)
    assert r.title == "AMD Ryzen 5 9600X & 메인보드"
    assert r.snippet == "9600X 가격 비교"
    assert r.source == "naver"
    assert seen["headers"]["X-Naver-Client-Id"] == "id"
    assert seen["params"]["display"] == 5


def test_naver_shop_returns_prices_as_numbers(monkeypatch):
    _fake_naver(monkeypatch, {"items": [
        {"title": "AMD 라이젠5 9600X", "link": "https://shopping.naver.com/x",
         "lprice": "289000", "hprice": "0", "mallName": "쿠팡",
         "productId": "42", "brand": "AMD", "maker": "AMD",
         "category1": "디지털/가전", "category2": "PC부품",
         "category3": "CPU", "category4": ""}]})
    [r] = PROVIDERS["naver_shop"]("9600X", 5)
    assert r.extra["price_low"] == 289000
    # "0" is Naver's "not disclosed", not a free product
    assert r.extra["price_high"] is None
    assert r.extra["mall"] == "쿠팡"
    assert r.extra["category"] == "디지털/가전 > PC부품 > CPU"
    assert r.snippet == "289,000원 · 쿠팡"


def test_naver_without_keys_is_a_clear_error(monkeypatch):
    from studyweb.config import settings
    monkeypatch.setattr(settings, "naver_client_id", "")
    monkeypatch.setattr(settings, "naver_client_secret", "")
    with pytest.raises(SearchError, match="NAVER_CLIENT_ID"):
        PROVIDERS["naver"]("q", 3)


def test_naver_joins_auto_but_the_shop_vertical_never_does(monkeypatch):
    from studyweb.search import _auto_order
    from studyweb.config import settings
    monkeypatch.setattr(settings, "search_disable", ())
    monkeypatch.setattr(settings, "naver_client_id", "id")
    monkeypatch.setattr(settings, "naver_client_secret", "secret")
    order = _auto_order()
    assert order[0] == "naver"          # Korean results first when configured
    assert "naver_shop" not in order    # product listings answer no web question


def test_disabled_provider_is_never_called(monkeypatch):
    """An engine that is unreachable from this network costs a full connect
    timeout on every search — twice when the recall retry fires. Disabling it
    must take it out of the auto chain entirely, not just tolerate its error."""
    from studyweb.search import _auto_order
    from studyweb.config import settings

    called = []

    def spy(name):
        def fn(q, n, market=None):
            called.append(name)
            return [] if name == "duckduckgo" else [
                SearchResult(title="t", url="https://example.com/a", snippet="s")]
        return fn

    monkeypatch.setitem(PROVIDERS, "bing", spy("bing"))
    monkeypatch.setitem(PROVIDERS, "duckduckgo", spy("duckduckgo"))
    monkeypatch.setattr(settings, "search_disable", ("duckduckgo",))

    assert "duckduckgo" not in _auto_order()
    search("anything")
    assert called == ["bing"]
