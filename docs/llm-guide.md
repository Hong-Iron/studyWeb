# studyweb tools — a guide for the model using them

You are reading this because you have studyweb's tools available. This document
is written to *you*, the model. It tells you which tool answers which question,
how to phrase the arguments, and what to do when a tool comes back empty.

The short version, if you only read one section:

> Use `find_prices` for "how much does X cost". Use `web_search` for general
> questions. Use `site_search` + `open_url` when you know the site. Never put
> `site:` or `OR` operators in a query. Cite the URL for every figure. If a tool
> reports that a source failed, say so — do not present a partial answer as a
> complete one.

---

## 1. Picking the tool

| The user wants | Tool | Why not the others |
|---|---|---|
| The price of a product | **`find_prices`** | `web_search` returns snippets *about* the product, not its price |
| A current fact, news, an explanation | **`web_search`** | It searches, reads the pages, and returns a grounded answer |
| Results from one specific site | **`site_search`** | Goes through that site's own search page — no search engine, so it isn't rate-limited or blocked |
| The contents of a page you already have a URL for | **`open_url`** | Searching again for a page you can already open wastes a call |
| Specific fields (price, spec, model) from one page | **`extract_data`** | Returns structured JSON, not prose you have to re-read |
| A study corpus on a topic | **`collect_rag`** | Returns cleaned chunks with metadata |

If two tools could work, pick the one that answers in a single call.

## 2. Rules

**Write queries the way a person types them into a search box.**
`AMD 라이젠5 9600X` — not a sentence, not `AMD Ryzen 5 9600X (가격 OR 견적 OR 비용)`,
and never with `site:` operators. Engines return almost nothing for OR-chained
`site:` queries. To restrict to sites, use the `include_domains` argument of
`web_search`, or `site_search`, or the `sites` argument of `find_prices` — the
arguments exist so you never have to encode this into the text.

**Search in the language of the sources.** Korean products on Korean sites:
query in Korean. English documentation: query in English.

**Never state a number you did not get from a tool.** Prices, versions, dates,
benchmark figures — if a tool did not return it, you do not know it. Say what
you could not find rather than filling the gap.

**Every figure carries its source URL.** The user has to be able to check it.

**Report what failed.** Tools tell you when a source could not be read. Passing
that on is not an apology, it is part of the answer: "다나와 기준 최저 260,710원
(쿠팡·11번가는 확인 실패)" is honest; dropping the second half is not.

**One call per question, then stop.** These tools hit the live network — each
call costs seconds. Do not call the same tool twice with near-identical
arguments hoping for a better answer. If a call comes back empty, change the
approach (see §4), not the wording.

## 3. The tools

### `find_prices(query, sites?, per_site?)`

What something costs, across several shopping sites at once.

```json
{"query": "AMD 라이젠5 9600X", "per_site": 3}
```

Returns:

```json
{"query": "…",
 "summary": {"count": 2, "currency": "KRW", "min": 260710, "median": 262855,
             "max": 265000, "cheapest_url": "https://…", "by_site": {"danawa.com": 260710}},
 "quotes": [{"site": "danawa.com", "price": 260710, "title": "AMD 라이젠5-6세대 9600X (멀티팩 정품)", "url": "https://…"}],
 "misses": [{"site": "coupang.com", "reason": "no results — the site's search page returned nothing to a static fetch"}]}
```

- `summary` is `null` when nothing could be priced. That means **no price was
  found** — it does not mean the item is free or unavailable. Say so and list
  the misses.
- `quotes` is sorted cheapest first. Each price came from that seller's own page.
- `misses` lists sites that could not be read. **Always mention them** when you
  report a minimum, because the real minimum may be on a site that failed.
- Prices are what the site listed at that moment, before shipping and options.
  Report them as such; do not describe one as "the cheapest in Korea".
- `sites` accepts any domains. Omit it to use the configured default list.

### `web_search(query, max_results?, include_domains?)`

General search. Returns `{query, answer, results:[{title, url, content}]}`.
`answer` is extracted from the sources, not generated — it is a starting point,
and the `results` are what you actually cite. Use `include_domains` to scope by
site instead of writing `site:` in the query.

### `site_search(site, query, max_results?)`

Searches one site through its own search page. Use it when the user names a site,
when a general engine keeps missing, or for shopping and reference sites.
Returns links in the site's own ranking. **The results carry little text** —
follow up with `open_url` or `extract_data` on the ones that matter.

### `open_url(url)`

Fetches one page and returns its cleaned main text as Markdown. Use it on URLs
you already have. If the URL was wrong or dead, the tool may recover the intended
page and tell you so — check for `recovered_from` and mention that the URL
differed if it matters.

### `extract_data(url, fields?)`

Pulls structured fields out of one page: `{url, method, data, warnings}`.
`method` tells you where the data came from — `structured:json-ld` means the site
published it itself and the values are exact; an LLM-based method means they were
inferred and deserve more caution. Ask for the fields you need
(`["name", "price", "release_date"]`) rather than taking the default set.

### `collect_rag(query, max_results?)`

Search + crawl + clean into chunks for a study dataset. Not for answering a
single question — it returns a lot of text.

## 4. When a tool comes back empty

Change the approach, don't repeat the call.

| What you got | Do this |
|---|---|
| `find_prices` → `summary: null`, all misses | Say no price was found and name the sites that failed. Then try `site_search` on one of them and `extract_data` on a result. |
| `web_search` → no results | Drop qualifiers and search the core noun. If you named sites, try `site_search` on one instead. |
| `site_search` → empty | That site's listing probably needs JavaScript. Try `web_search` with `include_domains` for the same site. |
| `open_url` → error | Search for the page rather than guessing another URL. Invented URLs are worse than none. |
| Any tool → `{"error": …}` | Read the message. A missing key or an unreachable backend is a configuration problem: report it plainly instead of retrying. |

## 5. Worked examples

**"라이젠 9600X 가격 얼마야?"**

```
find_prices({"query": "AMD 라이젠5 9600X"})
```
→ 260,710원 ~ 265,000원 (다나와 기준, 2건). 최저가는 멀티팩 정품 260,710원입니다.
쿠팡·11번가는 조회에 실패해 제외했습니다. 출처: https://prod.danawa.com/info/?pcode=62794079

Not this: `web_search({"query": "라이젠 9600X 가격 (site:danawa.com OR site:coupang.com)"})`
— that returns nothing usable, and any number you write from it is a guess.

**"다나와에서 9600X 리뷰 좋은 거 찾아줘"**

```
site_search({"site": "danawa.com", "query": "라이젠5 9600X"})
open_url({"url": "<the most relevant result>"})
```

**"이 페이지 스펙 정리해줘" + URL**

```
extract_data({"url": "…", "fields": ["name", "price", "specs"]})
```

## 6. Anti-patterns

- `site:` or `OR` operators inside a query string — the arguments do that job.
- Reporting a minimum price without mentioning `misses`.
- Repeating a failed search with slightly different words.
- Calling `web_search` for a page you already have the URL of.
- Writing a number the tools didn't return.
- Describing a listed price as a final cost — shipping, options and VAT are not in it.

---

If the LM Studio plugin is what exposes these tools to you, two more are
available: `studyweb_status()` reports whether the backend and each model
provider are reachable, and `ask_expert(question)` hands a hard question to an
external model that researches it with these same tools. `ask_expert` is slow
and costs money — use it only when your own attempt with the tools above has
genuinely failed.
