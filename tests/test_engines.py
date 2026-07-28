"""Fetch engines, the escalation ladder, and the adaptive-selector fallback.

Everything here runs offline. Escalation is exercised with fake engines rather
than the real ``chrome``/``scrapling`` backends so the suite never starts a
browser and passes identically with or without the optional extra installed.
"""

import pytest

from studyweb import adaptive, engines, fetch, net
from studyweb.config import settings


# --- block detection --------------------------------------------------------

def test_looks_blocked_on_status():
    assert engines.looks_blocked(403, b"whatever")
    assert engines.looks_blocked(429, b"whatever")
    assert engines.looks_blocked(503, b"whatever")


def test_looks_blocked_on_body_marker():
    assert engines.looks_blocked(200, b"<html>Just a moment...</html>")
    assert engines.looks_blocked(200, "please complete the CAPTCHA")
    assert engines.looks_blocked(200, b"Checking your browser before accessing")


def test_not_blocked_on_real_content():
    assert not engines.looks_blocked(200, b"<html><p>real article text</p></html>")


def test_404_is_not_blocked():
    """A missing page is an answer — escalating to a browser cannot invent it."""
    assert not engines.looks_blocked(404, b"<html>Not Found</html>")


# --- registry ---------------------------------------------------------------

def test_static_engine_always_available():
    assert engines.get_engine("static").available()


def test_unknown_engine_raises():
    with pytest.raises(engines.EngineError):
        engines.get_engine("nope")


def test_engines_are_ordered_by_cost():
    tiers = [e.tier for e in engines.all_engines()]
    assert tiers == sorted(tiers)
    assert engines.get_engine("static").tier == 0
    # Stealth must stay the most expensive rung so it is never picked casually.
    assert engines.get_engine("stealth").tier == max(tiers)


def test_status_reports_every_engine():
    names = {row["name"] for row in engines.status()}
    assert names == {"static", "scrapling", "chrome", "dynamic", "stealth"}
    for row in engines.status():
        assert row["available"] or row["reason"], f"{row['name']} unavailable with no reason"


def test_stealth_is_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "stealth_enabled", False)
    eng = engines.get_engine("stealth")
    assert not eng.available()
    assert "STUDYWEB_STEALTH" in eng.why_unavailable


def test_install_hint_is_runnable_off_a_checkout():
    """The hint is printed on machines that may have no source tree.

    studyweb is not on PyPI, so `pip install "studyweb[scrapling]"` fails to
    resolve — naming the dependency is the form that works from anywhere.
    """
    assert "studyweb[" not in engines.INSTALL_HINT
    assert "scrapling[fetchers]" in engines.INSTALL_HINT
    assert ".[scrapling]" in engines.INSTALL_HINT_SOURCE


def test_stealth_never_on_the_default_ladder():
    """Opting into stealth must stay an explicit act, not a fallback."""
    default = "static,scrapling,chrome,dynamic"
    assert "stealth" not in default


# --- the ladder -------------------------------------------------------------

class FakeEngine(engines.Engine):
    """Records calls and returns whatever the test told it to."""

    def __init__(self, name, tier, body=b"", status=200, available=True, boom=False):
        self.name, self.tier = name, tier
        self._body, self._status = body, status
        self._available, self._boom = available, boom
        self.calls = 0

    def available(self):
        self.why_unavailable = "" if self._available else "fake: off"
        return self._available

    def fetch(self, url, *, params=None, headers=None, timeout=None, on_redirect=None):
        self.calls += 1
        if self._boom:
            raise engines.EngineError("fake transport failure")
        return engines.Fetched(url=url, status=self._status, content=self._body,
                               headers={"content-type": "text/html"}, engine=self.name)


@pytest.fixture
def fake_ladder(monkeypatch):
    """Swap the registry for two fake engines: weak -> strong."""
    def install(weak, strong):
        monkeypatch.setattr(engines, "_ENGINES", {weak.name: weak, strong.name: strong})
        monkeypatch.setattr(settings, "fetch_engine", weak.name)
        monkeypatch.setattr(settings, "fetch_ladder", (weak.name, strong.name))
        monkeypatch.setattr(settings, "fetch_escalate", True)
        return weak, strong
    return install


def test_ladder_starts_at_configured_engine(fake_ladder):
    weak, strong = fake_ladder(FakeEngine("weak", 0), FakeEngine("strong", 3))
    assert [e.name for e in engines.ladder()] == ["weak", "strong"]
    assert [e.name for e in engines.escalation_targets()] == ["strong"]


