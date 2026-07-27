"""Token-usage and cost accounting for every LLM call studyweb makes.

Two pieces:

``Usage``       what one model call consumed — tokens in/out, wall time, and a
                cost in USD when the model's price is known.
``UsageLedger`` where those add up: an in-memory session total plus a JSONL log
                and a small rollup file on disk, so "how much have I spent"
                survives restarts.

Prices are *data*, not code: ``DEFAULT_PRICING`` seeds a table you can override
per model in ``~/.config/studyweb/pricing.json`` (or ``$STUDYWEB_PRICING``).
Provider prices change — treat the built-in numbers as a starting point and
correct them there; a model with no entry reports tokens with ``cost_usd=None``
rather than inventing a figure.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
#  Pricing                                                                     #
# --------------------------------------------------------------------------- #

# USD per 1,000,000 tokens: {provider: {model-or-prefix*: {"in": x, "out": y}}}.
# "cached" is the per-MTok price of a cache *read* where the provider offers one.
# Keys ending in "*" are prefix patterns; the longest match wins.
DEFAULT_PRICING: dict[str, dict[str, dict]] = {
    "lmstudio": {"*": {"in": 0.0, "out": 0.0}},      # local — electricity only
    "custom": {"*": {"in": 0.0, "out": 0.0}},
    "openai": {
        "gpt-4o": {"in": 2.50, "out": 10.00, "cached": 1.25},
        "gpt-4o-mini": {"in": 0.15, "out": 0.60, "cached": 0.075},
        "gpt-4.1": {"in": 2.00, "out": 8.00, "cached": 0.50},
        "gpt-4.1-mini": {"in": 0.40, "out": 1.60, "cached": 0.10},
        "gpt-4.1-nano": {"in": 0.10, "out": 0.40, "cached": 0.025},
        "o3-mini": {"in": 1.10, "out": 4.40, "cached": 0.55},
    },
    # Anthropic list prices; a cache read is 0.1x the input rate.
    "anthropic": {
        "claude-fable-5": {"in": 10.00, "out": 50.00, "cached": 1.00},
        "claude-mythos-5": {"in": 10.00, "out": 50.00, "cached": 1.00},
        "claude-opus-5": {"in": 5.00, "out": 25.00, "cached": 0.50},
        "claude-opus-4*": {"in": 5.00, "out": 25.00, "cached": 0.50},
        "claude-sonnet-5": {"in": 3.00, "out": 15.00, "cached": 0.30},
        "claude-sonnet-4*": {"in": 3.00, "out": 15.00, "cached": 0.30},
        "claude-haiku-4*": {"in": 1.00, "out": 5.00, "cached": 0.10},
    },
    # NVIDIA NIM: the hosted build.nvidia.com endpoint is credit-metered rather
    # than per-token, and self-hosted NIM containers cost you nothing per call.
    # Left unpriced on purpose — add your own rates if you are billed per token.
    "nvidia": {},
    # The Claude Code CLI reports its own USD cost per call, so no table needed.
    "claude-code": {},
}

_PRICING_LOCK = threading.Lock()
_pricing_cache: dict | None = None


def pricing_path() -> str:
    """Where user price overrides live."""
    p = os.environ.get("STUDYWEB_PRICING")
    if p:
        return os.path.expanduser(p)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "studyweb", "pricing.json")


def load_pricing(*, refresh: bool = False) -> dict:
    """Built-in prices with the user's overrides merged over them (per model)."""
    global _pricing_cache
    with _PRICING_LOCK:
        if _pricing_cache is not None and not refresh:
            return _pricing_cache
        table = {k: dict(v) for k, v in DEFAULT_PRICING.items()}
        try:
            with open(pricing_path(), "r", encoding="utf-8") as fh:
                user = json.load(fh)
            for provider, models in (user or {}).items():
                if provider.startswith("_") or not isinstance(models, dict):
                    continue  # "_note" and friends
                table.setdefault(provider, {}).update(models)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass  # no overrides, or a broken file — built-ins still work
        _pricing_cache = table
        return table


def save_pricing(table: dict) -> str:
    """Write ``table`` as the user override file and return its path."""
    path = pricing_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"_note": ("USD per 1,000,000 tokens. Keys ending in '*' are prefix "
                         "patterns; the longest match wins. Edit freely — studyweb "
                         "merges this over its built-in defaults."), **table}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    load_pricing(refresh=True)
    return path


