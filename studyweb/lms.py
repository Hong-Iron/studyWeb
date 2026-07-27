"""LLM function-calling glue.

`TOOL_SCHEMAS` is an OpenAI/LM-Studio-compatible ``tools`` array. `dispatch_tool`
executes a tool call by name and returns a compact, JSON-serialisable result
sized to drop back into a model's context window.
"""

from __future__ import annotations

import json

from .research import research as _research_fn, build_rag as _build_rag
from .fetch import fetch_page


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live internet and return relevant sources with a short, "
                "locally-extracted, source-grounded answer. Use this whenever you need "
                "current facts, data, or anything you are unsure about."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "max_results": {"type": "integer", "description": "How many sources (default 5).", "default": 5},
                    "include_domains": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Restrict results to these domains, e.g. [\"danawa.com\"].",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "site_search",
            "description": (
                "Search WITHIN a specific website via its own search page — no "
                "external search engine, fully local. Best for shopping/price or "
                "reference sites (e.g. danawa.com, coupang.com, amazon.com, "
                "github.com). Returns on-site result links; follow up with open_url."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {"type": "string", "description": 'Domain to search, e.g. "danawa.com".'},
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["site", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_prices",
            "description": (
                "Look up what a product costs across several shopping sites at once "
                "and return every price found, cheapest first, with min/median/max. "
                "USE THIS FOR ANY 'how much does X cost' QUESTION — do not use "
                "web_search with site: operators, which returns snippets, not prices. "
                "Each price is read from the seller's own page, so it is exact and "
                "carries its source URL. Sites that could not be read are listed in "
                "'misses' with a reason: report those honestly, never present the "
                "remaining prices as if the whole market was checked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The product, as a shopper would type it into a shopping "
                            'site — "AMD 라이젠5 9600X", not a sentence and not a query '
                            "with site:/OR operators."
                        ),
                    },
                    "sites": {
                        "type": "array", "items": {"type": "string"},
                        "description": (
                            "Domains to check. Omit for the configured default list "
                            "(Korean shopping sites). Any domain works."
                        ),
                    },
                    "per_site": {"type": "integer", "description": "Results priced per site (default 3).", "default": 3},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Fetch one web page and return its cleaned main text (Markdown).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The absolute URL to open."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "collect_rag",
            "description": (
                "Search + crawl the web on a topic and return cleaned, RAG-ready text "
                "chunks with metadata, for building a study dataset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 4},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_data",
            "description": (
                "Extract STRUCTURED data (e.g. product name, price, specs, options) "
                "from any web page as JSON. Works across sites: it reads standard "
                "product markup when present, otherwise reads the page content to "
                "pull the requested fields. Use this when the user wants specific "
                "structured facts (prices, specs, a component list) from a URL, "
                "rather than the raw page text. Falls back to a headless browser for "
                "JavaScript-rendered pages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The page to extract from."},
                    "fields": {
                        "type": "array", "items": {"type": "string"},
                        "description": 'Fields to extract, e.g. ["name","price","gpu_options"].',
                    },
                    "render": {
                        "type": "string", "enum": ["auto", "always", "never"],
                        "description": "Headless-browser rendering (default auto).",
                    },
                },
                "required": ["url"],
            },
        },
    },
]


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + " …[truncated]"


def _req_str(args: dict, key: str) -> str:
    """Fetch a required string arg or raise ValueError with a model-readable
    message (so a malformed tool call becomes an error the model can fix,
    not a KeyError that kills the run)."""
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"missing or invalid required argument {key!r}")
    return v.strip()


def _opt_int(args: dict, key: str, default: int) -> int:
    try:
        return int(args.get(key, default))
    except (TypeError, ValueError):
        return default


