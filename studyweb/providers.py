"""One chat interface over local *and* cloud models.

studyweb started as "local model + web tools". This module keeps that default
but lets you point the same tool-calling loop at a hosted model instead:

    from studyweb.providers import chat, check, PROVIDERS

    out = chat([{"role": "user", "content": "hi"}], provider="anthropic")
    print(out["message"]["content"], out["usage"].to_dict())

Every provider speaks the OpenAI message shape at this boundary — messages with
``role``/``content``, assistant ``tool_calls``, ``role: "tool"`` results — and
each adapter translates to and from its own wire format. So the agent loop,
the HTTP API and the Obsidian plugin all stay provider-agnostic.

Every call returns a :class:`~studyweb.usage.Usage` and is recorded in the
ledger, and :func:`check` reports whether a provider is reachable, missing its
key, or erroring — which is what the GUI's status lights read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict

import requests

from .config import settings
from .usage import Usage, estimate_tokens, ledger, price_for

# --------------------------------------------------------------------------- #
#  Registry                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    kind: str                 # "openai" | "anthropic" | "cli"
    base_url: str             # default endpoint (overridable per provider)
    key_env: str              # environment variable holding the API key
    default_model: str
    requires_key: bool = True
    local: bool = False       # runs on this machine — no network, no bill
    supports_tools: bool = True
    supports_stream: bool = True
    docs: str = ""
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "lmstudio": Provider(
        id="lmstudio", label="LM Studio (local)", kind="openai",
        base_url="http://localhost:1234/v1", key_env="LMSTUDIO_API_KEY",
        default_model="", requires_key=False, local=True,
        docs="https://lmstudio.ai/docs/app/api",
        note="Whatever model is loaded in the LM Studio server. Free, private, offline.",
    ),
    "openai": Provider(
        id="openai", label="OpenAI", kind="openai",
        base_url="https://api.openai.com/v1", key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
        docs="https://platform.openai.com/api-keys",
    ),
    "anthropic": Provider(
        id="anthropic", label="Claude (Anthropic API)", kind="anthropic",
        base_url="https://api.anthropic.com/v1", key_env="ANTHROPIC_API_KEY",
        default_model="claude-opus-5",
        docs="https://console.anthropic.com/settings/keys",
    ),
    "nvidia": Provider(
        id="nvidia", label="NVIDIA NIM", kind="openai",
        base_url="https://integrate.api.nvidia.com/v1", key_env="NVIDIA_API_KEY",
        default_model="meta/llama-3.3-70b-instruct",
        docs="https://build.nvidia.com/",
        note="build.nvidia.com hosted NIM, or a self-hosted NIM container "
             "(point the base URL at http://localhost:8000/v1).",
    ),
    "claude-code": Provider(
        id="claude-code", label="Claude Code CLI", kind="cli",
        base_url="", key_env="", default_model="", requires_key=False, local=True,
        supports_tools=False, supports_stream=False,
        docs="https://claude.com/claude-code",
        note="Runs the `claude` binary you already sign in to, and reports the "
             "exact USD cost it returns. Has its own tools, so studyweb's web "
             "tools are not attached to it.",
    ),
    "custom": Provider(
        id="custom", label="Custom OpenAI-compatible", kind="openai",
        base_url="http://localhost:11434/v1", key_env="STUDYWEB_CUSTOM_API_KEY",
        default_model="", requires_key=False,
        note="Ollama, vLLM, llama.cpp, OpenRouter, Groq, Together — anything "
             "that speaks /chat/completions.",
    ),
}

DEFAULT_PROVIDER = "lmstudio"

ANTHROPIC_VERSION = "2023-06-01"

# Fallback model lists for providers whose catalogue we can't enumerate without
# a key. The GUI's "Refresh" replaces these with the live list.
FALLBACK_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
                  "claude-opus-4-8", "claude-fable-5"],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
    "nvidia": ["meta/llama-3.3-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct",
               "deepseek-ai/deepseek-r1", "qwen/qwen2.5-coder-32b-instruct"],
}


class ProviderError(Exception):
    """A provider call failed. ``kind`` classifies it for the UI."""

    def __init__(self, message: str, *, kind: str = "error", status: int = 0,
                 provider: str = ""):
        super().__init__(message)
        self.kind = kind        # no_key|unauthorized|rate_limit|unreachable|http|bad_response|error
        self.status = status
        self.provider = provider

    def to_dict(self) -> dict:
        return {"error": str(self), "kind": self.kind, "status": self.status,
                "provider": self.provider}


def resolve(provider_id: str | None) -> Provider:
    pid = (provider_id or settings.llm_provider or DEFAULT_PROVIDER).strip()
    if pid not in PROVIDERS:
        raise ProviderError(f"unknown provider {pid!r}; known: {', '.join(PROVIDERS)}",
                            kind="unknown_provider", provider=pid)
    return PROVIDERS[pid]


def base_url(p: Provider) -> str:
    """Configured endpoint for a provider (env override, else the default)."""
    override = settings.provider_base_urls.get(p.id, "")
    return (override or p.base_url).rstrip("/")


def api_key(p: Provider) -> str:
    """The provider's key: an explicit studyweb override, else its own env var."""
    return (settings.provider_keys.get(p.id) or os.environ.get(p.key_env, "")).strip()


