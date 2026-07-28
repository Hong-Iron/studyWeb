# studyweb tools — a guide for the model using them

You are reading this because you have studyweb's tools available. This document
is written to *you*, the model. It tells you which tool answers which question,
how to phrase the arguments, what the results mean, and what to do when a tool
comes back empty.

(If you are setting a model up rather than reading this as one: the condensed,
paste-into-a-GUI version of these rules is
[`system-prompt.md`](./system-prompt.md) — the same text the agent already uses.)

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
| The price on one named shop | **`find_prices`** with `sites` | Same tool — the shop goes in the argument, never in the query text |
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
approach (see §5), not the wording.

## 3. `find_prices` — the price tool in detail

What something costs, across several shopping sites at once.

```json
{"query": "AMD 라이젠5 9600X", "per_site": 3}
```

Returns:

```json
{"query": "…",
 "summary": {"count": 6, "currency": "KRW", "min": 252000, "median": 262610,
             "max": 1869990, "cheapest_url": "https://…",
             "by_site": {"enuri.com": 252000, "compuzone.co.kr": 259000, "danawa.com": 260720}},
 "quotes": [{"site": "enuri.com", "price": 252000, "method": "json-ld",
             "title": "AMD 라이젠5-6세대 9600X (그래니트 릿지) [멀티팩 정품]", "url": "https://…"}],
 "misses": [{"site": "coupang.com", "reason": "no results — the site's search page returned nothing to a static fetch"},
            {"site": "11st.co.kr", "reason": "3 page(s) found, none priced — blocked by robots.txt"}]}
```

### How to read it

- `summary` is `null` when nothing could be priced. That means **no price was
  found** — it does not mean the item is free or unavailable. Say so and list
  the misses.
- `quotes` is sorted cheapest first, and every price came from that seller's own
  page. No number here is a guess or an estimate.
- `quotes` follows each site's own ranking, so a query for a *part* can bring
  back whole machines that contain it — that is why `max` above is 1,869,990원
  for a CPU. **Do not report `summary.max` or `summary.median` at all** unless
  you have checked that every quote is the same product; they almost never are.
  `min`, `by_site` and the individual quotes are the figures worth reporting.
- Prices are what the site listed at that moment, before shipping, options and
  card discounts. Report them as such; do not call one "the cheapest in Korea".

### `method` — how exact a quote is

Every quote says how its number was read. All of them are the seller's own
figure, but they are not equally precise:

| `method` | Where it came from | Treat it as |
|---|---|---|
| `json-ld`, `microdata`, `opengraph` | The page's own product markup (`Offer.price`) | Exact — the site published this number for machines to read |
| `naver_api` | Naver's shopping API | Exact |
| `dom` | Read off the rendered page, under its price label (판매가 / 최저가 / Price) | Exact, but it is the number the page *displays*; a page showing both a cash and a card price may give either |
| `listing` | The price the site's search-results row showed | Exact for that row, but it may be a "from" price for a product with options |

You do not need to mention `method` unless the user asks how sure you are, or
two sites disagree by a small margin — then it is the honest explanation.

### `misses` — and what each reason means

**Always mention misses when you report a minimum**, because the real minimum
may be on a site that failed. The reasons are not interchangeable:

| Reason | What actually happened | What to say |
|---|---|---|
| `no results — the site's search page returned nothing to a static fetch` | The shop builds its results with JavaScript; there was nothing to read | "이 사이트는 조회하지 못했습니다" — the product may well be there |
| `N page(s) found, none priced — blocked by robots.txt` | studyweb found the products but the site's robots.txt forbids fetching them | Say it was **blocked**, not that there is no price. A human can open it |
| `N page(s) found, none priced — no price in the page` | The pages loaded but published no price anywhere readable | The price is probably behind a login, an option picker, or "가격 문의" |
| `N result(s) found, all for other products — the site answered with ads, not this model` | The shop returned only sponsored rows for different models (a 7500F for a 9600X query); they were dropped rather than priced | The shop was searched but does not answer for this model — say that, don't retry the same query |
| A site simply absent from both `quotes` and `misses` | It was not in the list at all | Check whether the user named it; pass it in `sites` |

### `sites` — naming shops yourself

`sites` accepts **any** domains; omit it for the configured default list. Shops
that are not in studyweb's registry get their search form read off their
homepage, so an ordinary server-rendered shop usually just works.

