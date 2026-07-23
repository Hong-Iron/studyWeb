"""LLM tool schemas / dispatch and server auth + pipeline shaping."""

import json

from studyweb import lms
from studyweb.lms import TOOL_SCHEMAS, dispatch_tool, _truncate, tool_result_message
from studyweb.server import _auth_ok
from studyweb.config import settings


def test_tool_schemas_wellformed():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert {"web_search", "open_url", "collect_rag"} <= names
    for t in TOOL_SCHEMAS:
        fn = t["function"]
        assert t["type"] == "function"
        assert "parameters" in fn and fn["parameters"]["type"] == "object"


def test_truncate():
    assert _truncate("abc", 10) == "abc"
    assert _truncate("abcdef", 3).startswith("abc") and "truncated" in _truncate("abcdef", 3)
    assert _truncate(None, 5) == ""


def test_dispatch_unknown_tool():
    assert "error" in dispatch_tool("nope", {})


def test_dispatch_web_search(monkeypatch):
    fake = {"query": "q", "answer": "the answer",
            "results": [{"title": "T", "url": "https://u/1", "content": "C" * 5000}]}
    monkeypatch.setattr(lms, "_research_fn", lambda *a, **k: fake)
    out = dispatch_tool("web_search", {"query": "q", "max_results": 1})
    assert out["query"] == "q" and out["answer"] == "the answer"
    assert out["results"][0]["url"] == "https://u/1"
    assert len(out["results"][0]["content"]) < 5000  # truncated to budget


def test_dispatch_open_url(monkeypatch):
    class D:
        ok = True; url = "https://u/1"; title = "T"; markdown = "# body"; text = "body"
        status = 200; error = ""
    monkeypatch.setattr(lms, "fetch_page", lambda url: D())
    out = dispatch_tool("open_url", {"url": "https://u/1"})
    assert out["title"] == "T" and "body" in out["content"]


def test_tool_result_message_shape():
    m = tool_result_message("id1", "web_search", {"a": 1})
    assert m["role"] == "tool" and m["tool_call_id"] == "id1"
    assert json.loads(m["content"]) == {"a": 1}


def test_server_auth(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    assert _auth_ok({}, {}) is True                     # no key configured
    monkeypatch.setattr(settings, "api_key", "secret")
    assert _auth_ok({}, {"api_key": "secret"}) is True
    assert _auth_ok({"Authorization": "Bearer secret"}, {}) is True
    assert _auth_ok({}, {"api_key": "wrong"}) is False
