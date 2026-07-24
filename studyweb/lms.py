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
            if not d.ok:
                return {"url": url, "error": d.error or f"status {d.status}"}
            return {"url": d.url, "title": d.title, "content": _truncate(d.markdown or d.text, 3500)}
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
            return {"url": res["url"], "method": res["method"],
                    "rendered": res["rendered"], "data": res["data"],
                    "warnings": res["warnings"]}
        return {"error": f"unknown tool: {name}"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface backend errors to the model
        return {"error": f"{type(exc).__name__}: {exc}"}


def tool_result_message(call_id: str, name: str, result: dict) -> dict:
    """Wrap a dispatch result as an OpenAI-style `tool` role message."""
    return {"role": "tool", "tool_call_id": call_id, "name": name,
            "content": json.dumps(result, ensure_ascii=False)}
