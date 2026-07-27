"""Multi-provider model layer: pricing, usage accounting, wire-format
translation, connection status, and the agent loop's usage roll-up."""

import json

import pytest

from studyweb import providers as P
from studyweb import usage as U
from studyweb.config import settings
from studyweb.providers import ProviderError
from studyweb.usage import Usage, UsageLedger


# --------------------------------------------------------------------------- #
#  Registry                                                                    #
# --------------------------------------------------------------------------- #

def test_registry_covers_local_and_cloud():
    assert {"lmstudio", "openai", "anthropic", "nvidia", "claude-code", "custom"} <= set(P.PROVIDERS)
    for pid, p in P.PROVIDERS.items():
        assert p.id == pid
        assert p.kind in ("openai", "anthropic", "cli")
        assert p.requires_key is False or p.key_env, f"{pid} needs a key but names no env var"


def test_resolve_unknown_provider():
    with pytest.raises(ProviderError) as exc:
        P.resolve("gpt5-turbo-max")
    # naming a provider that doesn't exist is the caller's mistake, not a
    # backend failure — the server turns this kind into a 400.
    assert exc.value.kind == "unknown_provider"
    assert "known:" in str(exc.value)


def test_describe_reports_key_presence_not_the_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    d = P.describe(P.PROVIDERS["openai"])
    assert d["has_key"] is True and d["key_source"] == "env"
    assert "sk-secret-value" not in json.dumps(d)


def test_settings_key_overrides_env(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "from-env")
    monkeypatch.setitem(settings.provider_keys, "nvidia", "from-settings")
    assert P.api_key(P.PROVIDERS["nvidia"]) == "from-settings"


def test_base_url_override(monkeypatch):
    monkeypatch.setitem(settings.provider_base_urls, "custom", "http://localhost:8000/v1/")
    assert P.base_url(P.PROVIDERS["custom"]) == "http://localhost:8000/v1"


def test_current_claude_models_reject_sampling_params():
    # temperature/top_p/top_k are 400s on these; older models still accept them.
    assert not P._accepts_sampling("claude-opus-5")
    assert not P._accepts_sampling("claude-sonnet-5")
    assert not P._accepts_sampling("claude-fable-5")
    assert P._accepts_sampling("claude-haiku-4-5")
    assert P._accepts_sampling("claude-opus-4-6")


# --------------------------------------------------------------------------- #
#  Pricing + cost                                                              #
# --------------------------------------------------------------------------- #

def test_price_exact_then_prefix_then_unknown():
    assert U.price_for("anthropic", "claude-opus-5")["in"] == 5.0
    # longest prefix wins over a shorter one
    assert U.price_for("anthropic", "claude-haiku-4-5")["out"] == 5.0
    assert U.price_for("anthropic", "some-other-model") is None
    assert U.price_for("nvidia", "meta/llama-3.3-70b-instruct") is None


def test_local_models_are_free():
    assert U.estimate_cost("lmstudio", "anything-at-all", 100_000, 50_000) == 0.0


def test_estimate_cost_bills_cached_tokens_cheaper():
    plain = U.estimate_cost("anthropic", "claude-opus-5", 1_000_000, 0)
    cached = U.estimate_cost("anthropic", "claude-opus-5", 1_000_000, 0, cached_tokens=1_000_000)
    assert plain == 5.0
    assert cached == 0.5          # cache reads are a tenth of the input rate
    assert U.estimate_cost("anthropic", "claude-opus-5", 0, 1_000_000) == 25.0


def test_unpriced_model_reports_none_not_zero():
    assert U.estimate_cost("nvidia", "meta/llama-3.3-70b-instruct", 1000, 1000) is None