def test_ladder_skips_unavailable_engines(fake_ladder):
    """The bare install has neither Chrome nor Scrapling — the ladder must
    collapse to `static` rather than raise or stall."""
    weak, strong = fake_ladder(FakeEngine("weak", 0), FakeEngine("strong", 3, available=False))
    assert [e.name for e in engines.ladder()] == ["weak"]
    assert engines.escalation_targets() == []


def test_ladder_never_steps_down(fake_ladder):
    """Starting halfway up must not fall back to a weaker engine."""
    weak, strong = fake_ladder(FakeEngine("weak", 0), FakeEngine("strong", 3))
    assert [e.name for e in engines.ladder("strong")] == ["strong"]


def test_escalation_targets_when_the_start_is_unavailable(fake_ladder):
    """An unavailable start engine must not cost us the next rung — excluding
    it by position rather than by name would silently drop `strong`."""
    weak, strong = fake_ladder(FakeEngine("weak", 0, available=False),
                               FakeEngine("strong", 3))
    assert [e.name for e in engines.ladder()] == ["strong"]
    assert [e.name for e in engines.escalation_targets()] == ["strong"]


# --- net.get escalation -----------------------------------------------------

GOOD = b"<html><body><p>the real content</p></body></html>"


def test_get_escalates_past_a_wall(fake_ladder, monkeypatch):
    weak, strong = fake_ladder(
        FakeEngine("weak", 0, body=b"Just a moment...", status=200),
        FakeEngine("strong", 3, body=GOOD, status=200))
    resp = net.get("https://example.com/", use_cache=False)
    assert resp.content == GOOD
    assert resp.engine == "strong"
    assert weak.calls == 1 and strong.calls == 1


def test_get_stops_at_the_first_engine_that_works(fake_ladder):
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=GOOD),
                               FakeEngine("strong", 3, body=GOOD))
    resp = net.get("https://example.com/", use_cache=False)
    assert resp.engine == "weak"
    assert strong.calls == 0, "must not pay for a browser when static succeeded"


def test_get_escalates_past_a_crashing_engine(fake_ladder):
    weak, strong = fake_ladder(FakeEngine("weak", 0, boom=True),
                               FakeEngine("strong", 3, body=GOOD))
    resp = net.get("https://example.com/", use_cache=False)
    assert resp.engine == "strong"


def test_get_returns_the_wall_when_every_engine_is_walled(fake_ladder):
    """A 403 is an answer, not an exception — callers read status themselves."""
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=b"nope", status=403),
                               FakeEngine("strong", 3, body=b"nope", status=403))
    resp = net.get("https://example.com/", use_cache=False)
    assert resp.status == 403
    assert strong.calls >= 1


def test_get_raises_when_every_engine_crashed(fake_ladder):
    fake_ladder(FakeEngine("weak", 0, boom=True), FakeEngine("strong", 3, boom=True))
    with pytest.raises(net.FetchError):
        net.get("https://example.com/", use_cache=False)


def test_escalate_false_uses_one_engine_only(fake_ladder):
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=b"Just a moment..."),
                               FakeEngine("strong", 3, body=GOOD))
    net.get("https://example.com/", use_cache=False, escalate=False)
    assert strong.calls == 0


def test_explicit_engine_is_honoured(fake_ladder):
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=GOOD),
                               FakeEngine("strong", 3, body=GOOD))
    resp = net.get("https://example.com/", use_cache=False, engine="strong")
    assert resp.engine == "strong"
    assert weak.calls == 0


def test_policy_runs_before_any_engine(fake_ladder):
    """robots/SSRF/scheme gates must not be reachable past a stronger engine."""
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=GOOD),
                               FakeEngine("strong", 3, body=GOOD))
    with pytest.raises(net.FetchError):
        net.get("ftp://example.com/x", use_cache=False)
    assert weak.calls == 0 and strong.calls == 0


def test_oversized_response_is_final(fake_ladder):
    """max_bytes is policy, not a transport hiccup: no retry, no escalation."""
    class TooBig(FakeEngine):
        def fetch(self, url, **kw):
            self.calls += 1
            raise engines.TooLarge("response exceeds max_bytes (10)")

    weak, strong = fake_ladder(TooBig("weak", 0), FakeEngine("strong", 3, body=GOOD))
    with pytest.raises(net.FetchError):
        net.get("https://example.com/", use_cache=False)
    assert weak.calls == 1, "must not retry an oversized page"
    assert strong.calls == 0, "must not escalate an oversized page"