def price_for(provider: str, model: str) -> dict | None:
    """Per-MTok prices for a model, or None when it is unpriced."""
    models = load_pricing().get(provider or "", {})
    if not models:
        return None
    model = model or ""
    if model in models:
        return models[model]
    best, best_len = None, -1
    for key, val in models.items():
        if not key.endswith("*"):
            continue
        stem = key[:-1]
        if model.startswith(stem) and len(stem) > best_len:
            best, best_len = val, len(stem)
    return best


def estimate_cost(provider: str, model: str, prompt_tokens: int,
                  completion_tokens: int, cached_tokens: int = 0) -> float | None:
    """USD for one call, or None when the model has no known price."""
    p = price_for(provider, model)
    if p is None:
        return None
    billed_prompt = max(0, prompt_tokens - max(0, cached_tokens))
    cost = (billed_prompt / 1e6) * float(p.get("in", 0.0))
    cost += (completion_tokens / 1e6) * float(p.get("out", 0.0))
    if cached_tokens:
        cache_rate = p.get("cached", p.get("in", 0.0))
        cost += (cached_tokens / 1e6) * float(cache_rate)
    return round(cost, 6)


# --------------------------------------------------------------------------- #
#  One call's usage                                                            #
# --------------------------------------------------------------------------- #

@dataclass
class Usage:
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0          # prompt tokens served from the provider cache
    requests: int = 1
    latency_ms: int = 0
    cost_usd: float | None = None   # None = model not priced
    estimated: bool = False         # True when token counts are our estimate
    label: str = ""                 # what the call was for, e.g. "chat", "extract"
    ts: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def priced(self) -> "Usage":
        """Fill in ``cost_usd`` from the pricing table if it isn't set."""
        if self.cost_usd is None:
            self.cost_usd = estimate_cost(self.provider, self.model,
                                          self.prompt_tokens, self.completion_tokens,
                                          self.cached_tokens)
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        return d

    def __add__(self, other: "Usage") -> "Usage":
        """Combine two calls (e.g. every step of one agent turn)."""
        if not isinstance(other, Usage):
            return NotImplemented
        cost: float | None
        if self.cost_usd is None and other.cost_usd is None:
            cost = None
        else:
            cost = round((self.cost_usd or 0.0) + (other.cost_usd or 0.0), 6)
        return Usage(
            provider=self.provider or other.provider,
            model=self.model or other.model,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            requests=self.requests + other.requests,
            latency_ms=self.latency_ms + other.latency_ms,
            cost_usd=cost,
            estimated=self.estimated or other.estimated,
            label=self.label or other.label,
            ts=max(self.ts, other.ts),
        )


def estimate_tokens(text: str) -> int:
    """Rough, script-aware token count for providers that report none.

    CJK text packs far more tokens per character than Latin, so count the two
    separately. Only ever used as a fallback — real counts come from the API.
    """
    if not text:
        return 0
    cjk = 0
    for ch in text:
        o = ord(ch)
        if (0x1100 <= o <= 0x11FF or 0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF
                or 0x4E00 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF):
            cjk += 1
    return int(cjk * 1.1 + (len(text) - cjk) / 4) + 1


# --------------------------------------------------------------------------- #
#  The ledger                                                                  #
# --------------------------------------------------------------------------- #

def state_dir() -> str:
    p = os.environ.get("STUDYWEB_STATE_DIR")
    if p:
        return os.path.expanduser(p)
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "studyweb")


def _blank() -> dict:
    return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
            "unpriced_requests": 0}


def _add(bucket: dict, u: Usage) -> dict:
    bucket["requests"] += u.requests
    bucket["prompt_tokens"] += u.prompt_tokens
    bucket["completion_tokens"] += u.completion_tokens
    bucket["cached_tokens"] += u.cached_tokens
    bucket["total_tokens"] += u.total_tokens
    if u.cost_usd is None:
        bucket["unpriced_requests"] += u.requests
    else:
        bucket["cost_usd"] = round(bucket["cost_usd"] + u.cost_usd, 6)
    return bucket