def dispatch_tool(name: str, arguments: dict, *, budget_chars: int = 2400) -> dict:
    """Execute a tool call. Returns a small dict suitable for a tool message.
    Never raises: bad arguments and backend failures come back as {"error": ...}
    so the calling model can recover instead of the agent loop crashing."""
    args = arguments or {}
    try:
        if name == "web_search":
            n = _opt_int(args, "max_results", 5)
            res = _research_fn(
                _req_str(args, "query"), max_results=n,
                search_depth="advanced", include_answer=True, include_raw_content=False,
                include_domains=args.get("include_domains"))
            return {
                "query": res["query"], "answer": _truncate(res.get("answer", ""), 800),
                "results": [
                    {"title": r["title"], "url": r["url"],
                     "content": _truncate(r["content"], budget_chars // max(len(res["results"]), 1))}
                    for r in res["results"][:n]
                ],
            }
        if name == "find_prices":
            from .prices import find_prices
            out = find_prices(_req_str(args, "query"), sites=args.get("sites"),
                              per_site=_opt_int(args, "per_site", 3))
            # Trim to what a model needs to answer and cite: the number, the
            # name, the source. Snippets and brands only pad the context.
            return {
                "query": out["query"], "summary": out["summary"],
                "quotes": [{"site": q["site"], "price": q["price"], "title": q["title"],
                            "url": q["url"]} for q in out["quotes"][:12]],
                "misses": out["misses"],
            }
        if name == "site_search":
            n = _opt_int(args, "max_results", 5)
            site = _req_str(args, "site")
            res = _research_fn(
                _req_str(args, "query"), max_results=n,
                search_depth="advanced", include_answer=True, include_raw_content=False,
                site=site)
            return {
                "site": site, "query": res["query"],
                "answer": _truncate(res.get("answer", ""), 800),
                "results": [
                    {"title": r["title"], "url": r["url"],
                     "content": _truncate(r["content"], budget_chars // max(len(res["results"]), 1))}
                    for r in res["results"][:n]
                ],
            }
        if name == "open_url":
            url = _req_str(args, "url")
            d = fetch_page(url)
            if d.ok:
                return {"url": d.url, "title": d.title,
                        "content": _truncate(d.markdown or d.text, 3500)}
            # The URL failed (often a hallucinated/404 link). Try to recover the
            # intended page by searching the same site before giving up.
            from .config import settings
            if settings.recover_urls:
                from .recover import open_best
                doc, real_url, cands = open_best(
                    url, max_candidates=settings.recover_max_candidates)
                if doc is not None and doc.ok:
                    alts = [{"title": c.title, "url": c.url}
                            for c in cands if c.url != doc.url][:4]
                    return {
                        "url": doc.url, "title": doc.title,
                        "content": _truncate(doc.markdown or doc.text, 3500),
                        "recovered_from": url,
                        "note": (f"요청한 URL '{url}' 을(를) 열 수 없어(404/오류) 같은 사이트에서 "
                                 "가장 근접한 실제 페이지를 대신 열었습니다. 원하는 페이지가 아니면 "
                                 "alternatives 중 하나를 open_url 로 다시 시도하세요."),
                        "alternatives": alts,
                    }
                if cands:
                    return {
                        "error": f"요청한 URL '{url}' 을(를) 찾을 수 없습니다(404/오류).",
                        "suggestions": [{"title": c.title, "url": c.url} for c in cands],
                        "note": "위 suggestions 중 하나를 open_url 로 다시 시도하세요.",
                    }
            return {
                "url": url, "error": d.error or f"status {d.status}",
                "note": ("URL을 찾지 못했습니다(404/오류). 추측한 URL 대신 web_search "
                         "또는 site_search 로 올바른 링크를 먼저 찾은 뒤 그 URL을 open_url 하세요."),
            }
        if name == "collect_rag":
            rag = _build_rag(query=_req_str(args, "query"),
                             max_results=_opt_int(args, "max_results", 4))
            return {"query": rag["query"], "n_documents": rag["n_documents"],
                    "n_chunks": rag["n_chunks"],
                    "sample_chunks": [
                        {"source_url": c["metadata"]["source_url"],
                         "text": _truncate(c["text"], 300)}
                        for c in rag["chunks"][:5]
                    ]}
        if name == "extract_data":
            from .dataextract import extract_data as _extract_data
            fields = args.get("fields")
            render_mode = args.get("render", "auto")
            if render_mode not in ("auto", "always", "never"):
                render_mode = "auto"
            res = _extract_data(_req_str(args, "url"), schema=fields or None,
                                render_mode=render_mode)
            out = {"url": res["url"], "method": res["method"],
                   "rendered": res["rendered"], "data": res["data"],
                   "warnings": res["warnings"]}
            if res.get("recovered_from"):
                out["recovered_from"] = res["recovered_from"]
                out["note"] = ("요청한 URL을 열 수 없어 같은 사이트에서 실제 페이지를 찾아 "
                               "대신 추출했습니다. url 필드가 실제로 사용된 페이지입니다.")
            return out
        return {"error": f"unknown tool: {name}"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface backend errors to the model
        return {"error": f"{type(exc).__name__}: {exc}"}


def tool_result_message(call_id: str, name: str, result: dict) -> dict:
    """Wrap a dispatch result as an OpenAI-style `tool` role message."""
    return {"role": "tool", "tool_call_id": call_id, "name": name,
            "content": json.dumps(result, ensure_ascii=False)}
