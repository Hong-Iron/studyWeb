# studyweb

A **local, free, private** web **search + crawl + RAG** toolkit — a self-hosted
replacement for [Tavily](https://tavily.com). Built to plug into a local LLM
(e.g. **LM Studio**) so a model can search the live internet, read pages, and
turn them into clean, RAG-ready data for study and data collection.

- 🔌 **Drop-in Tavily replacement** — `POST /search` speaks the same
  request/response shape. Point your existing client at `localhost:8787` and
  delete the API key.
- 🏠 **Runs entirely on your machine** — your queries never leave your infra
  (only the actual page fetches go out). No per-search cost.
- 🧹 **RAG pipeline built in** — `crawl → strip → clean → chunk → output`
  produces embed-ready chunks with metadata (generic + LangChain/LlamaIndex shapes).
- 🧠 **LLM tool-calling ready** — ships OpenAI-style tool schemas and a
  dispatcher; verified end-to-end with a local Gemma model via LM Studio.
- 💰 **Prices, not snippets** — `studyweb prices` checks a list of shopping
  sites and reads each price off the seller's own page, exact and sourced.
- ☁️ **Any model** — one tool-calling loop over LM Studio, OpenAI, the Claude
  API, NVIDIA NIM, the `claude` CLI, or any OpenAI-compatible endpoint, with
  every call priced and counted.
- 📦 **Tiny footprint** — only `requests` + `lxml`. The HTTP API server uses
  the standard library alone.
- 🖥️ **Two GUIs** — the [LM Studio](https://github.com/Hong-Iron/studyweb-lmstudio)
  and [Obsidian](https://github.com/Hong-Iron/studyweb-obsidian) plugins are thin
  clients over this backend.

> Why not just call Tavily? Cost, privacy, and control. `studyweb` does the
> search, the content extraction, the relevance ranking, and a source-grounded
> answer — all locally, with no third-party API in the loop (though you can
> still opt into Brave/Tavily/SerpAPI/Google keys if you want their backends).

---

## Install

```bash
pip install .
# or, without installing the package:
pip install -r requirements.txt
```

Requires Python ≥ 3.10. The whole install is **`requests` + `lxml`** — small
enough to sit on a server as a always-on backend.

Optionally, stronger fetch transports and self-healing selectors:

```bash
pip install ".[scrapling]" && scrapling install   # downloads the browsers
```

That pulls in [Scrapling](https://github.com/D4Vinci/Scrapling) and is worth it
for JS-heavy or bot-hostile sites — see [Fetch engines](#fetch-engines). Without
it every code path still works; the engine ladder just has one rung.

---

## Four ways to use it

### 1. Library

```python
from studyweb import search, fetch_page, research, build_rag, Corpus

# plain search (no key needed — Bing + Wikipedia backends)
for r in search("갤럭시 탭 S11", include_domains=["danawa.com"], market="ko-KR"):
    print(r.title, r.url)

# fetch + clean one page to Markdown
doc = fetch_page("https://en.wikipedia.org/wiki/Photosynthesis")
print(doc.title, doc.word_count, doc.markdown[:200])

# Tavily-style research: search -> read -> locally-ranked answer + sources
res = research("how does photosynthesis work", max_results=5)
print(res["answer"])
for s in res["results"]:
    print(s["score"], s["title"], s["url"])
```

### 2. CLI

```bash
python -m studyweb search "quantum computing" -n 5
python -m studyweb fetch https://en.wikipedia.org/wiki/CRISPR --markdown
python -m studyweb answer "what causes inflation"          # Tavily-style answer
python -m studyweb rag  "photosynthesis" --crawl-depth 1 --out ./data
python -m studyweb serve --port 8787                       # HTTP API

python -m studyweb providers                # who's connected right now
python -m studyweb ask "GPU prices" --provider anthropic   # model + web tools
python -m studyweb prompt                   # the system prompt, to paste into a GUI
python -m studyweb usage                    # tokens and cost so far
python -m studyweb pricing --write          # export prices to edit
```

### 3. HTTP API (the Tavily-replacement server)

```bash
python -m studyweb serve --port 8787
```

```bash
# Same body shape as Tavily's /search:
curl -X POST http://localhost:8787/search -H 'Content-Type: application/json' -d '{
  "query": "retrieval augmented generation",
  "max_results": 5,
  "search_depth": "advanced",
  "include_answer": true,
  "include_raw_content": false
}'
```

| Method & path      | Purpose                                                        |
|--------------------|----------------------------------------------------------------|
| `POST /search`     | Tavily-shaped: `{query, answer, results:[{title,url,content,score}], ...}` |
| `POST /extract`    | `{urls:[...]}` → cleaned content per URL (Tavily `/extract` shape) |
| `POST /rag`        | `{query|urls, crawl_depth, chunk_size, ...}` → RAG-ready chunks |
| `POST /prices`     | `{query, sites?, per_site?}` → `{quotes, summary, misses}` price comparison |
| `GET  /search?q=`  | Convenience GET form of search                                 |
| `GET  /health`     | Status + (secret-masked) config                                |
| `GET  /tool-schema`| The OpenAI/LM-Studio tool definitions                          |
| `GET  /providers`  | Model providers; `?probe=1` dials each one for live status      |
| `GET  /models`     | `?provider=openai` → the model ids it offers                    |
| `POST /chat`       | One model turn on any provider → `{message, usage, provider, model}` |
| `POST /agent`      | `{query}` → full tool-calling loop → `{final, trace, usage}`    |
| `GET  /usage`      | Token/cost totals: session, today, last N days, all time        |
| `POST /usage/reset`| `{scope: "session"\|"all"}`                                     |
| `GET  /pricing`    | The effective per-model price table                            |

Set `STUDYWEB_API_KEY` to require auth (via `{"api_key": ...}` or
`Authorization: Bearer ...`). A provider failure comes back with a status you
can act on — `428` needs a key or a model, `401` means the key was rejected,
`429` rate-limited, `502` unreachable — plus a `kind` field, instead of a
generic 500. `POST /chat` lets a client use cloud models without holding any
keys of its own: they stay in the server's environment.

### 4. LLM tool integration (LM Studio / OpenAI-compatible)

```python
from studyweb.lmstudio import run_agent

out = run_agent(
    "갤럭시 탭 S11을 삼성 공홈과 다나와에서 검색해서 가격을 비교해줘",
    model="gemma-4-e4b-uncensored-hauhaucs-aggressive",  # any tool-capable local model
    temperature=0.0,
)
print(out["final"])
```

`run_agent` runs the full tool-calling loop: the model calls `web_search` /
`find_prices` / `open_url` / `collect_rag`, `studyweb` executes them against the
live web, and the results are fed back until the model produces its answer. You
can also grab the raw schemas with `from studyweb.lms import TOOL_SCHEMAS,
dispatch_tool` and wire them into any OpenAI-style client yourself.

**Two documents are written for the model**, not for you:

- **[`docs/system-prompt.md`](./docs/system-prompt.md)** — the prompt the agent
  already runs with (`studyweb.agent.SYSTEM_PROMPT`, also returned by
  `GET /tool-schema`). Copy it into LM Studio's *System Prompt* box, or any
  OpenAI-style client you wire the schemas into yourself, so a model there
  behaves the way `studyweb ask` does.
- **[`docs/llm-guide.md`](./docs/llm-guide.md)** — the long form of the same
  rules with worked examples: which tool answers which question, how to phrase
  arguments, what each `method` and `misses` reason means, and what to do when a
  tool comes back empty. Hand it to a model that keeps reaching for `web_search`
  with `site:` operators.

### 5. Cloud models (OpenAI · Claude · NVIDIA NIM · Claude Code)

Local stays the default, but the same loop runs against a hosted model when you
want more capability — `studyweb.providers` normalises them all to the OpenAI
message shape, so nothing else changes:

```python
from studyweb.agent import run_agent

out = run_agent("Compare RTX 5080 prices on danawa", provider="anthropic")
print(out["final"])
print(out["usage"])   # tokens, cost in USD, latency, number of calls
```

| Provider id   | What it is                     | Key                        |
|---------------|--------------------------------|----------------------------|
| `lmstudio`    | Local LM Studio (default)      | none — free, private       |
| `openai`      | OpenAI                         | `OPENAI_API_KEY`           |
| `anthropic`   | Claude API                     | `ANTHROPIC_API_KEY`        |
| `nvidia`      | NVIDIA NIM (hosted or your own container) | `NVIDIA_API_KEY`  |
| `claude-code` | The local `claude` CLI you're already signed in to | none      |
| `custom`      | Any OpenAI-compatible server (Ollama, vLLM, OpenRouter, Groq…) | optional |

Pick a default with `STUDYWEB_PROVIDER_LLM=anthropic`, a model with
`ANTHROPIC_MODEL=claude-sonnet-5`, and an endpoint with `NVIDIA_BASE_URL=...`
(handy for a self-hosted NIM). The structured-data extractor uses the same
layer, so `extract_data(url, llm_provider="openai")` works too — though it
reaches for a model last: structured markup first, then the price under the
page's own price label, and only then the LLM. When both the model and the
label produce a price, the label wins and the disagreement is reported in
`warnings`.

Claude models that removed sampling parameters are handled for you — studyweb
omits `temperature` for them instead of tripping a 400 — and the Claude Code
provider brings its own tools, so studyweb's web tools aren't attached to it.

### 6. Connection status and usage

```console
$ studyweb providers
   PROVIDER       STATUS         MODEL                    DETAIL
*✓ lmstudio       ok             gemma-4-12b-it           Connected · 6 model(s) available (6ms)
 🔑 openai         no_key         gpt-4o-mini              Set OPENAI_API_KEY to enable.
 🔑 anthropic      no_key         claude-opus-5            Set ANTHROPIC_API_KEY to enable.
 ✓ claude-code    ok             (CLI default)            2.1.220 (Claude Code) (111ms)
 ✗ custom         unreachable    —                        cannot reach … Connection refused

$ studyweb usage
SESSION      2 calls      1,000,015 tokens  (1,000,010 in / 5 out)  $5.0000 (+1 unpriced)
TODAY        2 calls      1,000,015 tokens  …
TOTAL       17 calls      3,204,881 tokens  …
```

Every call is priced and recorded — per session, per day, and all time — under
`~/.local/share/studyweb`. Statuses are `ok`, `no_key`, `no_model`,
`unauthorized`, `rate_limit`, `unreachable`, `not_installed`, `error`, so a
failure tells you what to fix rather than just that it broke.

Prices live in a table you can correct (`studyweb pricing --write` dumps it to
`~/.config/studyweb/pricing.json`); a model with no entry reports tokens with no
cost rather than inventing a figure. Turn recording off with
`STUDYWEB_USAGE_PERSIST=0`.

### GUI plugins (built on this backend)

`studyweb` is the engine of a three-project suite. Two companion projects turn
it into point-and-click tools — start `studyweb serve`, then install one:

- **[`studyweb-lmstudio`](https://github.com/Hong-Iron/studyweb-lmstudio)** — an
  LM Studio Tools Provider plugin. Any model in the LM Studio GUI gets
  `web_search` / `site_search` / `find_prices` / `open_url` / `collect_rag` /
  `extract_data`, plus `ask_expert` (hands a hard question to an external API)
  and `studyweb_status` (what's connected, what it cost). `lms dev --install`.
- **[`studyweb-obsidian`](https://github.com/Hong-Iron/studyweb-obsidian)** — an
  Obsidian plugin. A right-pane chat with a local *or* cloud model and the same
  web tools, provider status lights, per-answer token/cost receipts, and
  one-click "save research to note".

Both are thin HTTP clients over this backend, so all the heavy lifting stays in
one place and the plugins stay small and configurable.

```
   LM Studio GUI  ─▶  studyweb-lmstudio  ─┐
                                          ├─HTTP─▶  studyweb  ─▶  the web
   Obsidian pane  ─▶  studyweb-obsidian  ─┘         (search · prices · extract
                              │                      · rank · RAG · providers)
                              └─ chat + tool calls ─▶ LM Studio · OpenAI · Claude
                                                      · NVIDIA NIM · Claude Code
```

---

## The RAG pipeline: `crawl → strip → clean → chunk → output`

```python
from studyweb import build_rag, Corpus

rag = build_rag(query="quantum computing basics",
                crawl_depth=1, max_pages=15,
                chunk_size=1000, overlap=150)

# rag["chunks"] is a list of {id, text, metadata:{source_url, title, chunk_index,
# n_chars, n_tokens_est, quality, host, ...}} — ready to embed.
Corpus("./data/quantum").save_rag_records(rag["chunks"])
```

Each stage:

| Stage    | What happens                                                                    |
|----------|---------------------------------------------------------------------------------|
| `crawl`  | search and/or follow links within a domain, depth- and page-capped              |
| `strip`  | HTML chrome removed (nav, footer, ads, cookie bars, reference lists, …)          |
| `clean`  | Unicode NFKC, whitespace/hyphenation repair, boilerplate + duplicate removal     |
| `chunk`  | boundary-aware splitting with overlap; low-quality docs dropped                  |
| `output` | `{id, text, metadata}` records — or `Chunk.to_langchain()` for LangChain/LlamaIndex |

---

## Search backends

Works out of the box with **no keys**:

- **bing** — scrapes Bing's HTML SERP (general web), decodes redirect URLs
- **duckduckgo** — the HTML endpoint, as a second independent general engine
- **wikipedia** — MediaWiki search API (great for study topics)

Used automatically when their env key is present (higher quality / quota):

- **naver** (`NAVER_CLIENT_ID` + `NAVER_CLIENT_SECRET`), **brave** (`BRAVE_API_KEY`),
  **tavily** (`TAVILY_API_KEY`), **serpapi** (`SERPAPI_API_KEY`),
  **google_cse** (`GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX`), **searxng** (`SEARXNG_URL`)

### Korean search and real prices: `naver` / `naver_shop`

The [Naver Open API](https://developers.naver.com) is free for 25,000 calls a
day and indexes Korean pages far better than a scraped Bing SERP. Register an
application, then set both env vars — `naver` joins the front of the auto chain.

`naver_shop` is a separate provider that stays **out** of the auto chain: it
returns product listings, not web pages. Ask for it by name and you get prices
as numbers, with no page fetching and no LLM extraction:

```bash
studyweb search "AMD Ryzen 5 9600X" --provider naver_shop
```

```json
{"title": "AMD 라이젠5 9600X", "url": "https://…", "source": "naver_shop",
 "content": "289,000원 · 쿠팡",
 "extra": {"price_low": 289000, "price_high": null, "mall": "쿠팡",
           "brand": "AMD", "category": "디지털/가전 > PC부품 > CPU"}}
```

`extra` survives into `/search` responses, so a price comparison needs one call
instead of a crawl. Naver reports `"0"` for an undisclosed price; that becomes
`null`, never `0`.

### A private SearXNG (no key, no quota)

[`deploy/searxng/`](./deploy/searxng) has a compose file and a settings file
that run a private metasearch instance next to studyweb — Google, Bing and the
rest behind one endpoint, bound to loopback. The one setting that matters is
`search.formats: [html, json]`; a stock SearXNG serves HTML only and studyweb
sees an empty result list with no error.

`provider="auto"` (the default) tries keyed providers first, then the free ones,
so a single rate-limited backend never leaves you empty-handed.

If an engine is unreachable from your network, take it out of the chain —
otherwise every search pays its full connect timeout before moving on, and
twice over when the domain-scoped recall retry fires:

```bash
STUDYWEB_SEARCH_DISABLE=duckduckgo      # comma-separated provider ids
```

## Finding a price across a fixed list of sites

The thing a search engine is worst at. `(site:danawa.com OR site:coupang.com OR …)`
returns almost nothing, and what it returns is a snippet, not a price. So this
doesn't use a search engine at all:

```bash
studyweb prices "AMD 라이젠5 9600X" --per-site 2
```
```
      252,000원  enuri.com       AMD 라이젠5-6세대 9600X (그래니트 릿지) [멀티팩 정품]
                 https://www.enuri.com/detail.jsp?modelno=127363413
      259,000원  compuzone.co.kr [AMD] 라이젠5 그래니트 9600X (…/쿨러포함) 멀티팩
                 https://www.compuzone.co.kr/product/product_detail.htm?ProductNo=1164055
      260,720원  danawa.com      AMD 라이젠5-6세대 9600X (그래니트 릿지) (멀티팩 정품)
                 https://prod.danawa.com/info/?pcode=62794079

6건 · 최저 252,000원 · 중앙값 265,000원 · 최고 2,429,000원  (6.3s)
  - shopping.naver.com: no results — the site's search page returned nothing to a static fetch
  - 11st.co.kr: 3 page(s) found, none priced — blocked by robots.txt
  - coupang.com: no results — the site's search page returned nothing to a static fetch
```

Each site's **own** search page is crawled, then the price is read off the
product page — in this order:

| `method` | Where the number came from |
|---|---|
| `json-ld` / `microdata` / `opengraph` | The page's structured markup (`Offer.price`). Exact. |
| `dom` | The page itself, under a visible price label (판매가, 최저가, Price…). For shops that publish no markup at all. |
| `listing` | The price the site's own search results already showed. |
| `naver_api` | Naver's shopping API, when its keys are set. |

The `dom` reader drops hidden nodes before it reads, because Korean shops
routinely plant a decoy — Compuzone ships `<div style="display:none">256,000</div>`
directly in front of the real digits. It also refuses to answer without a price
label nearby: a promo banner's "99% 100원" is not what the page is selling, and
a reported miss beats a number you'd have to double-check.

No API key, no LLM, a few seconds. `POST /prices` returns the same as JSON, and
`STUDYWEB_PRICE_SITES` sets the default site list.

Sites that answer a plain fetch today: **danawa.com**, **compuzone.co.kr**,
**enuri.com**. Naver Shopping, 11st and Coupang serve a JS shell (or block
product pages in robots.txt) and are reported as misses — set `STUDYWEB_RENDER=true`
with Chromium installed, or Naver API keys, to bring them in.

Adding a shop is usually one line in `sitesearch.SITES`; for a site with no
`<form>`-based search, point the entry at whatever URL its own results page
actually fetches. Unregistered sites are handled by reading the homepage's
search form.

Two rules it sticks to:

- **A price in prose must carry its currency.** `265,000원` is a price;
  the `9600X` in a product name is not, and neither is a review count.
- **A site that yields nothing is reported, never silently dropped.** Every
  entry in `misses` says which site and why, so an empty answer can't be
  mistaken for a cheap one.

Sites that render their listings in JavaScript (Coupang, 11st, Naver shopping)
return nothing to a static fetch. Two ways out: install Chrome and leave
`STUDYWEB_RENDER=true`, which retries the listing in a headless browser; or set
the Naver API keys, after which `shopping.naver.com` is answered by the API
instead of a crawl — exact prices for the marketplaces it aggregates, no
fetching at all.

### Fully-local site search (no search engine at all)

When you already know the site, skip the search engine entirely and crawl the
site's **own** search page. This can't be rate-limited like Bing scraping and is
ideal for shopping/price and reference sites:

```python
from studyweb import site_search, research

site_search("danawa.com", "갤럭시 탭 S11")          # on-site result links
research("갤럭시 탭 S11", site="danawa.com")         # + reads pages, extracts prices
```

```bash
studyweb search "갤럭시 탭 S11" --site danawa.com
studyweb answer "갤럭시 탭 S11 최저가"  --site danawa.com
```

A built-in registry covers common sites (Danawa, Compuzone, Enuri, Coupang,
Amazon, eBay, GitHub, StackOverflow, Reddit, PyPI, npm, YouTube, Wikipedia, …);
for any other site it reads the homepage's search `<form>` to build the query
URL automatically — scoring the forms, so a shop that leads with a login box
doesn't send every query to `/member/login`. Over HTTP:
`POST /search {"query": ..., "site": "danawa.com"}`. As an LLM tool it's exposed
as `site_search(site, query)`. Works best on server-rendered pages.

---

## Fetch engines

The transport is pluggable; the politeness layer is not. `robots.txt`, the SSRF
guard, per-host throttling, the size cap and the cache all live in `net.get()`
and wrap **every** engine, so a stronger backend never costs safety.

| Engine | Tier | What it adds | Needs |
|---|---|---|---|
| `static` | 0 | plain `requests` + a browser UA | — (default) |
| `scrapling` | 1 | TLS/JA3 fingerprint impersonation | the `scrapling` extra |
| `chrome` | 2 | runs JavaScript (system Chrome `--dump-dom`) | a Chrome/Chromium binary |
| `dynamic` | 3 | a real Playwright browser | the `scrapling` extra |
| `stealth` | 4 | anti-bot fingerprint spoofing | the `scrapling` extra + opt-in |

Fetches start at `static` and climb only when a page forces it — on an anti-bot
wall (`net.get`) or on a body so empty it must be a JS shell (`fetch_page`).
Unavailable rungs are skipped, so on a bare two-dependency install the ladder
collapses to `[static]` and nothing changes.

```bash
studyweb engines                      # what this box can reach for
studyweb fetch <url> --engine chrome  # force one

# Unlock the rest. studyweb is not on PyPI, so name the extra against a
# checkout of this repo — or install the dependency directly, which works
# from anywhere and is the same thing:
pip install ".[scrapling]" && scrapling install    # from this directory
pip install "scrapling[fetchers]" && scrapling install   # anywhere
pipx inject studyweb "scrapling[fetchers]" --include-apps  # if installed via pipx
```

`stealth` is never on the default ladder. It exists so a site you are *allowed*
to read stops rejecting you for looking like a script — the robots.txt gate
still runs first, whichever engine is chosen. Turning on
`STUDYWEB_SOLVE_CLOUDFLARE` goes a step further and is yours to justify.

Site adapters additionally get **self-healing selectors** (`STUDYWEB_ADAPTIVE`):
when a hardcoded class name like Itmaya's `.price_system_start` stops matching,
the element is relocated by similarity from a fingerprint saved on an earlier
run instead of silently reading zero. Scalar fields only — relocation finds an
element, and pretending it can rebuild a nested price table would invent numbers
rather than recover them.

---

## Good-web-citizen defaults

- Honours `robots.txt` (toggle with `STUDYWEB_RESPECT_ROBOTS`) — before any
  engine is selected, including the stealth one
- Per-host rate limiting (`STUDYWEB_PER_HOST_DELAY`, default 1 req/s), applied
  around every engine so a backend's own retry loop can't multiply against ours
- Response size caps, request timeouts, and retry-with-backoff
- On-disk response cache to avoid re-fetching

It presents a normal browser User-Agent by default (many sites reject obvious
bot UAs), but you can set an identifiable one with `STUDYWEB_UA`. **You are
responsible for complying with each site's Terms of Service and applicable law.**

See [`.env.example`](./.env.example) for the full list of configuration knobs.

---

## Limitations (be honest about these)

- **JavaScript costs a browser.** The `static` default reads raw HTML only; SPA
  storefronts that render prices client-side need the `chrome` or `dynamic`
  engine, which escalation reaches automatically when a page comes back empty.
  Server-rendered pages (e.g. Danawa product listings) never pay that cost.
- **Some sites block scraping** (Cloudflare "Just a moment…", hard 403s). Escalation
  will try a browser-shaped fingerprint, but `studyweb` still prefers routing around
  a wall via other search results to defeating the protection, and never touches a
  path robots.txt disallows.
- **The built-in answer is extractive** (BM25 sentence selection), not LLM-synthesised.
  It's fully local and source-grounded; for prose answers, feed the `results` to your
  own LLM (that's exactly what the LM Studio integration does).

---

## Development

```bash
pip install pytest
python -m pytest         # offline, deterministic tests
```

The suite mocks the network, so it's fast and runs anywhere. Tests include
regressions for real bugs found while validating against live sites (namespaced
tags, over-broad noise removal, Bing redirect decoding, citation-list leakage)
plus the hardening set (auth on every route, domain-boundary filtering, charset
handling, the SSRF guard, and input validation).

## Deploy

```bash
docker build -t studyweb .
docker run -p 8787:8787 studyweb
# -> Tavily-replacement API on http://localhost:8787
```

The image runs as a non-root user and ships a `/health` HEALTHCHECK.

### Running in production

The API is safe to run locally with no configuration, but before exposing it —
even on a LAN — set at least these (see [`.env.example`](./.env.example)):

- **`STUDYWEB_API_KEY`** — require a shared secret on every data route
  (`/search`, `/extract`, `/rag`). `/health` and `/tool-schema` stay public.
- **SSRF** — the server refuses to fetch private/loopback/link-local hosts by
  default. Only set `STUDYWEB_ALLOW_PRIVATE=true` for deliberate intranet use.
- **`STUDYWEB_CORS_ORIGIN`** — left empty (no CORS header) so a random web page
  can't drive your API from the browser. Set an explicit origin only if a
  browser client needs it; avoid `*` when the API is unauthenticated.
- **`STUDYWEB_MAX_CONCURRENT_REQUESTS`** caps simultaneous search/RAG pipelines,
  and `STUDYWEB_MAX_REQUEST_BYTES` bounds request bodies — both guard against a
  single client fanning out into unbounded outbound fetches.
- Put the cache dir on a writable volume; it self-evicts past
  `STUDYWEB_CACHE_MAX_MB`. Run behind a reverse proxy (TLS) if remote.

`SIGTERM`/`SIGINT` trigger a graceful shutdown, so it stops cleanly under
Docker/systemd.

## License

MIT — see [LICENSE](./LICENSE).