```json
{"query": "AMD 라이젠5 9600X", "sites": ["compuzone.co.kr"]}
```

When the user names a shop, put it in `sites` — do not write "컴퓨존에서" into
the query, and do not fall back to `web_search`. If that shop comes back as a
miss, §5 says what to do next.

You do not have to get the TLD right. A shop studyweb knows under another domain
is retried automatically (`compuzone.com` → `compuzone.co.kr`), so guess the
obvious domain and read the quote's `site` field to see which one answered — it
is the one to name in your answer.

## 4. The other tools

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

Pulls structured fields out of one page: `{url, method, data, warnings}`. Ask
for the fields you need (`["name", "price", "specs"]`) rather than taking the
default set. `method` tells you where the data came from:

| `method` | Meaning |
|---|---|
| `structured:json-ld` / `:microdata` / `:opengraph` | The site published the values itself — exact |
| `dom` | No markup; the price was read under the page's own price label — exact, but only `name` and `price` are filled in |
| `llm` | A model inferred the fields from the page text — the least certain, treat with caution |
| `llm+dom` | A model filled the fields, but the price came from the page's price label because the two disagreed. **Read `warnings`** — it names both numbers |
| `none` | Nothing could be extracted |

Always check `warnings`. It is where a recovered URL, a missing browser, or a
model/page price disagreement is reported.

### `collect_rag(query, max_results?)`

Search + crawl + clean into chunks for a study dataset. Not for answering a
single question — it returns a lot of text.

## 5. When a tool comes back empty

Change the approach, don't repeat the call.

| What you got | Do this |
|---|---|
| `find_prices` → `summary: null`, all misses | Say no price was found and name the sites that failed. Then `site_search` one of them and `extract_data` a result. |
| `find_prices` → the shop the user named is in `misses` | Try `site_search({"site": "that-shop.com", …})`. If it returns links, `extract_data` the best one. If it returns nothing, the shop needs JavaScript — say that plainly and offer the prices you *did* get. |
| `web_search` → no results | Drop qualifiers and search the core noun. If you named sites, try `site_search` on one instead. |
| `site_search` → empty | That site's listing probably needs JavaScript. Try `web_search` with `include_domains` for the same site. |
| `open_url` → error | Search for the page rather than guessing another URL. Invented URLs are worse than none. |
| Any tool → `{"error": …}` | Read the message. A missing key or an unreachable backend is a configuration problem: report it plainly instead of retrying. |

Escalate at most one step. Two failed approaches is an answer — "확인하지
못했습니다, 이유는 …" — not a reason for a third.

## 6. Worked examples

**"라이젠 9600X 가격 얼마야?"**

```
find_prices({"query": "AMD 라이젠5 9600X"})
```
→ 252,000원 ~ 260,720원 (에누리·컴퓨존·다나와). 최저가는 에누리 멀티팩 정품 252,000원입니다.
네이버쇼핑·쿠팡은 조회에 실패했고, 11번가는 robots.txt로 막혀 있어 제외했습니다.
출처: https://www.enuri.com/detail.jsp?modelno=127363413

Not this: `web_search({"query": "라이젠 9600X 가격 (site:danawa.com OR site:coupang.com)"})`
— that returns nothing usable, and any number you write from it is a guess.

**"컴퓨존에서 9600X 얼마야?"**

```
find_prices({"query": "AMD 라이젠5 9600X", "sites": ["compuzone.co.kr"]})
```
→ 컴퓨존 기준 259,000원 (쿨러포함 멀티팩), 쿨러미포함은 264,500원입니다.
출처: https://www.compuzone.co.kr/product/product_detail.htm?ProductNo=1164055

The shop belongs in `sites`. Do not put "컴퓨존" in the query text, and do not
reach for `web_search` because the shop sounds unusual.

**"다나와에서 9600X 리뷰 좋은 거 찾아줘"**

```
site_search({"site": "danawa.com", "query": "라이젠5 9600X"})
open_url({"url": "<the most relevant result>"})
```

**"이 페이지 스펙 정리해줘" + URL**

```
extract_data({"url": "…", "fields": ["name", "price", "specs"]})
```

## 7. Anti-patterns

- `site:` or `OR` operators inside a query string — the arguments do that job.
- Reporting a minimum price without mentioning `misses`.
- Reading "blocked by robots.txt" as "this product has no price".
- Quoting `summary.max` or `median` without checking that every quote is the
  same kind of thing.
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