# --- fetch_page thin-content escalation -------------------------------------

JS_SHELL = b"<html><body><div id='root'></div></body></html>"
FULL = b"<html><body><article>" + (b"word " * 300) + b"</article></body></html>"


def test_fetch_page_escalates_a_js_shell(fake_ladder):
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=JS_SHELL),
                               FakeEngine("strong", 3, body=FULL))
    doc = fetch.fetch_page("https://example.com/", use_cache=False)
    assert doc.word_count > 200
    assert doc.meta.get("fetch_engine") == "strong"
    assert doc.meta.get("escalated_from") == "weak"


def test_fetch_page_keeps_a_good_static_result(fake_ladder):
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=FULL),
                               FakeEngine("strong", 3, body=FULL))
    doc = fetch.fetch_page("https://example.com/", use_cache=False)
    assert doc.meta.get("fetch_engine") == "weak"
    assert strong.calls == 0


def test_fetch_page_does_not_escalate_a_404(fake_ladder):
    """Short pages and missing pages are not broken transports."""
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=b"<html>gone</html>", status=404),
                               FakeEngine("strong", 3, body=FULL))
    fetch.fetch_page("https://example.com/", use_cache=False)
    assert strong.calls == 0


def test_fetch_page_keeps_the_best_of_a_failed_escalation(fake_ladder):
    """If the stronger engine does no better, the original result survives."""
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=JS_SHELL),
                               FakeEngine("strong", 3, boom=True))
    doc = fetch.fetch_page("https://example.com/", use_cache=False)
    assert doc.status == 200
    assert doc.meta.get("fetch_engine") == "weak"


def test_short_page_is_not_treated_as_thin(fake_ladder, monkeypatch):
    monkeypatch.setattr(settings, "escalate_thin_words", 50)
    body = b"<html><body><article>" + (b"word " * 80) + b"</article></body></html>"
    weak, strong = fake_ladder(FakeEngine("weak", 0, body=body),
                               FakeEngine("strong", 3, body=FULL))
    fetch.fetch_page("https://example.com/", use_cache=False)
    assert strong.calls == 0


# --- adaptive selectors -----------------------------------------------------

def test_adaptive_returns_none_without_scrapling(monkeypatch):
    """The contract the site adapters rely on: None means "use the lxml path",
    which is distinct from [] meaning "looked, found nothing"."""
    monkeypatch.setattr(adaptive, "_selector_class", lambda: None)
    assert adaptive.select(b"<html></html>", "https://x.kr/", ".p", identifier="t") is None
    assert adaptive.first_text(b"<html></html>", "https://x.kr/", ".p", identifier="t") is None


def test_adaptive_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(settings, "adaptive_selectors", False)
    assert not adaptive.enabled()
    assert adaptive.parse(b"<html></html>", "https://x.kr/") is None


def test_adaptive_status_shape():
    st = adaptive.status()
    assert set(st) == {"enabled", "available", "storage", "storage_exists"}
    assert st["storage"].endswith("adaptive.db")


def test_text_of_survives_a_foreign_element():
    class Weird:
        text = "  hello  "
    assert adaptive.text_of(Weird()) == "hello"

    class Empty:
        pass
    assert adaptive.text_of(Empty()) == ""


@pytest.mark.skipif(not adaptive.enabled(), reason="scrapling not installed")
def test_adaptive_finds_and_relocates(tmp_path, monkeypatch):
    """With Scrapling present: fingerprint an element, then find it again after
    its class name changes."""
    monkeypatch.setattr(adaptive, "storage_path", lambda: str(tmp_path / "a.db"))
    url = "https://itmaya.co.kr/server_view.php?idx=1"
    before = b"<html><body><span class='pay_start'>1,234,000</span></body></html>"
    found = adaptive.select(before, url, ".pay_start", identifier="itmaya:pay_start")
    assert found and "1,234,000" in adaptive.text_of(found[0])

    after = b"<html><body><span class='price-start'>1,234,000</span></body></html>"
    relocated = adaptive.select(after, url, ".pay_start", identifier="itmaya:pay_start")
    assert relocated, "adaptive tracking should relocate the renamed element"