def test_user_pricing_override_merges_over_defaults(tmp_path, monkeypatch):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps({
        "_note": "ignored",
        "nvidia": {"meta/llama-3.3-70b-instruct": {"in": 0.2, "out": 0.2}},
        "anthropic": {"claude-opus-5": {"in": 1.0, "out": 2.0}},
    }), encoding="utf-8")
    monkeypatch.setenv("STUDYWEB_PRICING", str(path))
    U.load_pricing(refresh=True)
    try:
        assert U.price_for("nvidia", "meta/llama-3.3-70b-instruct")["in"] == 0.2
        assert U.price_for("anthropic", "claude-opus-5")["out"] == 2.0   # overridden
        assert U.price_for("anthropic", "claude-sonnet-5")["out"] == 15.0  # default kept
    finally:
        monkeypatch.delenv("STUDYWEB_PRICING")
        U.load_pricing(refresh=True)


def test_broken_pricing_file_falls_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "pricing.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("STUDYWEB_PRICING", str(path))
    U.load_pricing(refresh=True)
    try:
        assert U.price_for("anthropic", "claude-opus-5")["in"] == 5.0
    finally:
        monkeypatch.delenv("STUDYWEB_PRICING")
        U.load_pricing(refresh=True)


# --------------------------------------------------------------------------- #
#  Usage + ledger                                                              #
# --------------------------------------------------------------------------- #

def test_usage_add_and_pricing():
    a = Usage(provider="anthropic", model="claude-opus-5", prompt_tokens=1000,
              completion_tokens=100, latency_ms=500).priced()
    b = Usage(provider="anthropic", model="claude-opus-5", prompt_tokens=2000,
              completion_tokens=200, latency_ms=700).priced()
    total = a + b
    assert total.total_tokens == 3300
    assert total.requests == 2
    assert total.latency_ms == 1200
    assert total.cost_usd == pytest.approx(a.cost_usd + b.cost_usd)


def test_usage_add_keeps_unknown_cost_unknown():
    a = Usage(provider="nvidia", model="x", prompt_tokens=10).priced()
    b = Usage(provider="nvidia", model="x", completion_tokens=10).priced()
    assert (a + b).cost_usd is None
    # ...but a known cost on either side wins over the unknown one
    c = Usage(provider="lmstudio", model="y", prompt_tokens=10).priced()
    assert (a + c).cost_usd == 0.0


def test_estimate_tokens_counts_cjk_heavier():
    assert U.estimate_tokens("") == 0
    latin = U.estimate_tokens("a" * 100)
    korean = U.estimate_tokens("가" * 100)
    assert korean > latin


def test_ledger_accumulates_and_persists(tmp_path):
    led = UsageLedger(str(tmp_path), persist=True)
    led.record(Usage(provider="anthropic", model="claude-opus-5",
                     prompt_tokens=1_000_000, completion_tokens=0))
    led.record(Usage(provider="nvidia", model="unpriced", prompt_tokens=10,
                     completion_tokens=5))
    s = led.summary()
    assert s["session"]["requests"] == 2
    assert s["session"]["total_tokens"] == 1_000_015
    assert s["session"]["cost_usd"] == pytest.approx(5.0)
    assert s["session"]["unpriced_requests"] == 1
    assert s["providers"]["anthropic"]["requests"] == 1
    assert s["total"]["requests"] == 2
    assert len(s["recent"]) == 2

    # a fresh ledger over the same directory still sees the lifetime totals
    assert UsageLedger(str(tmp_path), persist=True).summary()["total"]["requests"] == 2


def test_ledger_reset_scopes(tmp_path):
    led = UsageLedger(str(tmp_path), persist=True)
    led.record(Usage(provider="lmstudio", model="m", prompt_tokens=5, completion_tokens=5))
    led.reset("session")
    assert led.summary()["session"]["requests"] == 0
    assert led.summary()["total"]["requests"] == 1     # history untouched
    led.reset("all")
    assert led.summary()["total"]["requests"] == 0


def test_ledger_survives_an_unwritable_directory(tmp_path):
    # Accounting must never break the call it is measuring.
    led = UsageLedger(str(tmp_path / "nope" / "\0bad"), persist=True)
    led.record(Usage(provider="lmstudio", model="m", prompt_tokens=1))
    assert led.summary()["session"]["requests"] == 1


# --------------------------------------------------------------------------- #
#  OpenAI shape  <->  Anthropic Messages API                                   #
# --------------------------------------------------------------------------- #

