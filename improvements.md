# studyweb — Improvements toward production

Status: 219/220 tests pass (220/220 with the `scrapling` extra installed). The
engine is solid for local use; the items below are what stands between "works on
my machine" and something you can publish, expose on a LAN, or run unattended.
Ordered by priority.

## Shipped

### 0.3.0 — pluggable fetch engines + self-healing selectors

Closes the two limitations the README used to just admit to: no JavaScript, and
no answer to a site that blocks scripts.

- `engines.py` — one transport contract behind five backends
  (`static` → `scrapling` → `chrome` → `dynamic` → `stealth`), ordered by cost.
  The politeness layer (robots, SSRF, per-host throttle, size cap, cache) stays
  in `net.get()` and wraps *every* engine, so a stronger backend never buys
  reach at the cost of safety. `stealth` is opt-in and never on the ladder.
- Escalation on two separate signals: an anti-bot wall (status/body markers,
  handled in `net.get`) and a JS shell (word count after extraction, handled in
  `fetch_page` — the signal does not exist until the page has been parsed).
- `adaptive.py` — Scrapling's element tracking for site adapters, so Itmaya's
  hardcoded `.price_system_start` relocates by similarity instead of silently
  reading zero. Scalar fields only: relocation finds *an element*, and applying
  it to the nested component table would invent prices rather than recover them.
- Scrapling is an optional extra. Without it the ladder collapses to
  `[static]`, adaptive selection no-ops, and the install stays `requests` +
  `lxml`.
- `studyweb engines`, `studyweb fetch --engine`, and both maps on `GET /health`.

Remaining from this area: nothing blocking. Scrapling's Spider machinery
(pause/resume, sessions/cookies, proxy rotation, XHR capture) was deliberately
left out — studyweb crawls 5–10 pages per question, so it would not pay for
itself. Revisit if login-gated pages or large crawls become a use case.

## P0 — Correctness & security bugs

### 1. `GET /search` bypasses API-key auth
`server.py:58-70` — `_auth_ok()` is only called in `do_POST`. When
`STUDYWEB_API_KEY` is set, anyone can still search via
`GET /search?q=...` with no key. Move the auth check into a shared
pre-route step for both verbs (health/tool-schema can stay open).

### 2. Domain filters match by raw suffix, not domain boundary
`search.py:228-236` — `netloc.endswith(("danawa.com",))` also matches
`notdanawa.com`, so `include_domains` admits look-alike domains and
`exclude_domains` over-blocks. Use the same rule `sitesearch.py` already gets
right: `host == d or host.endswith("." + d)`. `crawl.py:45` (`allow_domains`)
has the identical bug.

### 3. Non-UTF-8 pages are mojibake before extraction
`extract.py:319-320` decodes bytes as UTF-8-replace, ignoring both the HTTP
`charset` header and `<meta charset>`. EUC-KR sites (still common in Korea —
a core use case here) come out as `Ʈ�Դϴ�`. Fix: pass **bytes** straight to
`lxml.html.fromstring` (it honours the meta charset), or decode via
`Response.text` (which reads the header) and fall back to bytes. Add a test
with an EUC-KR fixture.

### 4. CORS is wide open while auth can be off
`server.py:41` sends `Access-Control-Allow-Origin: *` on every response. For a
localhost service with no key, that lets any web page you visit drive your
search/crawl API (classic drive-by / DNS-rebinding target). Default to no CORS
header (or `null`), make the allowed origin a setting, and add an `OPTIONS`
preflight handler if you actually want browser clients (right now a
cross-origin JSON POST fails preflight anyway — the `*` is decorative).

### 5. SSRF: `/extract` and `/rag` fetch arbitrary URLs
Anything that can reach the server can make it fetch `http://169.254.169.254/`,
`http://localhost:???/admin`, internal LAN hosts, etc. Add an opt-out guard in
`net.get()` that resolves the host and refuses private/link-local/loopback
ranges (`STUDYWEB_ALLOW_PRIVATE=true` to disable for intranet use).

### 6. Tool/agent crash on missing arguments
`lms.py:104-146` — `args["query"]` / `args["url"]` raise `KeyError` when a
model emits malformed arguments, and `lmstudio.py:70` calls `dispatch_tool`
with no try/except, so one bad tool call kills the whole agent run. Validate
arguments and return `{"error": ...}` so the model can self-correct; wrap the
dispatch in `run_agent` too.

## P1 — Robustness for a long-running server

### 7. Input validation and error semantics in the HTTP API
- `server.py:100` — missing `query` → `KeyError` → 500. Return 400 with a clear
  message for all malformed input (including non-int `max_results`,
  `chunk_size`, etc.).
- Clamp resource-shaping parameters server-side: `max_results`, `max_pages`,
  `crawl_depth`, `chunk_size`. Today a single `POST /rag` with
  `crawl_depth=5, max_pages=100000` is a self-DoS.
