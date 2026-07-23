"""Command-line interface.

    python -m studyweb search "photosynthesis" -n 5
    python -m studyweb fetch https://en.wikipedia.org/wiki/Photosynthesis --markdown
    python -m studyweb answer "how does CRISPR work"          # Tavily-style
    python -m studyweb rag "quantum computing basics" --out ./data --crawl-depth 1
    python -m studyweb serve --port 8787
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import settings
from .search import search
from .fetch import fetch_page
from .research import research as _research_fn, build_rag as _build_rag
from .collect import Corpus
from .server import serve


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_search(a) -> int:
    res = search(a.query, provider=a.provider, max_results=a.n, site=a.site)
    if a.json:
        _print_json([r.to_dict() for r in res])
    else:
        for i, r in enumerate(res, 1):
            print(f"{i}. {r.title}\n   {r.url}\n   {r.snippet[:160]}\n")
    return 0


def cmd_fetch(a) -> int:
    d = fetch_page(a.url)
    if not d.ok:
        print(f"error: {d.error or d.status}", file=sys.stderr)
        return 1
    if a.json:
        _print_json(d.to_dict(include_links=a.links))
    else:
        body = d.text if a.text else d.markdown
        print(f"# {d.title}\n(source: {d.url}, {d.word_count} words)\n\n{body}")
    return 0


def cmd_answer(a) -> int:
    res = _research_fn(a.query, max_results=a.n,
                       search_depth=a.depth, include_answer=True,
                       include_raw_content=a.raw, provider=a.provider, site=a.site)
    if a.json:
        _print_json(res)
    else:
        print(f"Q: {res['query']}\n\nANSWER:\n{res['answer']}\n")
        print(f"SOURCES ({res['provider']}, {res['response_time']}s):")
        for i, r in enumerate(res["results"], 1):
            print(f"  {i}. [{r['score']}] {r['title']}\n     {r['url']}")
    return 0


def cmd_rag(a) -> int:
    rag = _build_rag(query=a.query, urls=a.url or None,
                     max_results=a.n, crawl_depth=a.crawl_depth,
                     max_pages=a.max_pages, chunk_size=a.chunk_size,
                     overlap=a.overlap, provider=a.provider)
    if a.out:
        c = Corpus(a.out)
        path = c.save_rag_records(rag["chunks"])
        print(f"{rag['n_chunks']} chunks from {rag['n_documents']} docs -> {path}")
    if a.json:
        _print_json(rag)
    elif not a.out:
        print(f"{rag['n_chunks']} chunks from {rag['n_documents']} docs")
        for c in rag["chunks"][:3]:
            print(f"  - {c['metadata']['source_url']}: {c['text'][:120]}…")
    return 0


def cmd_serve(a) -> int:
    serve(host=a.host, port=a.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("studyweb", description="Local web search/crawl for study & RAG.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="web search")
    s.add_argument("query"); s.add_argument("-n", type=int, default=settings.default_max_results)
    s.add_argument("--provider", default=None)
    s.add_argument("--site", default=None, help="search within one site directly (no Bing), e.g. danawa.com")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    f = sub.add_parser("fetch", help="fetch & clean one page")
    f.add_argument("url"); f.add_argument("--text", action="store_true")
    f.add_argument("--links", action="store_true"); f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_fetch)

    an = sub.add_parser("answer", help="search + local answer (Tavily-style)")
    an.add_argument("query"); an.add_argument("-n", type=int, default=settings.default_max_results)
    an.add_argument("--depth", choices=["basic", "advanced"], default="advanced")
    an.add_argument("--provider", default=None); an.add_argument("--raw", action="store_true")
    an.add_argument("--site", default=None, help="search within one site directly (no Bing), e.g. danawa.com")
    an.add_argument("--json", action="store_true"); an.set_defaults(func=cmd_answer)

    r = sub.add_parser("rag", help="crawl -> clean -> chunk -> RAG records")
    r.add_argument("query", nargs="?"); r.add_argument("--url", action="append")
    r.add_argument("-n", type=int, default=settings.default_max_results)
    r.add_argument("--crawl-depth", type=int, default=0, dest="crawl_depth")
    r.add_argument("--max-pages", type=int, default=20, dest="max_pages")
    r.add_argument("--chunk-size", type=int, default=1000, dest="chunk_size")
    r.add_argument("--overlap", type=int, default=150)
    r.add_argument("--provider", default=None); r.add_argument("--out", default=None)
    r.add_argument("--json", action="store_true"); r.set_defaults(func=cmd_rag)

    sv = sub.add_parser("serve", help="run the HTTP API")
    sv.add_argument("--host", default="127.0.0.1"); sv.add_argument("--port", type=int, default=8787)
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "rag" and not args.query and not args.url:
        print("rag needs a query or --url", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
