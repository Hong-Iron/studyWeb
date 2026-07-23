"""HTTP layer helpers (URL normalise, cache, concurrency) and robots gate."""

import pytest

from studyweb import net, robots
from studyweb.config import settings
from studyweb.net import Response, FetchError, normalize_url, get_many


def test_normalize_url():
    assert normalize_url("HTTP://Example.COM/Path") == "http://example.com/Path"
    assert normalize_url("https://a.com") == "https://a.com/"
    assert normalize_url("//weird") .startswith("https")  # scheme defaulted


def test_response_text_and_content_type():
    r = Response(url="u", status=200, content="café".encode("utf-8"),
                 headers={"content-type": "text/html; charset=utf-8"})
    assert r.text == "café"
    assert r.content_type == "text/html"


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "cache_enabled", True)
    monkeypatch.setattr(settings, "cache_ttl", 1000)
    r = Response(url="https://x.com/", status=200, content=b"hello",
                 headers={"content-type": "text/html"})
    net._cache_put("k1", r)
    got = net._cache_get("k1")
    assert got is not None and got.content == b"hello" and got.from_cache


def test_cache_skips_non_200(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "cache_enabled", True)
    net._cache_put("k404", Response("u", 404, b"x", {}))
    assert net._cache_get("k404") is None


def test_get_rejects_bad_scheme():
    with pytest.raises(FetchError):
        net.get("ftp://example.com/file")


def test_get_many_order_and_exception_capture():
    def worker(u):
        if u == "bad":
            raise ValueError("boom")
        return u.upper()
    out = get_many(["a", "bad", "c"], worker=worker)
    assert out[0] == "A" and out[2] == "C"
    assert isinstance(out[1], Exception)


def test_robots_disallow(monkeypatch):
    from urllib.robotparser import RobotFileParser
    rp = RobotFileParser()
    rp.parse(["User-agent: *", "Disallow: /private"])
    monkeypatch.setattr(settings, "respect_robots", True)
    monkeypatch.setattr(robots, "_load", lambda url: rp)
    robots._cache.clear()
    assert robots.allowed("https://site.com/public") is True
    assert robots.allowed("https://site.com/private/x") is False


def test_robots_respect_disabled(monkeypatch):
    monkeypatch.setattr(settings, "respect_robots", False)
    assert robots.allowed("https://anything.com/private") is True
