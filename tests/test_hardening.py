"""Regression tests for the production-hardening fixes: domain-boundary
matching, charset handling, SSRF guard, server auth/validation, retry parsing,
and CJK ranking. All offline."""

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from studyweb import net, server
from studyweb.config import settings
from studyweb.extract import extract
from studyweb.rank import tokenize
from studyweb.search import SearchResult, PROVIDERS, search


# --- P0-2: domain filters match on a domain boundary ------------------------

def _fake_provider(results):
    def fn(query, n, *rest):
        return results[:n]
    return fn


def test_include_domain_rejects_lookalike(monkeypatch):
    fixed = [SearchResult("A", "https://danawa.com/a", source="bing"),
             SearchResult("B", "https://notdanawa.com/b", source="bing"),
             SearchResult("C", "https://shop.danawa.com/c", source="bing")]
    monkeypatch.setitem(PROVIDERS, "bing", _fake_provider(fixed))
    out = search("x", provider="bing", include_domains=["danawa.com"], max_results=5)
    urls = {r.url for r in out}
    assert "https://danawa.com/a" in urls        # exact host
    assert "https://shop.danawa.com/c" in urls    # subdomain
    assert "https://notdanawa.com/b" not in urls  # look-alike rejected


def test_exclude_domain_boundary(monkeypatch):
    fixed = [SearchResult("A", "https://naver.com/a", source="bing"),
             SearchResult("B", "https://notnaver.com/b", source="bing")]
    monkeypatch.setitem(PROVIDERS, "bing", _fake_provider(fixed))
    out = search("x", provider="bing", exclude_domains=["naver.com"], max_results=5)
    urls = {r.url for r in out}
    assert "https://naver.com/a" not in urls
    assert "https://notnaver.com/b" in urls  # not excluded by suffix accident


# --- P0-3: non-UTF-8 pages are decoded correctly ----------------------------

def test_extract_honours_http_charset():
    html = "<html><body><p>한국어 텍스트입니다 테스트 문장</p></body></html>"
    raw = html.encode("euc-kr")
    ex = extract(raw, "https://x.kr/", encoding="euc-kr")
    assert "한국어 텍스트입니다" in ex.text


def test_extract_sniffs_meta_charset_when_header_absent():
    html = ('<html><head><meta charset="euc-kr"></head>'
            '<body><p>가격 비교 삼성전자 갤럭시 테스트</p></body></html>')
    raw = html.encode("euc-kr")
    ex = extract(raw, "https://x.kr/", encoding=None)  # no HTTP charset
    assert "삼성전자" in ex.text


# --- P0-5: SSRF guard -------------------------------------------------------

def test_is_private_ip():
    assert net._is_private_ip("127.0.0.1")
    assert net._is_private_ip("10.0.0.1")
    assert net._is_private_ip("169.254.169.254")  # cloud metadata
    assert net._is_private_ip("::1")
    assert not net._is_private_ip("8.8.8.8")


def test_guard_ssrf_blocks_loopback(monkeypatch):
    monkeypatch.setattr(settings, "allow_private_hosts", False)
    with pytest.raises(net.FetchError):
        net._guard_ssrf("localhost")


def test_guard_ssrf_bypass(monkeypatch):
    monkeypatch.setattr(settings, "allow_private_hosts", True)
    net._guard_ssrf("localhost")  # no raise


# --- P1-12: Retry-After parsing ---------------------------------------------

def test_retry_after_seconds():
    assert net._retry_after({"Retry-After": "5"}) == 5.0
    assert net._retry_after({"Retry-After": "9999"}) == 30.0  # capped
    assert net._retry_after({}) is None
    assert net._retry_after({"Retry-After": "garbage"}) is None


# --- P2-13: CJK bigram tokenisation -----------------------------------------

def test_tokenize_emits_cjk_bigrams():
    toks = tokenize("삼성전자")
    assert "삼성전자" in toks           # whole token kept
    assert "삼성" in toks and "전자" in toks  # bigrams for recall
    # a query with an attached particle still shares bigrams with the base term
    assert set(tokenize("삼성전자는")) & set(tokenize("삼성전자"))


def test_tokenize_latin_unchanged():
    assert tokenize("Hello World") == ["hello", "world"]


# --- P0-1 / P1-7: server auth on GET + input validation ---------------------

def _serve(monkeypatch, api_key=""):
    monkeypatch.setattr(settings, "api_key", api_key)
    monkeypatch.setattr(server, "_research_fn",
                        lambda q, **k: {"query": q, "answer": "", "results": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _req(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_get_search_requires_auth(monkeypatch):
    httpd, port = _serve(monkeypatch, api_key="secret")
    try:
        status, _ = _req(port, "GET", "/search?q=hello")
        assert status == 401  # previously this path bypassed auth entirely
        status, _ = _req(port, "GET", "/search?q=hello",
                         headers={"Authorization": "Bearer secret"})
        assert status == 200
        # health stays public
        status, _ = _req(port, "GET", "/health")
        assert status == 200
    finally:
        httpd.shutdown(); httpd.server_close()


def test_post_bad_input_is_400(monkeypatch):
    import json
    httpd, port = _serve(monkeypatch, api_key="")
    try:
        # missing query
        status, _ = _req(port, "POST", "/search",
                         headers={"Content-Type": "application/json"},
                         body=json.dumps({}))
        assert status == 400
        # rag with neither query nor urls
        status, _ = _req(port, "POST", "/rag",
                         headers={"Content-Type": "application/json"},
                         body=json.dumps({}))
        assert status == 400
        # non-integer max_results
        status, _ = _req(port, "POST", "/search",
                         headers={"Content-Type": "application/json"},
                         body=json.dumps({"query": "x", "max_results": "lots"}))
        assert status == 400
    finally:
        httpd.shutdown(); httpd.server_close()


# --- a client that hangs up mid-request is not a server error ---------------

def test_send_survives_a_client_that_hung_up():
    """A cancelled or timed-out client leaves us writing to a dead socket.
    That deserves a log line, not a traceback — and above all the error path
    must not call _send again, which is what turned one BrokenPipeError into
    a socketserver 'Exception occurred during processing' dump."""

    class DeadSocket:
        closed = False

        def write(self, _b):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

    h = server.Handler.__new__(server.Handler)
    h.wfile = DeadSocket()
    h.client_address = ("192.168.0.3", 49266)
    h.request_version = "HTTP/1.1"
    h.requestline = "POST /extract HTTP/1.1"
    h.log_request = lambda *a, **k: None

    h._send(200, {"results": []})          # the real response
    h._send(500, {"error": "whatever"})    # what the old error path retried
