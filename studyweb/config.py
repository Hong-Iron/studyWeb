"""Runtime configuration.

Everything is overridable via environment variables so the same code runs
unchanged as a library, a CLI, or a server. Nothing here requires a paid API
key — keys only *upgrade* the search backend when present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# A current-browser User-Agent. Many sites reject the obvious "python-requests"
# / custom-bot UA outright (Danawa, for one, returns 403), so to actually work
# on real sites we present as a browser — while still honouring robots.txt and
# per-host rate limits below. Override with STUDYWEB_UA if you prefer to
# identify your bot explicitly.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Sites `studyweb prices` checks, in buyer order. The first three answer a
# plain fetch and are what actually produces numbers; the marketplaces after
# them serve a JS shell and need STUDYWEB_RENDER (or, for Naver, API keys) —
# they stay in the list so an incomplete check reports itself as one.
DEFAULT_PRICE_SITES: tuple[str, ...] = (
    "danawa.com", "compuzone.co.kr", "enuri.com",
    "shopping.naver.com", "11st.co.kr", "coupang.com",
)


@dataclass
class Settings:
    # ---- HTTP / politeness -------------------------------------------------
    user_agent: str = field(default_factory=lambda: os.environ.get("STUDYWEB_UA", DEFAULT_UA))
    respect_robots: bool = field(default_factory=lambda: _env_bool("STUDYWEB_RESPECT_ROBOTS", True))
    per_host_delay: float = field(default_factory=lambda: _env_float("STUDYWEB_PER_HOST_DELAY", 1.0))
    timeout: float = field(default_factory=lambda: _env_float("STUDYWEB_TIMEOUT", 15.0))
    max_retries: int = field(default_factory=lambda: _env_int("STUDYWEB_MAX_RETRIES", 2))
    max_bytes: int = field(default_factory=lambda: _env_int("STUDYWEB_MAX_BYTES", 4_000_000))
    max_workers: int = field(default_factory=lambda: _env_int("STUDYWEB_MAX_WORKERS", 8))
    # SSRF guard: by default refuse to fetch private/loopback/link-local hosts.
    # Set true for intranet use where you deliberately fetch internal addresses.
    allow_private_hosts: bool = field(default_factory=lambda: _env_bool("STUDYWEB_ALLOW_PRIVATE", False))

    # ---- Search backends ---------------------------------------------------
    # "auto" tries free providers (bing, wikipedia) then any key-based ones.
    default_provider: str = field(default_factory=lambda: os.environ.get("STUDYWEB_PROVIDER", "auto"))
    # Bing market, e.g. "en-US" or "ko-KR". "" lets Bing auto-detect (less stable).
    search_market: str = field(default_factory=lambda: os.environ.get("STUDYWEB_MARKET", "en-US"))
    brave_api_key: str = field(default_factory=lambda: os.environ.get("BRAVE_API_KEY", ""))
    tavily_api_key: str = field(default_factory=lambda: os.environ.get("TAVILY_API_KEY", ""))
    serpapi_api_key: str = field(default_factory=lambda: os.environ.get("SERPAPI_API_KEY", ""))
    google_cse_key: str = field(default_factory=lambda: os.environ.get("GOOGLE_CSE_KEY", ""))
    google_cse_cx: str = field(default_factory=lambda: os.environ.get("GOOGLE_CSE_CX", ""))
    searxng_url: str = field(default_factory=lambda: os.environ.get("SEARXNG_URL", ""))
    # Naver Open API (developers.naver.com) — free 25k calls/day. Enables both
    # the "naver" web provider and the "naver_shop" price vertical.
    naver_client_id: str = field(default_factory=lambda: os.environ.get("NAVER_CLIENT_ID", ""))
    naver_client_secret: str = field(
        default_factory=lambda: os.environ.get("NAVER_CLIENT_SECRET", ""))
    # Providers to skip under provider="auto", comma-separated. Worth setting
    # when an engine is unreachable from your network: the chain otherwise pays
    # a full connect timeout per attempt before moving on, twice over when the
    # domain-scoped recall retry fires.
    search_disable: tuple = field(default_factory=lambda: tuple(
        p.strip().lower() for p in os.environ.get("STUDYWEB_SEARCH_DISABLE", "").split(",")
        if p.strip()))

    # Sites checked by `studyweb prices` / POST /prices, in buyer order.
    price_sites: tuple = field(default_factory=lambda: tuple(
        s.strip() for s in os.environ.get(
            "STUDYWEB_PRICE_SITES", ",".join(DEFAULT_PRICE_SITES)).split(",")
        if s.strip()))

    # ---- Research pipeline defaults ---------------------------------------
    default_max_results: int = field(default_factory=lambda: _env_int("STUDYWEB_MAX_RESULTS", 6))
    passage_chars: int = field(default_factory=lambda: _env_int("STUDYWEB_PASSAGE_CHARS", 900))

    # ---- URL recovery (for hallucinated / 404 URLs) -----------------------
    # When open_url / extract_data get a URL that fails, search the same site for
    # the intended page instead of returning a bare 404.
    recover_urls: bool = field(default_factory=lambda: _env_bool("STUDYWEB_RECOVER_URLS", True))
    recover_max_candidates: int = field(default_factory=lambda: _env_int("STUDYWEB_RECOVER_MAX", 5))

    # ---- Structured data extraction (general, cross-site) ------------------
    # LLM used for schema-guided extraction when a page has no structured
    # markup (JSON-LD/microdata). Defaults to a local LM Studio server.
    llm_base_url: str = field(default_factory=lambda: os.environ.get("STUDYWEB_LLM_BASE", "http://localhost:1234/v1"))
    llm_model: str = field(default_factory=lambda: os.environ.get("STUDYWEB_LLM_MODEL", ""))
    llm_timeout: float = field(default_factory=lambda: _env_float("STUDYWEB_LLM_TIMEOUT", 120.0))

    # ---- Model providers (local + cloud) -----------------------------------
    # Which provider studyweb talks to by default. See studyweb.providers for
    # the registry: lmstudio | openai | anthropic | nvidia | claude-code | custom.
    llm_provider: str = field(default_factory=lambda: os.environ.get("STUDYWEB_PROVIDER_LLM", "lmstudio"))
    llm_temperature: float = field(default_factory=lambda: _env_float("STUDYWEB_LLM_TEMPERATURE", 0.0))
    # Upper bound on a reply. Required by the Anthropic API, optional elsewhere.
    # Claude counts thinking against this budget, so leave real headroom.
    llm_max_tokens: int = field(default_factory=lambda: _env_int("STUDYWEB_LLM_MAX_TOKENS", 8192))
    # Reasoning effort for providers that expose it (Claude: low|medium|high|
    # xhigh|max). Empty = don't send it, which is what older models need.
    llm_effort: str = field(default_factory=lambda: os.environ.get("STUDYWEB_LLM_EFFORT", ""))
    # Per-provider overrides, keyed by provider id. Keys come from each
    # provider's own env var (OPENAI_API_KEY, ANTHROPIC_API_KEY, NVIDIA_API_KEY…)
    # unless something sets them here — the HTTP API does, for clients that
    # would rather not keep keys of their own.
    provider_keys: dict = field(default_factory=dict)
    provider_base_urls: dict = field(default_factory=dict)
    provider_models: dict = field(default_factory=dict)
    # The Claude Code binary used by the "claude-code" provider.
    claude_code_binary: str = field(default_factory=lambda: os.environ.get("STUDYWEB_CLAUDE_BIN", "claude"))

    def __post_init__(self) -> None:
        # STUDYWEB_LLM_BASE / STUDYWEB_LLM_MODEL predate the provider registry
        # and mean "the local OpenAI-compatible server" — keep honouring them.
        if os.environ.get("STUDYWEB_LLM_BASE"):
            self.provider_base_urls.setdefault("lmstudio", self.llm_base_url)
        if self.llm_model:
            self.provider_models.setdefault("lmstudio", self.llm_model)
        for pid, env in (("openai", "OPENAI_BASE_URL"), ("anthropic", "ANTHROPIC_BASE_URL"),
                         ("nvidia", "NVIDIA_BASE_URL"), ("custom", "STUDYWEB_CUSTOM_BASE")):
            if os.environ.get(env):
                self.provider_base_urls.setdefault(pid, os.environ[env].rstrip("/"))
        for pid, env in (("openai", "OPENAI_MODEL"), ("anthropic", "ANTHROPIC_MODEL"),
                         ("nvidia", "NVIDIA_MODEL"), ("custom", "STUDYWEB_CUSTOM_MODEL"),
                         ("claude-code", "CLAUDE_CODE_MODEL")):
            if os.environ.get(env):
                self.provider_models.setdefault(pid, os.environ[env])

    # ---- Headless browser fallback (for JS-rendered pages) ----------------
    # When a static fetch yields thin/JS-shell content, optionally re-fetch via
    # a headless Chrome (system binary, subprocess). Off => never render.
    render_enabled: bool = field(default_factory=lambda: _env_bool("STUDYWEB_RENDER", True))
    chrome_path: str = field(default_factory=lambda: os.environ.get("STUDYWEB_CHROME", ""))
    render_timeout: float = field(default_factory=lambda: _env_float("STUDYWEB_RENDER_TIMEOUT", 25.0))
    # A static result shorter than this (chars of extracted text) is treated as
    # "thin" and triggers the headless fallback when rendering is enabled.
    render_thin_threshold: int = field(default_factory=lambda: _env_int("STUDYWEB_RENDER_THIN", 200))

    # ---- Cache -------------------------------------------------------------
    cache_dir: str = field(default_factory=lambda: os.environ.get(
        "STUDYWEB_CACHE_DIR", os.path.expanduser("~/.cache/studyweb")))
    cache_ttl: float = field(default_factory=lambda: _env_float("STUDYWEB_CACHE_TTL", 3600.0))
    cache_enabled: bool = field(default_factory=lambda: _env_bool("STUDYWEB_CACHE", True))
    # Soft cap on total on-disk cache size (MB); oldest entries are evicted past it.
    cache_max_mb: float = field(default_factory=lambda: _env_float("STUDYWEB_CACHE_MAX_MB", 500.0))

    # ---- Server ------------------------------------------------------------
    # Optional shared secret. If set, the API requires it (Tavily-style:
    # request body {"api_key": ...} or "Authorization: Bearer <key>").
    api_key: str = field(default_factory=lambda: os.environ.get("STUDYWEB_API_KEY", ""))
    # CORS: empty = send no Access-Control-Allow-Origin (safest for a localhost
    # service). Set to "*" or an explicit origin only if a browser client needs it.
    cors_allow_origin: str = field(default_factory=lambda: os.environ.get("STUDYWEB_CORS_ORIGIN", ""))
    # Reject request bodies larger than this many bytes (defends _body()).
    max_request_bytes: int = field(default_factory=lambda: _env_int("STUDYWEB_MAX_REQUEST_BYTES", 1_000_000))
    # Cap on simultaneous research/rag pipelines so a burst of clients can't
    # fan out into an unbounded number of outbound fetches.
    max_concurrent_requests: int = field(default_factory=lambda: _env_int("STUDYWEB_MAX_CONCURRENT_REQUESTS", 4))

    def as_dict(self) -> dict:
        d = asdict(self)
        # never leak secrets when a config is serialised (e.g. /health)
        for k in ("brave_api_key", "tavily_api_key", "serpapi_api_key",
                  "google_cse_key", "api_key", "naver_client_id", "naver_client_secret"):
            d[k] = bool(d[k])  # report only presence
        d["provider_keys"] = {k: bool(v) for k, v in d["provider_keys"].items()}
        return d


# Module-level singleton used across the package.
settings = Settings()