def model_for(p: Provider, model: str | None = None) -> str:
    return (model or settings.provider_models.get(p.id) or p.default_model or "").strip()


def describe(p: Provider) -> dict:
    """Public description of a provider — configuration, never the key itself."""
    d = asdict(p)
    d["base_url"] = base_url(p)
    d["has_key"] = bool(api_key(p))
    d["key_source"] = ("settings" if settings.provider_keys.get(p.id)
                       else "env" if os.environ.get(p.key_env) else "")
    d["model"] = model_for(p)
    d["priced"] = price_for(p.id, d["model"]) is not None
    return d


def list_providers() -> list[dict]:
    return [describe(p) for p in PROVIDERS.values()]


# --------------------------------------------------------------------------- #
#  HTTP plumbing                                                               #
# --------------------------------------------------------------------------- #

_session: requests.Session | None = None


def _http() -> requests.Session:
    """A plain session for API calls.

    Deliberately *not* :func:`studyweb.net.session` — that one carries the
    crawler's browser User-Agent, robots/rate-limit politeness and the SSRF
    guard, none of which apply to talking to a model endpoint you configured
    (which is very often localhost).
    """
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "studyweb/llm-client",
                          "Content-Type": "application/json"})
        _session = s
    return _session


def _auth_headers(p: Provider) -> dict:
    key = api_key(p)
    if p.requires_key and not key:
        raise ProviderError(
            f"{p.label} needs an API key — set {p.key_env} and restart, "
            f"or configure the key in your client.",
            kind="no_key", provider=p.id)
    if p.kind == "anthropic":
        h = {"anthropic-version": ANTHROPIC_VERSION}
        if key:
            h["x-api-key"] = key
        return h
    return {"Authorization": f"Bearer {key}"} if key else {}