- Cap accepted request body size (`Content-Length` is trusted blindly in
  `_body()`).

### 8. Unbounded concurrency
`ThreadingHTTPServer` spawns a thread per connection, and each advanced search
fetches up to N pages on its own pool. A small burst of clients multiplies into
a large number of outbound requests. Add a global semaphore around the
research/rag pipelines (e.g. `STUDYWEB_MAX_CONCURRENT_REQUESTS`) or move to a
worker-pool server.

### 9. Graceful shutdown
`serve()` only handles `KeyboardInterrupt`. Under Docker/systemd, SIGTERM is
ignored until the kill timeout. Register a `signal.SIGTERM` handler that calls
`httpd.shutdown()`. Relatedly the Dockerfile should add a `HEALTHCHECK`
(GET /health) and a non-root user.

### 10. Logging
`log_message` is silenced and the library never logs. Adopt the `logging`
module throughout (`studyweb.*` loggers): access log at INFO behind a flag,
per-provider failures at DEBUG (today the `errors` list in `search.py:277` is
built and then thrown away when `auto` returns empty — surface it in the
response, e.g. `"search_errors": [...]`, and log it).

### 11. HTTP cache needs a size cap
`net.py:_cache_put` writes hex-encoded JSON (2× content size) into
`~/.cache/studyweb` forever; TTL only gates reads, nothing ever deletes files.
Add: binary storage (or base64), a max-size setting, and a periodic sweep that
evicts expired/oldest entries. Also `_host_locks`/`_host_last` grow per host
for the life of the process.

### 12. Retry/politeness details
- Honour `Retry-After` on 429 responses (`net.py:191`).
- Robots is checked for the request URL only; a redirect to a disallowed path
  is followed anyway (`allow_redirects=True`). Re-check on the final URL.
- `robots.crawl_delay()` exists but nothing uses it — feed it into
  `_throttle()` so declared crawl delays are respected.
- Timing-safe compare for the API key: `hmac.compare_digest`
  (`server.py:29`).

## P2 — Quality of results

### 13. Korean ranking recall
`rank.py` tokenises Hangul as whole runs, so `삼성전자는` ≠ `삼성전자` and BM25
recall on Korean is poor — significant given the Danawa/Naver use cases.
Cheapest effective fix: index CJK text as character bigrams alongside word
tokens (no new dependency); optionally support `kiwipiepy` as an extra.

### 14. Search provider resilience
- Bing scraping fails silently on consent/CAPTCHA pages (empty parse → falls
  through to Wikipedia). Detect those pages and record a distinct error.
- Add a second no-key general provider (e.g. DuckDuckGo HTML endpoint) so
  `auto` has real redundancy.
- Keyed providers (`search.py:132-182`) never call `raise_for_status()`; an
  invalid key yields an empty result set that looks like "no hits". Check
  status and raise `SearchError` with the body.
- `_wikipedia` is hardcoded to `en`; derive the language from
  `settings.search_market` (`ko-KR` → `ko.wikipedia.org`).

### 15. Extraction gaps
- Tables are dropped from `passages` (`extract.py:351`), so price/spec tables —
  the thing a Danawa query is about — never reach BM25 or the answer. Include a
  flattened text form of tables as passages.
- `_title()` comments say it trims " - Site Name" suffixes but doesn't
  (`extract.py:113`).
- `_est_tokens` (`clean.py:158`, chars/4) undercounts Korean ~2×; use a
  script-aware estimate.

## P3 — Packaging & project hygiene

- **Publishable metadata**: `pyproject.toml` still points at
  `github.com/your-org/studyweb`; author is a placeholder. `__version__` is
  duplicated in `__init__.py` and `pyproject.toml` — single-source it
  (`[project] dynamic = ["version"]` or read `importlib.metadata`).
- **Repo hygiene**: `build/`, `studyweb.egg-info/`, `.pytest_cache/` exist in
  the working tree and `a.txt` is a stray empty file — delete them (gitignore
  already covers the first three). `requirements.txt` duplicates pyproject
  deps; keep one (pyproject) and delete the other or generate it.
- **CI & tooling**: no lint/type/CI config. Add `ruff` (lint+format), `mypy`
  (the codebase is already fully annotated — cheap win), and a GitHub Actions
  workflow running ruff + mypy + pytest on 3.10–3.13.
- **Test gaps**: no tests for server auth (would have caught P0-1), domain
  filtering (P0-2), encodings (P0-3), the `sitesearch` form-discovery fallback,
  or `run_agent`'s loop. All are mockable offline.
- **Docs**: README documents the API; add a short "production deployment" note
  (behind a reverse proxy, API key set, cache dir on a volume) once the P0/P1
  items land.
