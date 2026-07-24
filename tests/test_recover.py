"""Offline tests for URL recovery (hallucinated / 404 URL fallback)."""

import pytest

from studyweb import recover, lms
from studyweb.search import SearchResult


# --- keyword & variant derivation -------------------------------------------

def test_url_keywords_keeps_model_codes_drops_ids():
    kw = recover.url_keywords("https://itmaya.co.kr/products/esc4000-gpu-server?idx=99999")
    assert "esc4000" in [k.lower() for k in kw]
    assert "gpu" in [k.lower() for k in kw]
    # pure-number id and CMS words are dropped
    assert "99999" not in kw
    assert not any(k.lower() in ("products", "idx", "view", "php") for k in kw)


def test_url_keywords_drops_ids_and_scaffolding():
    # 'view'/'php'/'idx'/'401' are dropped; a real word like 'server' is kept
    kw = recover.url_keywords("https://itmaya.co.kr/server_view.php?idx=401")
    assert kw == ["server"]

    # a URL that is ONLY scaffolding + a numeric id yields nothing to search on
    assert recover.url_keywords("https://site.com/goods/view?no=12345") == []


def test_url_variants():
    v = recover.url_variants("http://www.example.com/path/")
    # toggles https, drops www, strips trailing slash — original excluded
    assert "http://www.example.com/path/" not in v
    assert any(x.startswith("https://") for x in v)
    assert any("://example.com/" in x for x in v)


# --- open_best cascade ------------------------------------------------------

class _Doc:
    def __init__(self, ok, url="", title="T", md="body"):
        self.ok, self.url, self.title, self.markdown, self.text = ok, url, title, md, md
        self.status = 200 if ok else 404
        self.error = "" if ok else "not found"


def test_open_best_recovers_via_candidate(monkeypatch):
    bad = "https://itmaya.co.kr/gpu-server-esc4000"   # hallucinated path
    good = "https://itmaya.co.kr/server_view.php?idx=401"

    def fake_fetch(u, **k):
        return _Doc(True, url=good) if u == good else _Doc(False, url=u)
    monkeypatch.setattr("studyweb.fetch.fetch_page", fake_fetch)
    monkeypatch.setattr(recover, "recover_candidates",
                        lambda url, **k: [SearchResult("ESC4000", good, source="site")])

    doc, real_url, cands = recover.open_best(bad)
    assert doc is not None and doc.ok and real_url == good
    assert cands and cands[0].url == good


def test_open_best_tries_variants_first(monkeypatch):
    bad = "https://example.com/page"
    fixed = "https://www.example.com/page"   # www variant works

    def fake_fetch(u, **k):
        return _Doc(True, url=fixed) if u == fixed else _Doc(False, url=u)
    monkeypatch.setattr("studyweb.fetch.fetch_page", fake_fetch)
    called = {"search": False}
    monkeypatch.setattr(recover, "recover_candidates",
                        lambda *a, **k: called.__setitem__("search", True) or [])

    doc, real_url, cands = recover.open_best(bad)
    assert doc is not None and real_url == fixed
    assert called["search"] is False   # variant hit — no search needed


def test_open_best_returns_candidates_when_nothing_opens(monkeypatch):
    monkeypatch.setattr("studyweb.fetch.fetch_page", lambda u, **k: _Doc(False, url=u))
    cand = SearchResult("maybe", "https://x/real", source="site")
    monkeypatch.setattr(recover, "recover_candidates", lambda url, **k: [cand])
    doc, real_url, cands = recover.open_best("https://x/dead")
    assert doc is None and real_url is None and cands == [cand]


# --- tool-level behaviour (open_url) ----------------------------------------

def test_open_url_tool_auto_recovers(monkeypatch):
    good = "https://itmaya.co.kr/server_view.php?idx=401"
    monkeypatch.setattr(lms, "fetch_page", lambda u: _Doc(False, url=u))  # first call fails
    monkeypatch.setattr("studyweb.recover.open_best",
                        lambda url, **k: (_Doc(True, url=good, md="ESC4000 details"),
                                          good,
                                          [SearchResult("ESC4000", good, source="site"),
                                           SearchResult("alt", "https://itmaya.co.kr/x", source="site")]))
    out = lms.dispatch_tool("open_url", {"url": "https://itmaya.co.kr/wrong"})
    assert out["url"] == good and "ESC4000" in out["content"]
    assert out["recovered_from"] == "https://itmaya.co.kr/wrong"
    assert "alternatives" in out


def test_open_url_tool_suggests_when_no_open(monkeypatch):
    monkeypatch.setattr(lms, "fetch_page", lambda u: _Doc(False, url=u))
    monkeypatch.setattr("studyweb.recover.open_best",
                        lambda url, **k: (None, None,
                                          [SearchResult("cand", "https://x/1", source="site")]))
    out = lms.dispatch_tool("open_url", {"url": "https://x/dead"})
    assert "suggestions" in out and out["suggestions"][0]["url"] == "https://x/1"


def test_open_url_tool_guidance_when_unrecoverable(monkeypatch):
    monkeypatch.setattr(lms, "fetch_page", lambda u: _Doc(False, url=u))
    monkeypatch.setattr("studyweb.recover.open_best", lambda url, **k: (None, None, []))
    out = lms.dispatch_tool("open_url", {"url": "https://nope.invalid/x"})
    assert "error" in out and "web_search" in out["note"]