def test_to_anthropic_splits_system_and_merges_tool_results():
    system, msgs = P.to_anthropic([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "prices?"},
        {"role": "assistant", "content": "looking",
         "tool_calls": [{"id": "c1", "function": {"name": "web_search",
                                                  "arguments": '{"query": "gpu"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "{}"},
        {"role": "tool", "tool_call_id": "c2", "name": "open_url", "content": "{}"},
    ])
    assert system == "be terse"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    blocks = msgs[1]["content"]
    assert blocks[0] == {"type": "text", "text": "looking"}
    assert blocks[1]["type"] == "tool_use" and blocks[1]["input"] == {"query": "gpu"}
    # both tool results land on one user turn, as the API requires
    results = msgs[2]["content"]
    assert [b["tool_use_id"] for b in results] == ["c1", "c2"]


def test_to_anthropic_drops_empty_assistant_turn_and_bad_json():
    _, msgs = P.to_anthropic([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},                       # dropped
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "x", "function": {"name": "f", "arguments": "{oops"}}]},
    ])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"][0]["input"] == {}                     # unparseable -> {}


def test_tools_translate_both_ways():
    from studyweb.lms import TOOL_SCHEMAS
    atools = P.tools_to_anthropic(TOOL_SCHEMAS)
    assert {t["name"] for t in atools} >= {"web_search", "open_url"}
    assert all("input_schema" in t for t in atools)
    assert P.tools_to_anthropic(None) is None
    assert P.tools_to_anthropic([]) is None


def test_from_anthropic_rebuilds_openai_message():
    msg = P.from_anthropic({
        "content": [{"type": "text", "text": "here"},
                    {"type": "tool_use", "id": "t1", "name": "open_url",
                     "input": {"url": "https://x"}}],
        "stop_reason": "tool_use",
    })
    assert msg["content"] == "here"
    assert msg["tool_calls"][0]["function"]["name"] == "open_url"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"url": "https://x"}
    assert msg["finish_reason"] == "tool_use"


def test_from_anthropic_text_only():
    msg = P.from_anthropic({"content": [{"type": "text", "text": "hi"}]})
    assert msg["content"] == "hi" and "tool_calls" not in msg


# --------------------------------------------------------------------------- #
#  chat() over stubbed transports                                              #
# --------------------------------------------------------------------------- #

@pytest.fixture
def no_ledger(monkeypatch):
    """Keep tests from writing to the real ledger."""
    monkeypatch.setattr(U.ledger, "_persist", False)


def test_chat_openai_extracts_usage(monkeypatch, no_ledger):
    seen = {}

    def fake_request(p, method, path, *, json_body=None, timeout=None, endpoint=None):
        seen["path"], seen["body"] = path, json_body
        return {"choices": [{"message": {"role": "assistant", "content": "hello"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3,
                          "prompt_tokens_details": {"cached_tokens": 4}}}

    monkeypatch.setattr(P, "_request", fake_request)
    out = P.chat([{"role": "user", "content": "hi"}], provider="lmstudio", model="m")
    assert seen["path"] == "/chat/completions"
    assert out["message"]["content"] == "hello"
    assert out["usage"].prompt_tokens == 12
    assert out["usage"].cached_tokens == 4
    assert out["usage"].cost_usd == 0.0        # local model
    assert out["usage"].latency_ms >= 0


def test_chat_openai_estimates_usage_when_server_omits_it(monkeypatch, no_ledger):
    monkeypatch.setattr(P, "_request", lambda *a, **k: {
        "choices": [{"message": {"role": "assistant", "content": "x" * 400}}]})
    out = P.chat([{"role": "user", "content": "hi"}], provider="lmstudio", model="m")
    assert out["usage"].estimated is True
    assert out["usage"].completion_tokens > 0


def test_chat_anthropic_payload_and_usage(monkeypatch, no_ledger):
    seen = {}

    def fake_request(p, method, path, *, json_body=None, timeout=None, endpoint=None):
        seen["path"], seen["body"] = path, json_body
        return {"content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 100, "output_tokens": 10,
                          "cache_read_input_tokens": 900}}

    monkeypatch.setattr(P, "_request", fake_request)
    out = P.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                 provider="anthropic", model="claude-opus-5", temperature=0.5)
    assert seen["path"] == "/messages"
    assert seen["body"]["system"] == "s"
    assert seen["body"]["max_tokens"] >= 1
    # this model rejects sampling params, so they must not be sent
    assert "temperature" not in seen["body"]
    u = out["usage"]
    assert u.prompt_tokens == 1000 and u.cached_tokens == 900   # cached reads included
    assert u.cost_usd == pytest.approx((100 / 1e6) * 5 + (900 / 1e6) * 0.5 + (10 / 1e6) * 25)


