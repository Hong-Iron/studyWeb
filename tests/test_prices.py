"""Multi-site price lookup. All offline — site search and page fetches are
monkeypatched, so the fixtures below are the only 'web' involved."""

import pytest

from studyweb import prices
from studyweb.config import settings
from studyweb.search import SearchResult

JSONLD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"AMD 라이젠5-6세대 9600X (벌크 정품)","brand":{"name":"AMD"},
 "offers":{"@type":"Offer","price":"265000","priceCurrency":"KRW"}}
</script></head><body>본문</body></html>"""

BARE = "<html><body><p>가격 정보 없음</p></body></html>"

# Coupang answers a bot with 200 OK and this, so nothing about the product is
# verified — but the row that led here still carries a price.
DENIED = "<html><head><title>Access Denied</title></head><body>Access Denied</body></html>"

# Compuzone in miniature: no markup at all, a decoy price hidden behind
# display:none with the real digits sitting in its *tail*, and an unlabelled
# promo banner earlier in the document.
DECOY = """<html><head><meta property="og:title" content="[AMD] 라이젠5 9600X : 컴퓨존"/>
</head><body>
  <div class="live"><p class="Live_prd_name">래플 상품</p>
                    <p class="Live_price_name"><span>99%</span> 100원</p></div>
  <div class="pd info_price"><h3>판매가</h3>
    <div class="price_real"><div style="display:none;">256,000</div>259,000<span>원</span></div>
  </div>
  <div class="card"><p>[토스페이] 50,000원 즉시할인 (1,000,000원 이상 결제 시)</p></div>