def _request(p: Provider, method: str, path: str, *, json_body: dict | None = None,
             timeout: float | None = None, endpoint: str | None = None) -> dict:
    url = f"{(endpoint or base_url(p)).rstrip('/')}{path}"
    timeout = timeout or settings.llm_timeout
    try:
        r = _http().request(method, url, headers=_auth_headers(p),
                            json=json_body, timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise ProviderError(f"{p.label} timed out after {timeout:.0f}s",
                            kind="unreachable", provider=p.id) from exc
    except requests.exceptions.RequestException as exc:
        raise ProviderError(f"cannot reach {p.label} at {url}: {exc}",
                            kind="unreachable", provider=p.id) from exc
    if r.status_code >= 400:
        raise ProviderError(_error_message(r), kind=_error_kind(r.status_code),
                            status=r.status_code, provider=p.id)
    try:
        return r.json()
    except ValueError as exc:
        raise ProviderError(f"{p.label} returned a non-JSON response",
                            kind="bad_response", provider=p.id) from exc


def _error_kind(status: int) -> str:
    if status in (401, 403):
        return "unauthorized"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server_error"
    return "http"


def _error_message(r: requests.Response) -> str:
    """Pull the provider's own message out of an error body (they all differ)."""
    detail = ""
    try:
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or err.get("type") or ""
        elif isinstance(err, str):
            detail = err
        elif isinstance(body, dict):
            detail = body.get("message") or body.get("detail") or ""
    except ValueError:
        detail = (r.text or "")[:200]
    return f"HTTP {r.status_code}: {detail or r.reason or 'request failed'}"


# --------------------------------------------------------------------------- #
#  Message translation: OpenAI shape  <->  Anthropic Messages API              #
# --------------------------------------------------------------------------- #

# Claude models that removed `temperature`/`top_p`/`top_k` — sending one is a
# 400, so the request omits it for anything matching these prefixes.
_NO_SAMPLING = ("claude-opus-5", "claude-opus-4-7", "claude-opus-4-8",
                "claude-sonnet-5", "claude-fable-5", "claude-mythos-5")


def _accepts_sampling(model: str) -> bool:
    return not any(model.startswith(m) for m in _NO_SAMPLING)


def to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split an OpenAI-shaped conversation into (system, anthropic messages).

    Anthropic keeps the system prompt out of the message list, expresses tool
    calls as ``tool_use`` blocks on the assistant turn, and expects tool results
    as ``tool_result`` blocks on a *user* turn — consecutive results merge into
    one message.
    """
    system_parts: list[str] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        if role == "tool":
            block = {"type": "tool_result",
                     "tool_use_id": m.get("tool_call_id") or m.get("name") or "tool",
                     "content": content if isinstance(content, str) else json.dumps(content)}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or fn.get("name", "call"),
                               "name": fn.get("name", ""), "input": args})
            if not blocks:
                continue  # an empty assistant turn is rejected by the API
            out.append({"role": "assistant", "content": blocks})
            continue
        # user (or anything unrecognised) — plain text
        out.append({"role": "user", "content": content})
    return "\n\n".join(system_parts), out


def tools_to_anthropic(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    out = []
    for t in tools:
        fn = t.get("function") or t
        out.append({"name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def from_anthropic(body: dict) -> dict:
    """Anthropic response -> an OpenAI-shaped assistant message."""
    texts, tool_calls = [], []
    for block in body.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""), "type": "function",
                "function": {"name": block.get("name", ""),
                             "arguments": json.dumps(block.get("input") or {},
                                                     ensure_ascii=False)},
            })
    msg: dict = {"role": "assistant", "content": "".join(texts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if body.get("stop_reason"):
        msg["finish_reason"] = body["stop_reason"]
    return msg


# --------------------------------------------------------------------------- #
#  Usage extraction                                                            #
# --------------------------------------------------------------------------- #

def _usage_openai(body: dict, p: Provider, model: str, ms: int, label: str) -> Usage:
    u = body.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    estimated = False
    if not prompt and not completion:
        # Some OpenAI-compatible servers omit usage entirely.
        text = json.dumps(body.get("choices") or [], ensure_ascii=False)
        completion = estimate_tokens(text)
        estimated = True
    return Usage(provider=p.id, model=model, prompt_tokens=prompt,
                 completion_tokens=completion,
                 cached_tokens=int(details.get("cached_tokens") or 0),
                 latency_ms=ms, estimated=estimated, label=label)


def _usage_anthropic(body: dict, p: Provider, model: str, ms: int, label: str) -> Usage:
    u = body.get("usage") or {}
    cache_read = int(u.get("cache_read_input_tokens") or 0)
    cache_write = int(u.get("cache_creation_input_tokens") or 0)
    return Usage(provider=p.id, model=model,
                 # input_tokens excludes cached reads; report the true prompt size
                 prompt_tokens=int(u.get("input_tokens") or 0) + cache_read + cache_write,
                 completion_tokens=int(u.get("output_tokens") or 0),
                 cached_tokens=cache_read, latency_ms=ms, label=label)


# --------------------------------------------------------------------------- #
#  Chat                                                                        #
# --------------------------------------------------------------------------- #

def chat(messages: list[dict], *, provider: str | None = None,
         model: str | None = None, tools: list[dict] | None = None,
         temperature: float | None = None, max_tokens: int | None = None,
         timeout: float | None = None, label: str = "chat",
         json_mode: bool = False, record: bool = True,
         endpoint: str | None = None) -> dict:
    """Send one chat turn to any provider.

    Returns ``{"message", "usage", "provider", "model", "raw"}`` where
    ``message`` is always the OpenAI shape: ``content`` plus optional
    ``tool_calls``. Raises :class:`ProviderError` on any failure, with a
    ``kind`` the caller can show or branch on.
    """
    p = resolve(provider)
    temperature = settings.llm_temperature if temperature is None else temperature
    max_tokens = max_tokens or settings.llm_max_tokens
    t0 = time.time()

    if p.kind == "cli":
        result = _chat_cli(p, messages, model=model, timeout=timeout, label=label)
    elif p.kind == "anthropic":
        result = _chat_anthropic(p, messages, model=model, tools=tools,
                                 temperature=temperature, max_tokens=max_tokens,
                                 timeout=timeout, label=label, endpoint=endpoint)
    else:
        result = _chat_openai(p, messages, model=model, tools=tools,
                              temperature=temperature, max_tokens=max_tokens,
                              timeout=timeout, label=label, json_mode=json_mode,
                              endpoint=endpoint)

    result["usage"].latency_ms = int((time.time() - t0) * 1000)
    if record:
        ledger.record(result["usage"])
    else:
        result["usage"].priced()
    return result


def _resolve_openai_model(p: Provider, model: str | None, timeout: float | None,
                          endpoint: str | None = None) -> str:
    """A concrete model id — asking the server when none is configured, which is
    the normal case for LM Studio ("use whatever is loaded")."""
    m = model_for(p, model)
    if m:
        return m
    ids = list_models(p.id, timeout=timeout, endpoint=endpoint)
    if not ids:
        raise ProviderError(f"no model available from {p.label}", kind="no_model",
                            provider=p.id)
    return ids[0]


def _chat_openai(p: Provider, messages, *, model, tools, temperature, max_tokens,
                 timeout, label, json_mode=False, endpoint=None) -> dict:
    mdl = _resolve_openai_model(p, model, timeout, endpoint)
    payload: dict = {"model": mdl, "messages": messages, "temperature": temperature,
                     "stream": False}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        body = _request(p, "POST", "/chat/completions", json_body=payload,
                        timeout=timeout, endpoint=endpoint)
    except ProviderError as exc:
        # Not every OpenAI-compatible server accepts response_format or
        # max_tokens; retry once without the optional knobs before giving up.
        if exc.kind != "http" or not (json_mode or max_tokens):
            raise
        payload.pop("response_format", None)
        payload.pop("max_tokens", None)
        body = _request(p, "POST", "/chat/completions", json_body=payload,
                        timeout=timeout, endpoint=endpoint)
    choices = body.get("choices") or []
    if not choices:
        raise ProviderError(f"{p.label} returned no choices", kind="bad_response",
                            provider=p.id)
    msg = dict(choices[0].get("message") or {})
    msg.setdefault("role", "assistant")
    msg["content"] = msg.get("content") or ""
    if choices[0].get("finish_reason"):
        msg["finish_reason"] = choices[0]["finish_reason"]
    return {"message": msg, "usage": _usage_openai(body, p, mdl, 0, label),
            "provider": p.id, "model": mdl, "raw": body}


def _chat_anthropic(p: Provider, messages, *, model, tools, temperature, max_tokens,
                    timeout, label, endpoint=None) -> dict:
    mdl = model_for(p, model) or p.default_model
    system, msgs = to_anthropic(messages)
    if not msgs:
        raise ProviderError("no user message to send", kind="error", provider=p.id)
    payload: dict = {"model": mdl, "messages": msgs,
                     "max_tokens": max_tokens or 4096}
    if system:
        payload["system"] = system
    atools = tools_to_anthropic(tools)
    if atools:
        payload["tools"] = atools
        payload["tool_choice"] = {"type": "auto"}
    # Optional knobs, sent only when they apply. Current Claude models reject
    # `temperature` outright (400), and `effort` only exists on some — so the
    # base payload stays minimal and anything extra is retried away on a 400.
    optional: dict = {}
    if temperature is not None and _accepts_sampling(mdl):
        optional["temperature"] = temperature
    if settings.llm_effort:
        optional["output_config"] = {"effort": settings.llm_effort}
    payload.update(optional)
    try:
        body = _request(p, "POST", "/messages", json_body=payload, timeout=timeout,
                        endpoint=endpoint)
    except ProviderError as exc:
        if exc.kind != "http" or not optional:
            raise
        for key in optional:
            payload.pop(key, None)
        body = _request(p, "POST", "/messages", json_body=payload, timeout=timeout,
                        endpoint=endpoint)
    return {"message": from_anthropic(body),
            "usage": _usage_anthropic(body, p, mdl, 0, label),
            "provider": p.id, "model": mdl, "raw": body}


def _flatten_for_cli(messages: list[dict]) -> tuple[str, str]:
    """Collapse a conversation into (system, prompt) for a text-in/text-out CLI."""
    system = "\n\n".join(m.get("content") or "" for m in messages
                         if m.get("role") == "system")
    lines = []
    for m in messages:
        role = m.get("role")
        if role in ("system", "tool"):
            continue
        who = "Human" if role == "user" else "Assistant"
        lines.append(f"{who}: {m.get('content') or ''}")
    return system, "\n\n".join(lines)


def _chat_cli(p: Provider, messages, *, model, timeout, label) -> dict:
    """Run the local `claude` binary in print mode and read back its own usage."""
    binary = settings.claude_code_binary or "claude"
    exe = shutil.which(binary)
    if not exe:
        raise ProviderError(
            f"`{binary}` is not on PATH — install Claude Code or set STUDYWEB_CLAUDE_BIN.",
            kind="not_installed", provider=p.id)
    system, prompt = _flatten_for_cli(messages)
    cmd = [exe, "-p", "--output-format", "json"]
    mdl = model_for(p, model)
    if mdl:
        cmd += ["--model", mdl]
    if system:
        cmd += ["--append-system-prompt", system]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=timeout or settings.llm_timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProviderError("claude CLI timed out", kind="unreachable",
                            provider=p.id) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        kind = "unauthorized" if "login" in detail.lower() or "auth" in detail.lower() else "error"
        raise ProviderError(f"claude CLI failed: {detail}", kind=kind, provider=p.id)
    try:
        body = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError("claude CLI returned unparseable output",
                            kind="bad_response", provider=p.id) from exc
    if body.get("is_error"):
        raise ProviderError(f"claude CLI: {body.get('result') or 'error'}",
                            kind="error", provider=p.id)
    u = body.get("usage") or {}
    cache_read = int(u.get("cache_read_input_tokens") or 0)
    usage = Usage(
        provider=p.id, model=body.get("model") or mdl or "claude-code",
        prompt_tokens=int(u.get("input_tokens") or 0) + cache_read
        + int(u.get("cache_creation_input_tokens") or 0),
        completion_tokens=int(u.get("output_tokens") or 0),
        cached_tokens=cache_read,
        # The CLI reports what it actually charged — trust it over our table.
        cost_usd=body.get("total_cost_usd"), label=label)
    return {"message": {"role": "assistant", "content": body.get("result") or ""},
            "usage": usage, "provider": p.id, "model": usage.model, "raw": body}


# --------------------------------------------------------------------------- #
#  Models + health                                                             #
# --------------------------------------------------------------------------- #

def list_models(provider: str | None = None, *, timeout: float | None = None,
                endpoint: str | None = None) -> list[str]:
    """Model ids a provider offers. Falls back to a static list where the
    catalogue needs a key we don't have."""
    p = resolve(provider)
    if p.kind == "cli":
        return []
    try:
        body = _request(p, "GET", "/models", timeout=timeout or 15, endpoint=endpoint)
    except ProviderError:
        if p.id in FALLBACK_MODELS and not api_key(p):
            return list(FALLBACK_MODELS[p.id])
        raise
    data = body.get("data") if isinstance(body, dict) else None
    ids = [m.get("id") for m in (data or []) if isinstance(m, dict) and m.get("id")]
    return sorted(ids) if ids else list(FALLBACK_MODELS.get(p.id, []))


def check(provider: str | None = None, *, timeout: float = 8.0) -> dict:
    """Probe one provider and classify the result for the UI.

    ``status`` is one of:

    ``ok``            reachable and ready
    ``no_key``        needs an API key that isn't configured
    ``no_model``      reachable, but nothing is loaded/selected
    ``unauthorized``  the key was rejected
    ``rate_limit``    reachable, currently throttled
    ``unreachable``   nothing answered (server down, wrong URL, offline)
    ``not_installed`` a CLI provider whose binary is missing
    ``error``         anything else — ``detail`` says what
    """
    p = resolve(provider)
    started = time.time()
    out = {"provider": p.id, "label": p.label, "status": "error", "detail": "",
           "models": [], "model": model_for(p), "base_url": base_url(p),
           "has_key": bool(api_key(p)), "key_env": p.key_env, "local": p.local,
           "latency_ms": 0, "checked_at": time.time()}

    if p.kind == "cli":
        exe = shutil.which(settings.claude_code_binary or "claude")
        if not exe:
            out.update(status="not_installed",
                       detail="`claude` is not on PATH. Install Claude Code, or set STUDYWEB_CLAUDE_BIN.")
            return out
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  timeout=timeout)
            ver = (proc.stdout or "").strip()
            out.update(status="ok" if proc.returncode == 0 else "error",
                       detail=ver or (proc.stderr or "").strip()[:200], models=[])
        except (OSError, subprocess.SubprocessError) as exc:
            out.update(status="error", detail=str(exc)[:200])
        out["latency_ms"] = int((time.time() - started) * 1000)
        return out

    if p.requires_key and not api_key(p):
        # Name the variable, not a particular client's settings screen — this
        # answer is read by the CLI, the HTTP API and two different plugins.
        out.update(status="no_key", detail=f"No API key. Set {p.key_env}.")
        return out

    try:
        ids = list_models(p.id, timeout=timeout)
        out["models"] = ids
        chosen = model_for(p)
        if not ids and not chosen:
            out.update(status="no_model",
                       detail="Connected, but no model is loaded. Load one in LM Studio."
                       if p.local else "Connected, but the server listed no models.")
        elif chosen and ids and chosen not in ids:
            out.update(status="ok",
                       detail=f"Connected ({len(ids)} models). Note: '{chosen}' is not in the list.")
        else:
            out.update(status="ok", model=chosen or (ids[0] if ids else ""),
                       detail=f"Connected · {len(ids)} model(s) available"
                       if ids else "Connected")
    except ProviderError as exc:
        status = {"no_key": "no_key", "unauthorized": "unauthorized",
                  "rate_limit": "rate_limit", "unreachable": "unreachable",
                  "no_model": "no_model"}.get(exc.kind, "error")
        out.update(status=status, detail=str(exc))
    out["latency_ms"] = int((time.time() - started) * 1000)
    return out


def check_all(*, timeout: float = 8.0, only_configured: bool = False) -> list[dict]:
    """Probe every provider, concurrently — one slow endpoint shouldn't hold up
    the rest. ``only_configured`` reports key-less providers without dialling
    out, so a first-run status refresh is instant."""
    from concurrent.futures import ThreadPoolExecutor

    def one(pid: str) -> dict:
        p = PROVIDERS[pid]
        if only_configured and p.requires_key and not api_key(p):
            return {"provider": pid, "label": p.label, "status": "no_key",
                    "detail": f"No API key. Set {p.key_env} to enable.", "models": [],
                    "model": model_for(p), "base_url": base_url(p),
                    "has_key": False, "key_env": p.key_env, "local": p.local,
                    "latency_ms": 0, "checked_at": time.time()}
        return check(pid, timeout=timeout)

    ids = list(PROVIDERS)
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        return list(pool.map(one, ids))