def test_chat_anthropic_sends_temperature_to_older_models(monkeypatch, no_ledger):
    seen = {}

    def fake_request(p, method, path, *, json_body=None, timeout=None, endpoint=None):
        seen["body"] = json_body
        return {"content": [{"type": "text", "text": "hi"}], "usage": {}}

    monkeypatch.setattr(P, "_request", fake_request)
    P.chat([{"role": "user", "content": "u"}], provider="anthropic",
           model="claude-haiku-4-5", temperature=0.3)
    assert seen["body"]["temperature"] == 0.3


def test_chat_anthropic_retries_without_optional_params_on_400(monkeypatch, no_ledger):
    calls = []

    def fake_request(p, method, path, *, json_body=None, timeout=None, endpoint=None):
        calls.append(dict(json_body))
        if len(calls) == 1:
            raise ProviderError("HTTP 400: temperature: unexpected", kind="http", status=400)
        return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

    monkeypatch.setattr(P, "_request", fake_request)
    out = P.chat([{"role": "user", "content": "u"}], provider="anthropic",
                 model="claude-haiku-4-5", temperature=0.3)
    assert out["message"]["content"] == "ok"
    assert "temperature" in calls[0] and "temperature" not in calls[1]


def test_chat_missing_key_is_a_typed_error(monkeypatch, no_ledger):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(settings.provider_keys, "anthropic", "")
    with pytest.raises(ProviderError) as exc:
        P.chat([{"role": "user", "content": "u"}], provider="anthropic")
    assert exc.value.kind == "no_key"
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_chat_records_into_the_ledger(monkeypatch, tmp_path):
    led = UsageLedger(str(tmp_path), persist=False)
    monkeypatch.setattr(P, "ledger", led)
    monkeypatch.setattr(P, "_request", lambda *a, **k: {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2}})
    P.chat([{"role": "user", "content": "hi"}], provider="lmstudio", model="m")
    assert led.summary()["session"]["total_tokens"] == 9


# --------------------------------------------------------------------------- #
#  Connection status                                                           #
# --------------------------------------------------------------------------- #

