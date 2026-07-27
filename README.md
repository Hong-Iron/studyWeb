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
- 📦 **Tiny footprint** — only `requests` + `lxml`. The HTTP API server uses
  the standard library alone.

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

Requires Python ≥ 3.10.

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
`open_url` / `collect_rag`, `studyweb` executes them against the live web, and
the results are fed back until the model produces its answer. You can also grab
the raw schemas with `from studyweb.lms import TOOL_SCHEMAS, dispatch_tool` and
wire them into any OpenAI-style client yourself.

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
layer, so `extract_data(url, llm_provider="openai")` works too.

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

Two companion projects turn this backend into point-and-click tools — start
`studyweb serve` and use one of:

- **`studyweb-lmstudio`** — an LM Studio plugin. Any model in the LM Studio GUI
  gets `web_search` / `open_url` / `collect_rag`. (`lms dev --install`)
- **`studyweb-obsidian`** — an Obsidian plugin. A right-pane chat with your
  local model + web tools, with one-click "save to note".

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

- **brave** (`BRAVE_API_KEY`), **tavily** (`TAVILY_API_KEY`),
  **serpapi** (`SERPAPI_API_KEY`), **google_cse** (`GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX`),
  **searxng** (`SEARXNG_URL`)

`provider="auto"` (the default) tries keyed providers first, then the free ones,
so a single rate-limited backend never leaves you empty-handed.

If an engine is unreachable from your network, take it out of the chain —
otherwise every search pays its full connect timeout before moving on, and
twice over when the domain-scoped recall retry fires:

```bash
STUDYWEB_SEARCH_DISABLE=duckduckgo      # comma-separated provider ids
```

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

A built-in registry covers common sites (Danawa, Coupang, Amazon, eBay, GitHub,
StackOverflow, Reddit, PyPI, npm, YouTube, Wikipedia, …); for any other site it
reads the homepage's search `<form>` to build the query URL automatically. Over
HTTP: `POST /search {"query": ..., "site": "danawa.com"}`. As an LLM tool it's
exposed as `site_search(site, query)`. Works best on server-rendered pages.

---

## Good-web-citizen defaults

- Honours `robots.txt` (toggle with `STUDYWEB_RESPECT_ROBOTS`)
- Per-host rate limiting (`STUDYWEB_PER_HOST_DELAY`, default 1 req/s)
- Response size caps, request timeouts, and retry-with-backoff
- On-disk response cache to avoid re-fetching

It presents a normal browser User-Agent by default (many sites reject obvious
bot UAs), but you can set an identifiable one with `STUDYWEB_UA`. **You are
responsible for complying with each site's Terms of Service and applicable law.**

See [`.env.example`](./.env.example) for the full list of configuration knobs.

---

## Limitations (be honest about these)

- **No JavaScript execution.** Pages that render prices/content purely client-side
  (many SPA storefronts, e.g. Samsung's product pages) expose little in their raw
  HTML. Server-rendered pages (e.g. Danawa product listings) work well.
- **Some sites block scraping** (Cloudflare "Just a moment…", hard 403s). `studyweb`
  routes around them via other search results rather than defeating the protection.
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
