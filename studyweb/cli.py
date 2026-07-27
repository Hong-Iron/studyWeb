"""Command-line interface.

    python -m studyweb search "photosynthesis" -n 5
    python -m studyweb fetch https://en.wikipedia.org/wiki/Photosynthesis --markdown
    python -m studyweb answer "how does CRISPR work"          # Tavily-style
    python -m studyweb rag "quantum computing basics" --out ./data --crawl-depth 1
    python -m studyweb serve --port 8787

Models — local by default, cloud when you point it there:

    python -m studyweb providers                    # who's connected right now
    python -m studyweb ask "GPU prices on danawa" --provider anthropic
    python -m studyweb usage                        # tokens + cost so far
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import settings
from .search import search, SearchError
from .providers import ProviderError
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


def cmd_prices(a) -> int:
    from .prices import find_prices
    sites = [s for s in (a.sites or "").split(",") if s.strip()] or None
    out = find_prices(a.query, sites=sites, per_site=a.per_site)
    if a.json:
        _print_json(out)
        return 0

    for q in out["quotes"]:
        price = f"{q['price']:,}원" if q["price"] is not None else "가격 없음"
        print(f"{price:>14}  {q['site']:<22} {q['title'][:52]}")
        print(f"{'':>14}  {q['url']}")
    s = out["summary"]
    if s:
        print(f"\n{s['count']}건 · 최저 {s['min']:,}원 · 중앙값 {s['median']:,}원 "
              f"· 최고 {s['max']:,}원  ({out['response_time']}s)")
    else:
        print("가격을 찾지 못했습니다.", file=sys.stderr)
    for m in out["misses"]:
        print(f"  - {m['site']}: {m['reason']}", file=sys.stderr)
    return 0 if s else 1


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


def cmd_extract_data(a) -> int:
    from .dataextract import extract_data
    res = extract_data(a.url, schema=a.field or None,
                       render_mode=a.render, use_llm=not a.no_llm,
                       llm_provider=a.provider)
    _print_json(res)
    return 0 if res.get("data") is not None else 1


# ---- model providers ------------------------------------------------------

_STATUS_MARK = {"ok": "✓", "no_key": "🔑", "no_model": "○", "unauthorized": "✗",
                "rate_limit": "⏳", "unreachable": "✗", "not_installed": "⬇",
                "error": "✗"}


def cmd_providers(a) -> int:
    """Show every provider and whether it is actually reachable right now."""
    from . import providers as P
    rows = ([P.check(a.provider)] if a.provider
            else P.check_all(only_configured=a.configured))
    if a.json:
        _print_json({"providers": rows, "default": settings.llm_provider})
        return 0
    print(f"{'':2} {'PROVIDER':<14} {'STATUS':<14} {'MODEL':<34} DETAIL")
    for r in rows:
        mark = _STATUS_MARK.get(r["status"], "?")
        star = "*" if r["provider"] == settings.llm_provider else " "
        model = (r.get("model") or "—")[:33]
        detail = r.get("detail", "")
        if r.get("latency_ms"):
            detail = f"{detail} ({r['latency_ms']}ms)"
        print(f"{star}{mark} {r['provider']:<14} {r['status']:<14} {model:<34} {detail}")
    print("\n* = default provider (STUDYWEB_PROVIDER_LLM). "
          "Keys come from each provider's env var.")
    return 0 if all(r["status"] in ("ok", "no_key") for r in rows) else 1


def _fmt_cost(bucket: dict) -> str:
    cost = bucket.get("cost_usd", 0.0)
    unpriced = bucket.get("unpriced_requests", 0)
    s = f"${cost:.4f}" if cost else "$0"
    return f"{s} (+{unpriced} unpriced)" if unpriced else s


def cmd_usage(a) -> int:
    """What the models have cost so far — this session, today, all time."""
    from .usage import ledger
    s = ledger.summary(days=a.days)
    if a.json:
        _print_json(s)
        return 0
    for name in ("session", "today", "total"):
        b = s[name]
        print(f"{name.upper():<8} {b['requests']:>5} calls  "
              f"{b['total_tokens']:>9,} tokens  "
              f"({b['prompt_tokens']:,} in / {b['completion_tokens']:,} out)  "
              f"{_fmt_cost(b)}")
    if s["providers"]:
        print("\nBY PROVIDER")
        for pid, b in sorted(s["providers"].items()):
            print(f"  {pid:<14} {b['requests']:>5} calls  {b['total_tokens']:>9,} tok  {_fmt_cost(b)}")
    if s["days"]:
        print(f"\nLAST {len(s['days'])} DAYS")
        for d in s["days"]:
            print(f"  {d['day']}  {d['requests']:>5} calls  {d['total_tokens']:>9,} tok  {_fmt_cost(d)}")
    if s.get("storage"):
        print(f"\nledger: {s['storage']}")
    return 0


def cmd_usage_reset(a) -> int:
    from .usage import ledger
    ledger.reset("all" if a.all else "session")
    print("usage reset:", "all history" if a.all else "this session")
    return 0


def cmd_pricing(a) -> int:
    from .usage import load_pricing, save_pricing, pricing_path
    table = load_pricing(refresh=True)
    if a.write:
        print(f"wrote {save_pricing(table)} — edit it to correct any price")
        return 0
    if a.json:
        _print_json(table)
        return 0
    print(f"USD per 1M tokens  (overrides: {pricing_path()})")
    for provider, models in sorted(table.items()):
        if not models:
            print(f"  {provider}: (unpriced — token counts only)")
            continue
        for model, p in sorted(models.items()):
            print(f"  {provider:<12} {model:<32} in ${p.get('in', 0):<8} out ${p.get('out', 0)}")
    return 0


def cmd_ask(a) -> int:
    """Ask a model — local or cloud — with the web tools attached."""
    from .agent import run_agent
    res = run_agent(a.query, provider=a.provider, model=a.model,
                    max_steps=a.max_steps, verbose=a.verbose)
    if a.json:
        _print_json(res)
        return 0 if not res.get("error") else 1
    if res.get("error"):
        print(f"error [{res['error'].get('kind')}]: {res['error'].get('error')}", file=sys.stderr)
        return 1
    print(res["final"])
    u = res["usage"]
    cost = f" · ${u['cost_usd']:.4f}" if u.get("cost_usd") else ""
    print(f"\n— {res['provider']}/{res['model']} · {u['requests']} calls · "
          f"{u['prompt_tokens']:,} in / {u['completion_tokens']:,} out"
          f" = {u['total_tokens']:,} tokens{cost} · {u['latency_ms']/1000:.1f}s",
          file=sys.stderr)
    return 0


def cmd_serve(a) -> int:
    serve(host=a.host, port=a.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    from .providers import PROVIDERS
    pids = "|".join(PROVIDERS)
    p = argparse.ArgumentParser("studyweb", description="Local web search/crawl for study & RAG.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="web search")
    s.add_argument("query"); s.add_argument("-n", type=int, default=settings.default_max_results)
    s.add_argument("--provider", default=None)
    s.add_argument("--site", default=None, help="search within one site directly (no Bing), e.g. danawa.com")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    pr = sub.add_parser("prices", help="find a price across a fixed list of sites")
    pr.add_argument("query")
    pr.add_argument("--sites", default=None,
                    help=f"comma-separated domains (default: {','.join(settings.price_sites)})")
    pr.add_argument("--per-site", type=int, default=3, dest="per_site")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_prices)

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

    ed = sub.add_parser("extract-data", help="extract structured data from a URL (JSON-LD/microdata + LLM, headless fallback)")
    ed.add_argument("url")
    ed.add_argument("--field", action="append", help="field to extract, repeatable (e.g. --field name --field price)")
    ed.add_argument("--render", choices=["auto", "always", "never"], default="auto")
    ed.add_argument("--no-llm", action="store_true", help="structured markup only, skip the LLM fallback")
    ed.add_argument("--provider", default=None, help=f"model provider ({pids})")
    ed.set_defaults(func=cmd_extract_data)

    pv = sub.add_parser("providers", help="model providers and their connection status")
    pv.add_argument("--provider", default=None, help="check just one")
    pv.add_argument("--configured", action="store_true",
                    help="skip providers with no API key instead of dialling them")
    pv.add_argument("--json", action="store_true")
    pv.set_defaults(func=cmd_providers)

    us = sub.add_parser("usage", help="token/cost usage for local and cloud models")
    us.add_argument("--days", type=int, default=7)
    us.add_argument("--json", action="store_true")
    us.set_defaults(func=cmd_usage)

    ur = sub.add_parser("usage-reset", help="clear usage counters")
    ur.add_argument("--all", action="store_true", help="wipe stored history, not just this session")
    ur.set_defaults(func=cmd_usage_reset)

    pr = sub.add_parser("pricing", help="show or export the per-model price table")
    pr.add_argument("--write", action="store_true",
                    help="write the table to ~/.config/studyweb/pricing.json for editing")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_pricing)

    ak = sub.add_parser("ask", help="ask a model (local or cloud) with the web tools attached")
    ak.add_argument("query")
    ak.add_argument("--provider", default=None, help=f"one of {pids}")
    ak.add_argument("--model", default=None)
    ak.add_argument("--max-steps", type=int, default=6, dest="max_steps")
    ak.add_argument("-v", "--verbose", action="store_true", help="print each tool call")
    ak.add_argument("--json", action="store_true")
    ak.set_defaults(func=cmd_ask)

    sv = sub.add_parser("serve", help="run the HTTP API")
    sv.add_argument("--host", default="127.0.0.1"); sv.add_argument("--port", type=int, default=8787)
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "rag" and not args.query and not args.url:
        print("rag needs a query or --url", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except (SearchError, ProviderError) as exc:
        # A missing key or a dead backend is a configuration problem, not a
        # crash — say so on one line instead of unrolling a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