class UsageLedger:
    """Accumulates :class:`Usage` in memory and on disk.

    On-disk layout (under ``state_dir()``)::

        usage-2026-07.jsonl   one line per call, for auditing
        usage-rollup.json     cumulative totals + the last 90 days

    Every write is atomic and guarded by a lock, so several threads (the HTTP
    server is threaded) can record concurrently. Disk failures are swallowed:
    accounting must never break the call it is measuring.
    """

    MAX_DAYS = 90
    MAX_RECENT = 50

    def __init__(self, directory: str | None = None, *, persist: bool = True):
        self._dir = directory or state_dir()
        self._persist = persist
        self._lock = threading.Lock()
        self._session = _blank()
        self._session_started = time.time()
        self._recent: list[dict] = []

    # -- paths ------------------------------------------------------------
    @property
    def rollup_path(self) -> str:
        return os.path.join(self._dir, "usage-rollup.json")

    def _log_path(self, when: float) -> str:
        stamp = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m")
        return os.path.join(self._dir, f"usage-{stamp}.jsonl")

    # -- recording --------------------------------------------------------
    def record(self, u: Usage) -> Usage:
        """Price ``u``, add it to every bucket, and return it."""
        u = u.priced()
        day = datetime.fromtimestamp(u.ts, timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            _add(self._session, u)
            self._recent.append(u.to_dict())
            del self._recent[:-self.MAX_RECENT]
            if self._persist:
                try:
                    self._append_log(u)
                    self._update_rollup(u, day)
                except Exception:  # noqa: BLE001
                    # Read-only home, full disk, an unusable path… measuring a
                    # call must never be what breaks it. In-memory totals stand.
                    pass
        return u

    def _append_log(self, u: Usage) -> None:
        os.makedirs(self._dir, exist_ok=True)
        with open(self._log_path(u.ts), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(u.to_dict(), ensure_ascii=False) + "\n")

    def _read_rollup(self) -> dict:
        try:
            with open(self.rollup_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "total" in data:
                return data
        except Exception:  # noqa: BLE001 — missing, truncated or unreadable: start fresh
            pass
        return {"total": _blank(), "days": {}, "providers": {}, "models": {},
                "since": datetime.now(timezone.utc).strftime("%Y-%m-%d")}

    def _update_rollup(self, u: Usage, day: str) -> None:
        data = self._read_rollup()
        _add(data["total"], u)
        _add(data["days"].setdefault(day, _blank()), u)
        _add(data["providers"].setdefault(u.provider or "?", _blank()), u)
        _add(data["models"].setdefault(f"{u.provider}/{u.model}", _blank()), u)
        for old in sorted(data["days"])[:-self.MAX_DAYS]:
            data["days"].pop(old, None)
        os.makedirs(self._dir, exist_ok=True)
        tmp = self.rollup_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.rollup_path)

    # -- reading ----------------------------------------------------------
    def summary(self, *, days: int = 7) -> dict:
        """Session / today / recent-days / all-time totals, plus breakdowns."""
        with self._lock:
            session = dict(self._session)
            recent = list(self._recent)
            started = self._session_started
        data = self._read_rollup() if self._persist else {
            "total": _blank(), "days": {}, "providers": {}, "models": {}, "since": ""}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_rows = sorted(data.get("days", {}).items(), reverse=True)[:days]
        return {
            "session": session,
            "session_started": started,
            "today": data.get("days", {}).get(today, _blank()),
            "total": data.get("total", _blank()),
            "since": data.get("since", ""),
            "days": [{"day": d, **v} for d, v in reversed(day_rows)],
            "providers": data.get("providers", {}),
            "models": data.get("models", {}),
            "recent": recent[-20:],
            "storage": self._dir if self._persist else None,
        }

    def reset(self, scope: str = "session") -> dict:
        """Clear the session counters, or wipe the persisted history too."""
        with self._lock:
            self._session = _blank()
            self._session_started = time.time()
            self._recent = []
            if scope == "all" and self._persist:
                try:
                    os.remove(self.rollup_path)
                except OSError:
                    pass
        return {"reset": scope}


# Process-wide ledger. Disable persistence with STUDYWEB_USAGE_PERSIST=0.
def _persist_default() -> bool:
    v = os.environ.get("STUDYWEB_USAGE_PERSIST", "1")
    return v.strip().lower() in ("1", "true", "yes", "on")


ledger = UsageLedger(persist=_persist_default())
