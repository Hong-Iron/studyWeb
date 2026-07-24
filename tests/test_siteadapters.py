"""Offline tests for the per-site adapter layer, using the Itmaya adapter.

The network is monkeypatched: net.get returns canned HTML keyed by URL so the
catalog -> category -> detail search flow runs fully offline.
"""

import pytest

from studyweb import net, siteadapters
from studyweb.siteadapters import ItmayaAdapter, adapter_for


# --- fixtures ---------------------------------------------------------------

HOME_HTML = """
<html><body>
  <div class="sub_set">
    <div class="menu_tit"><div class="tit">GPU서버</div></div>
    <ul class="sub">
      <li>
        <p class="tit">4GPU Server</p>
        <p class="s_tit"><a href="/server_list.php?category_s=196">ESC4000-E11 (5th INTEL)</a></p>
      </li>
    </ul>
  </div>
  <div class="sub_set">
    <div class="menu_tit"><div class="tit">스토리지/파일서버</div></div>
    <ul class="sub">
      <li>
        <p class="tit">Network Storage</p>
        <p class="s_tit"><a href="/server_list.php?category_s=210">ASUSTOR NAS</a></p>
      </li>
    </ul>
  </div>
</body></html>
"""

LIST_196 = """
<html><body>
  <a href="/server_view.php?idx=401">모델 옵션 선택</a>
  <a href="/server_view.php?idx=402">모델 옵션 선택</a>
</body></html>
"""

# The `total_price` span is the JS-overwritten static PLACEHOLDER (identical on
# every product) — the adapter must NEVER surface it. The real prices live in
# `.price_system_*` (base config) and each component's `.agree_txt`/`.pay_*`.
DETAIL_401 = """
<html><head><meta charset="utf-8"></head><body>
  <div class="r_box"><div class="r_box_fixed">
    <div class="title">ASUS GPU Server ESC4000-E11</div>
    <div class="cash"><span class="num">0</span><span class="won">원</span></div>
    <div class="tel"><span class="ko">전화</span><span class="num">02-713-1256</span></div>
  </div></div>
  <span class="num total_price">50,000,000</span>
  <span class="num total_price_top">65,000,000</span>
  <span class="pay price_system_start">6,910,000</span>
  <span class="pay price_system_top">7,141,000</span>
  <dl>
    <dt>예상딜리버리 :</dt><dd>2주 이내</dd>
    <dt>예상견적시간 :</dt><dd>영업시간 기준 2시간 이내</dd>
  </dl>
  <form>
    <div class="group_box group_component" data-required="y" data-min="1" data-max="1">
      <div class="name">Memory</div>
      <div class="group">
        <label><input type="checkbox" data-idx="1200" data-catem="Memory">
          <span class="chk"><span class="inner_txt">
            <span class="agree_txt">x 64GB DDR5 ECC (32GB x2)</span>
            <span class="pay">3,770,000</span>
            <span class="pay_start" style="display:none">3,770,000</span>
            <span class="pay_top" style="display:none">3,896,000</span>
          </span></span></label>
        <label><input type="checkbox" data-idx="1201" data-catem="Memory">
          <span class="chk"><span class="inner_txt">
            <span class="agree_txt">x 128GB DDR5 ECC (32GB x4)</span>
            <span class="pay">7,541,000</span>
            <span class="pay_start" style="display:none">7,541,000</span>
            <span class="pay_top" style="display:none">7,792,000</span>
          </span></span></label>
      </div>
    </div>
    <div class="group_box group_component" data-required="n" data-min="0" data-max="4">
      <div class="name">GPU</div>
      <div class="group">
        <label><input type="checkbox" data-idx="1300" data-catem="GPU">
          <span class="chk"><span class="inner_txt">
            <span class="agree_txt">x NVIDIA RTX PRO 4000 24GB</span>
            <span class="pay">3,613,000</span>
            <span class="pay_start" style="display:none">3,613,000</span>
            <span class="pay_top" style="display:none">3,733,000</span>
          </span></span></label>
      </div>
    </div>
  </form>
</body></html>
"""


def _resp(url, html):
    return net.Response(url=url, status=200, content=html.encode("utf-8"),
                        headers={"content-type": "text/html; charset=utf-8"})


def _fake_net(monkeypatch):
    def fake_get(url, **kw):
        if url.rstrip("/").endswith("itmaya.co.kr"):
            return _resp(url, HOME_HTML)
        if "category_s=196" in url:
            return _resp(url, LIST_196)
        if "server_view.php" in url:
            return _resp(url, DETAIL_401)
        # empty list for other categories
        return _resp(url, "<html><body></body></html>")
    monkeypatch.setattr(net, "get", fake_get)


# --- routing ----------------------------------------------------------------

def test_adapter_routing():
    assert isinstance(adapter_for("itmaya.co.kr"), ItmayaAdapter)
    assert isinstance(adapter_for("www.itmaya.co.kr"), ItmayaAdapter)
    assert adapter_for("notitmaya.co.kr") is None   # domain-boundary safe
    assert adapter_for("danawa.com") is None


# --- catalog + search (offline) ---------------------------------------------

def test_catalog_parses_menu(monkeypatch):
    _fake_net(monkeypatch)
    cats = ItmayaAdapter()._catalog()
    labels = " ".join(c.label for c in cats)
    assert any("ESC4000-E11" in c.label for c in cats)
    assert "GPU서버" in labels and "스토리지" in labels


def test_search_ranks_and_returns_products(monkeypatch):
    _fake_net(monkeypatch)
    res = ItmayaAdapter().search("ESC4000 GPU", max_results=2)
    assert res, "expected product results"
    assert res[0].title == "ASUS GPU Server ESC4000-E11"
    assert res[0].url.endswith("server_view.php?idx=401")
    # snippet carries the real base price, contact, and never the placeholder
    assert "6,910,000" in res[0].snippet     # real system base price
    assert "50,000,000" not in res[0].snippet  # JS placeholder, never surfaced
    assert "02-713-1256" in res[0].snippet


# --- product extraction (offline, no network) -------------------------------

def test_extract_itemises_component_prices():
    # fetch_page passes the HTTP-header charset; do the same here.
    ex = ItmayaAdapter().extract(
        "https://itmaya.co.kr/server_view.php?idx=401",
        DETAIL_401.encode("utf-8"), "utf-8")
    assert ex is not None
    assert ex.title == "ASUS GPU Server ESC4000-E11"
    # the JS-overwritten placeholder must NEVER be surfaced
    assert "50,000,000" not in ex.text and "65,000,000" not in ex.text
    # real base price + real per-component prices ARE surfaced
    assert "6,910,000" in ex.text                    # system base
    assert "Memory" in ex.text and "GPU" in ex.text  # component groups
    assert "128GB DDR5 ECC (32GB x4)" in ex.text and "7,541,000" in ex.text
    assert "NVIDIA RTX PRO 4000 24GB" in ex.text and "3,613,000" in ex.text
    assert "02-713-1256" in ex.text                  # sales contact
    assert "2주 이내" in ex.text                       # real delivery estimate
    assert ex.meta.get("pricing") == "itemized"


def test_extract_only_overrides_detail_pages():
    # category list / other pages fall back to the generic extractor (None)
    assert ItmayaAdapter().extract(
        "https://itmaya.co.kr/server_list.php?category_s=196", b"<html></html>", None) is None