</body></html>"""


class _Resp:
    def __init__(self, html):
        self.content = html.encode("utf-8")
        self.declared_encoding = "utf-8"
        self.text = html


def _wire(monkeypatch, hits: dict, pages: dict):
    """hits: {site: [SearchResult, …]}   pages: {url: html}"""
    def site_search(site, query, *, max_results=6, market=None):
        return hits.get(site, [])[:max_results]

    def get(url, **kw):
        if url not in pages:
            raise RuntimeError("404")
        return _Resp(pages[url])

    monkeypatch.setattr(prices.sitesearch, "site_search", site_search)
    monkeypatch.setattr(prices.net, "get", get)


# --- the parser -------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("265,000원", 265000),
    ("리뷰 1,234건 · 265,000원", 265000),     # the review count must not win
    ("₩265,000", 265000),
    ("9600X 최저가 260,710원", 260710),
    ("AMD 라이젠5 9600X", None),               # a model number is not a price
    ("별점 4.7 리뷰수(41)", None),
    ("", None),
])
def test_a_price_needs_its_currency(text, expected):
    assert prices.price_from_text(text) == expected


def test_structured_price_fields_stand_without_a_currency_marker():
    # JSON-LD already said "this is the price", so bare digits are fine there.
    assert prices.price_from_field("265000") == 265000
    assert prices.price_from_field(260710.0) == 260710
    assert prices.price_from_field("") is None


# --- reading a price off the page itself ------------------------------------

def _dom(html):
    from lxml import html as LH
    return LH.fromstring(html)


def test_dom_price_beats_a_hidden_decoy_and_an_unlabelled_banner():
    # 256,000 is a display:none decoy, 100원 is a promo with no price label,
    # 50,000원 is a coupon. Only the number under 판매가 is the price.
    assert prices.price_from_dom(_dom(DECOY)) == 259000


def test_dom_price_needs_a_label():
    # A bare amount on a page is as likely to be shipping or a coupon; a miss
    # the caller can see beats a number they'd have to double-check.
    assert prices.price_from_dom(_dom(
        "<html><body><div>무료배송 3,000원</div></body></html>")) is None
    assert prices.price_from_dom(_dom(BARE)) is None


def test_dom_price_ignores_a_struck_through_list_price():
    assert prices.price_from_dom(_dom(
        '<html><body><div><span>판매가</span>'
        '<del>300,000원</del><strong>259,000원</strong></div></body></html>')) == 259000


def test_page_name_drops_the_site_suffix():
    assert prices._page_name(_dom(DECOY)) == "[AMD] 라이젠5 9600X"


# --- the pipeline -----------------------------------------------------------

def test_a_page_with_no_markup_still_yields_its_labelled_price(monkeypatch):
    _wire(monkeypatch,
          hits={"compuzone.co.kr": [SearchResult("AMD(소켓AM5) / 6코어 / 12쓰레드",
                                                 "https://c/1")]},
          pages={"https://c/1": DECOY})
    [q] = prices.find_prices("9600X", sites=["compuzone.co.kr"])["quotes"]
    assert q["price"] == 259000 and q["method"] == "dom"
    # the listing linked spec text, so the page's own name has to win
    assert q["title"] == "[AMD] 라이젠5 9600X"


def test_a_miss_names_its_cause(monkeypatch):
    _wire(monkeypatch, hits={"11st.co.kr": [SearchResult("t", "https://e/1")]},
          pages={})
    def blocked(url, **kw):
        raise RuntimeError(f"blocked by robots.txt: {url}")
    monkeypatch.setattr(prices.net, "get", blocked)
    [miss] = prices.find_prices("9600X", sites=["11st.co.kr"])["misses"]
    assert "blocked by robots.txt" in miss["reason"]


def test_price_comes_from_the_product_page_not_the_listing(monkeypatch):
    _wire(monkeypatch,
          hits={"danawa.com": [SearchResult("리뷰수(41)", "https://d/1")]},
          pages={"https://d/1": JSONLD})
    out = prices.find_prices("9600X", sites=["danawa.com"])
    [q] = out["quotes"]
    assert q["price"] == 265000 and q["method"] == "json-ld"
    # the listing's link text was junk; the page's own name replaces it
    assert q["title"].startswith("AMD 라이젠5")
    assert out["summary"]["min"] == 265000


def test_listing_price_is_the_fallback_when_a_page_has_no_markup(monkeypatch):
    _wire(monkeypatch,
          hits={"danawa.com": [SearchResult("265,000원", "https://d/2")]},
          pages={"https://d/2": BARE})
    [q] = prices.find_prices("9600X", sites=["danawa.com"])["quotes"]
    assert q["price"] == 265000 and q["method"] == "listing"


# --- a search-results row, which is a whole card flattened into one string ---
# Verbatim shapes from coupang.com's rows for "AMD 라이젠5 9600X".

COUPANG_ROW = ("AMD 라이젠5-6세대 9600X (그래니트 릿지) (멀티팩(정품)) "
               "(369,000원29%259,000원배송비 2,500원내일(수) 도착 예정")
COUPANG_AD = ("AMD 라이젠 5 7500F CPU, 옵션1할인216,080원17%179,340원홍콩"
              "7/31(금) 도착 예정무료배송(11)최대 8,967원 적립광고")


@pytest.mark.parametrize("row,expected", [
    (COUPANG_ROW, 259000),          # the sale price, not the struck-through one
    (COUPANG_AD, 179340),
    ("265,000원", 265000),          # danawa: one number, nothing to choose from
    ("배송비 2,500원", None),        # shipping is not what the product costs
    ("최대 8,967원 적립", None),     # nor are the points it earns
    ("무료배송", None),
])
def test_a_listing_row_yields_the_price_you_would_pay(row, expected):
    assert prices.price_from_listing(row) == expected


@pytest.mark.parametrize("title,other", [
    (COUPANG_AD, True),                              # a 7500F ad answering 9600X
    ("AMD 라이젠 5 5600 CPU187,370원", True),
    (COUPANG_ROW, False),                            # names the model we asked for
    ("265,000원", False),                            # names no model at all
    ("포유컴퓨터 퍼포먼스PC 36 R5 9600X RTX5060", False),   # a PC *containing* it
])
def test_a_row_for_another_model_is_recognisable(title, other):
    assert prices.names_another_product(title, "AMD 라이젠5 9600X") is other


def test_ad_rows_for_other_models_never_become_the_minimum(monkeypatch):
    # What actually happened on coupang: the page fetch is walled off, so both
    # rows fall back to their listing price — and the ad's is the lower one.
    _wire(monkeypatch,
          hits={"coupang.com": [SearchResult(COUPANG_ROW, "https://c/1"),
                                SearchResult(COUPANG_AD, "https://c/2")]},
          pages={"https://c/1": DENIED, "https://c/2": DENIED})
    out = prices.find_prices("AMD 라이젠5 9600X", sites=["coupang.com"])
    assert [q["price"] for q in out["quotes"]] == [259000]
    assert out["summary"]["min"] == 259000
    # and the bot wall's title never passes for the product's name
    assert "Access Denied" not in out["quotes"][0]["title"]


def test_a_site_that_only_returns_ads_is_a_miss_that_says_so(monkeypatch):
    _wire(monkeypatch,
          hits={"coupang.com": [SearchResult(COUPANG_AD, "https://c/2")]},
          pages={"https://c/2": DENIED})
    out = prices.find_prices("AMD 라이젠5 9600X", sites=["coupang.com"])
    assert out["quotes"] == [] and out["summary"] is None
    assert "other products" in out["misses"][0]["reason"]


@pytest.mark.parametrize("typed,expected", [
    ("compuzone.com", "compuzone.co.kr"),    # the .com 403s; the shop is .co.kr
    ("www.compuzone.com", "compuzone.co.kr"),
    ("danawa.com", None),                    # already the domain we search
    ("someshop.io", None),                   # nothing to confuse it with
])
def test_a_shop_named_under_the_wrong_tld_has_a_known_sibling(typed, expected):
    from studyweb import sitesearch
    assert sitesearch.sibling_site(typed) == expected


def test_an_empty_domain_falls_back_to_the_shop_that_sells(monkeypatch):
    # The model answered "컴퓨존에서" with sites=["compuzone.com"], which exists
    # and returns nothing to anyone.
    _wire(monkeypatch,
          hits={"compuzone.co.kr": [SearchResult("라이젠5 9600X", "https://c/1")]},
          pages={"https://c/1": JSONLD})
    out = prices.find_prices("9600X", sites=["compuzone.com"])
    [q] = out["quotes"]
    assert q["price"] == 265000
    assert q["site"] == "compuzone.co.kr"      # tagged with the domain that answered
    assert out["misses"] == []


def test_a_site_that_yields_nothing_is_reported_not_silently_dropped(monkeypatch):
    _wire(monkeypatch,
          hits={"danawa.com": [SearchResult("x", "https://d/1")], "coupang.com": []},
          pages={"https://d/1": JSONLD})
    out = prices.find_prices("9600X", sites=["danawa.com", "coupang.com"])
    assert len(out["quotes"]) == 1
    assert [m["site"] for m in out["misses"]] == ["coupang.com"]


def test_pages_without_a_price_are_a_miss_with_a_reason(monkeypatch):
    _wire(monkeypatch,
          hits={"11st.co.kr": [SearchResult("라이젠5 9600X", "https://e/1")]},
          pages={"https://e/1": BARE})
    out = prices.find_prices("9600X", sites=["11st.co.kr"])
    assert out["quotes"] == [] and out["summary"] is None
    assert "none priced" in out["misses"][0]["reason"]
    assert "no price in the page" in out["misses"][0]["reason"]


def test_summary_ranks_by_price_and_keeps_the_cheapest_per_site(monkeypatch):
    _wire(monkeypatch, hits={
        "danawa.com": [SearchResult("a", "https://d/hi"), SearchResult("b", "https://d/lo")],
        "11st.co.kr": [SearchResult("c", "https://e/mid")],
    }, pages={
        "https://d/hi": JSONLD.replace("265000", "300000"),
        "https://d/lo": JSONLD.replace("265000", "200000"),
        "https://e/mid": JSONLD.replace("265000", "250000"),
    })
    out = prices.find_prices("9600X", sites=["danawa.com", "11st.co.kr"])
    assert [q["price"] for q in out["quotes"]] == [200000, 250000, 300000]
    s = out["summary"]
    assert (s["min"], s["median"], s["max"]) == (200000, 250000, 300000)
    assert s["by_site"] == {"danawa.com": 200000, "11st.co.kr": 250000}
    assert s["cheapest_url"] == "https://d/lo"


def test_naver_keys_swap_crawling_for_the_api(monkeypatch):
    """Naver publishes prices over an API, so crawling its JS shell is pointless
    when the keys are there."""
    from studyweb.search import PROVIDERS
    monkeypatch.setattr(settings, "naver_client_id", "id")
    monkeypatch.setattr(settings, "naver_client_secret", "secret")
    monkeypatch.setitem(PROVIDERS, "naver_shop", lambda q, n: [
        SearchResult("AMD 9600X", "https://shop/1", source="naver_shop",
                     extra={"price_low": 259000, "mall": "쿠팡", "brand": "AMD"})])

    def boom(*a, **k):
        raise AssertionError("must not crawl when the API is available")
    monkeypatch.setattr(prices.sitesearch, "site_search", boom)

    [q] = prices.find_prices("9600X", sites=["shopping.naver.com"])["quotes"]
    assert q["price"] == 259000 and q["method"] == "naver_api" and q["snippet"] == "쿠팡"
