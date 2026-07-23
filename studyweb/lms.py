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
]


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + " …[truncated]"


def dispatch_tool(name: str, arguments: dict, *, budget_chars: int = 2400) -> dict:
    """Execute a tool call. Returns a small dict suitable for a tool message."""
    args = arguments or {}
    if name == "web_search":
        res = _research_fn(
            args["query"], max_results=int(args.get("max_results", 5)),
            search_depth="advanced", include_answer=True, include_raw_content=False,
            include_domains=args.get("include_domains"))
        return {
            "query": res["query"], "answer": _truncate(res.get("answer", ""), 800),
            "results": [
                {"title": r["title"], "url": r["url"],
                 "content": _truncate(r["content"], budget_chars // max(len(res["results"]), 1))}
                for r in res["results"][:int(args.get("max_results", 5))]
            ],
        }
    if name == "site_search":
        res = _research_fn(
            args["query"], max_results=int(args.get("max_results", 5)),
            search_depth="advanced", include_answer=True, include_raw_content=False,
            site=args["site"])
        return {
            "site": args["site"], "query": res["query"],
            "answer": _truncate(res.get("answer", ""), 800),
            "results": [
                {"title": r["title"], "url": r["url"],
                 "content": _truncate(r["content"], budget_chars // max(len(res["results"]), 1))}
                for r in res["results"][:int(args.get("max_results", 5))]
            ],
        }
    if name == "open_url":
        d = fetch_page(args["url"])
        if not d.ok:
            return {"url": args["url"], "error": d.error or f"status {d.status}"}
        return {"url": d.url, "title": d.title, "content": _truncate(d.markdown or d.text, 3500)}
    if name == "collect_rag":
        rag = _build_rag(query=args["query"],
                         max_results=int(args.get("max_results", 4)))
        return {"query": rag["query"], "n_documents": rag["n_documents"],
                "n_chunks": rag["n_chunks"],
                "sample_chunks": [
                    {"source_url": c["metadata"]["source_url"],
                     "text": _truncate(c["text"], 300)}
                    for c in rag["chunks"][:5]
                ]}
    return {"error": f"unknown tool: {name}"}


def tool_result_message(call_id: str, name: str, result: dict) -> dict:
    """Wrap a dispatch result as an OpenAI-style `tool` role message."""
    return {"role": "tool", "tool_call_id": call_id, "name": name,
            "content": json.dumps(result, ensure_ascii=False)}