def test_check_reports_no_key_without_dialling(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(settings.provider_keys, "openai", "")
    monkeypatch.setattr(P, "_request", lambda *a, **k: pytest.fail("should not dial"))
    out = P.check("openai")
    assert out["status"] == "no_key" and out["has_key"] is False
    assert "OPENAI_API_KEY" in out["detail"]


def test_check_ok_lists_models(monkeypatch):
    monkeypatch.setattr(P, "list_models", lambda pid, **k: ["a", "b"])
    monkeypatch.setitem(settings.provider_models, "lmstudio", "a")
    try:
        out = P.check("lmstudio")
    finally:
        settings.provider_models.pop("lmstudio", None)
    assert out["status"] == "ok" and out["models"] == ["a", "b"]


def test_check_flags_a_model_missing_from_the_list(monkeypatch):
    monkeypatch.setattr(P, "list_models", lambda pid, **k: ["a"])
    monkeypatch.setitem(settings.provider_models, "lmstudio", "ghost")
    try:
        out = P.check("lmstudio")
    finally:
        settings.provider_models.pop("lmstudio", None)
    assert out["status"] == "ok" and "ghost" in out["detail"]


def test_check_no_model_when_nothing_is_loaded(monkeypatch):
    monkeypatch.setattr(P, "list_models", lambda pid, **k: [])
    out = P.check("lmstudio")
    assert out["status"] == "no_model"


@pytest.mark.parametrize("kind,expected", [
    ("unauthorized", "unauthorized"),
    ("rate_limit", "rate_limit"),
    ("unreachable", "unreachable"),
    ("bad_response", "error"),
])
def test_check_maps_failures_to_states(monkeypatch, kind, expected):
    def boom(pid, **k):
        raise ProviderError("nope", kind=kind, provider=pid)
    monkeypatch.setattr(P, "list_models", boom)
    monkeypatch.setitem(settings.provider_keys, "openai", "k")
    out = P.check("openai")
    assert out["status"] == expected and out["detail"]


def test_server_maps_provider_failures_to_actionable_status_codes():
    from studyweb.server import _PROVIDER_STATUS
    assert _PROVIDER_STATUS["unknown_provider"] == 400
    assert _PROVIDER_STATUS["no_key"] == 428          # configure a key, then retry
    assert _PROVIDER_STATUS["unauthorized"] == 401
    assert _PROVIDER_STATUS["rate_limit"] == 429
    assert _PROVIDER_STATUS["unreachable"] == 502
    # anything unclassified must not masquerade as our own 500
    assert _PROVIDER_STATUS.get("weird", 502) == 502


def test_check_cli_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(P.shutil, "which", lambda _n: None)
    out = P.check("claude-code")
    assert out["status"] == "not_installed"


def test_list_models_falls_back_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(settings.provider_keys, "anthropic", "")

    def boom(*a, **k):
        raise ProviderError("no key", kind="no_key", provider="anthropic")

    monkeypatch.setattr(P, "_request", boom)
    ids = P.list_models("anthropic")
    assert "claude-opus-5" in ids


# --------------------------------------------------------------------------- #
#  Agent loop                                                                  #
# --------------------------------------------------------------------------- #

def test_agent_sums_usage_across_tool_rounds(monkeypatch, no_ledger):
    from studyweb import agent as A

    replies = [
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"id": "c1", "function": {"name": "web_search",
                                                              "arguments": '{"query": "q"}'}}]},
         "usage": Usage(provider="lmstudio", model="m", prompt_tokens=100,
                        completion_tokens=10),
         "provider": "lmstudio", "model": "m", "raw": {}},
        {"message": {"role": "assistant", "content": "final answer"},
         "usage": Usage(provider="lmstudio", model="m", prompt_tokens=300,
                        completion_tokens=20),
         "provider": "lmstudio", "model": "m", "raw": {}},
    ]
    monkeypatch.setattr(A, "chat", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(A, "dispatch_tool", lambda name, args: {"ok": True})

    events = []
    out = A.run_agent("prices?", provider="lmstudio", on_event=events.append)
    assert out["final"] == "final answer"
    assert out["usage"]["requests"] == 2
    assert out["usage"]["total_tokens"] == 430
    assert out["trace"][0]["tool"] == "web_search"
    assert [e["type"] for e in events] == ["tool", "answer"]


def test_agent_returns_provider_error_instead_of_raising(monkeypatch, no_ledger):
    from studyweb import agent as A

    def boom(*a, **k):
        raise ProviderError("key rejected", kind="unauthorized", provider="openai")

    monkeypatch.setattr(A, "chat", boom)
    out = A.run_agent("hi", provider="openai")
    assert out["final"] == ""
    assert out["error"]["kind"] == "unauthorized"


def test_agent_drops_tools_for_providers_that_cant_use_them(monkeypatch, no_ledger):
    from studyweb import agent as A
    seen = {}

    def fake_chat(messages, **kw):
        seen["tools"] = kw.get("tools")
        return {"message": {"role": "assistant", "content": "done"},
                "usage": Usage(provider="claude-code", model="c"),
                "provider": "claude-code", "model": "c", "raw": {}}

    monkeypatch.setattr(A, "chat", fake_chat)
    A.run_agent("hi", provider="claude-code")
    assert seen["tools"] is None
