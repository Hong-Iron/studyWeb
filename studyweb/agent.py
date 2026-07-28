"""A provider-agnostic tool-calling loop.

The same agent that used to drive only a local LM Studio model now runs against
anything in :mod:`studyweb.providers` — LM Studio, OpenAI, Claude, NVIDIA NIM —
because every provider is normalised to the OpenAI message shape at that
boundary.

    from studyweb.agent import run_agent
    out = run_agent("Compare Galaxy Tab S11 prices on Samsung and Danawa",
                    provider="anthropic")
    print(out["final"])
    print(out["usage"]["total_tokens"], out["usage"]["cost_usd"])

Every turn's token/cost usage is summed into ``out["usage"]`` and recorded in
the ledger, so the caller can show what the answer cost.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from .lms import TOOL_SCHEMAS, dispatch_tool
from .providers import PROVIDERS, ProviderError, chat, resolve
from .usage import Usage

# The operating manual the model gets before it sees a question: which tool
# answers what, how to phrase the arguments, and — the part it cannot infer from
# the schemas — what the results actually mean. docs/llm-guide.md is the long
# form; docs/system-prompt.md is this same text, for pasting into a GUI that has
# no access to this module. Keep the three in step (tests/test_prompt.py checks).
SYSTEM_PROMPT = """\
You are a research assistant with live web tools. Every fact you report comes \
from a tool call, never from memory.

TOOLS
- find_prices(query, sites?, per_site?) — what a product costs, on several \
shopping sites at once. Use it for ANY "how much does X cost" question.
- web_search(query, max_results?, include_domains?) — current facts, news, \
explanations. Returns sources plus a locally-extracted answer.
- site_search(site, query, max_results?) — searches one site through its own \
search page. Returns links carrying little text; follow up with open_url or \
extract_data.
- open_url(url) — the cleaned main text of a page whose URL you already have.
- extract_data(url, fields?) — specific fields from one page, as JSON.
- collect_rag(query, max_results?) — a corpus of cleaned chunks for study. Not \
for answering a single question.

CALLING THEM
- Write a query the way a person types it into a search box: "AMD 라이젠5 9600X", \
not a sentence.
- Never put site: or OR operators in a query — they return nothing. Domains are \
arguments: find_prices' sites, web_search's include_domains, site_search's site.
- When the user names a shop, it goes in find_prices' sites. Do not write the \
shop name into the query text, and do not fall back to web_search.
- Query in the language of the sources: Korean products on Korean sites, Korean.
- One call per question. If it comes back empty, change the approach, not the \
wording; escalate at most one step, then answer with what you have.

READING find_prices
- summary = null means no price was found. It does not mean the product is free \
or unavailable: say so, and list the misses.
- quotes are cheapest first and each carries the seller's own URL. Report min, \
by_site and the individual quotes you have read the titles of.
- Do not report summary.max or summary.median at all unless you have checked \
that every quote is the same product. They usually are not: quotes follow each \
site's own ranking, so a query for a CPU also returns the whole PCs built \
around it, and their prices land in max and median. A 2,429,000원 "max" for a \
260,000원 part is a desktop computer, not a price for the part.
- method says how exact a quote is. json-ld, microdata, opengraph and naver_api \
come from the page's own product markup and are exact. dom was read under the \
page's price label: exact, but it is the number the page displays, and a page \
showing both a cash and a card price may give either. listing is the price the \
search-results row showed, which can be a "from" price for a product with \
options. Mention method only when asked how sure you are, or when two sites \
disagree.
- Always report misses alongside a minimum — the real minimum may be on a site \
that failed. "no results … static fetch" means the shop builds its results with \
JavaScript and was never checked. "blocked by robots.txt" means the pages were \
found but may not be fetched: never report that as "there is no price". "no \
price in the page" means it sits behind a login, an option picker, or 가격 문의.
- Prices are what the site listed at that moment, before shipping, options and \
card discounts. Report them as such; do not call one "the cheapest in Korea".

READING extract_data
- method: structured:json-ld / :microdata / :opengraph — the site published the \
values itself, exact. dom — read under the page's own price label, exact, but \
only name and price are filled in. llm — a model inferred the fields from the \
page text, the least certain. llm+dom — a model filled the fields but the price \
came from the page's label because the two disagreed; read warnings, it names \
both numbers. none — nothing could be extracted.
- Always read warnings: a recovered URL, a missing browser or a price \
disagreement is reported there. open_url reports a recovered page as \
recovered_from — mention it when the page differs from the one asked for.

ANSWERING
- Never state a number a tool did not return. Say what you could not find \
instead of filling the gap.
- Every figure carries its source URL, so the user can check it.
- Report what failed. A partial answer presented as a complete one is wrong.
- Answer in the language the user asked in. The tools answer in English and \
their wording is not yours: a Korean question gets a Korean answer, misses \
included.
- Be direct and concise. Do not narrate your reasoning or announce the tool you \
are about to call."""


def run_agent(user_msg: str, *, provider: str | None = None,
              model: str | None = None, endpoint: str | None = None,
              temperature: float | None = None, max_steps: int = 6,
              system: str = SYSTEM_PROMPT, tools: list | None = None,
              verbose: bool = False,
              on_event: Callable[[dict], None] | None = None) -> dict:
    """Run the loop and return ``{final, steps, trace, usage, provider, model}``.

    ``on_event`` receives ``{"type": "tool"|"answer"|"error", ...}`` as things
    happen, for a UI that wants to show progress live.
    """
    p = resolve(provider)
    tool_schemas = TOOL_SCHEMAS if tools is None else tools
    if not p.supports_tools:
        # e.g. the Claude Code CLI, which brings its own tools and its own loop.
        tool_schemas = None

    messages: list[dict] = [{"role": "system", "content": system},
                            {"role": "user", "content": user_msg}]
    trace: list[dict] = []
    total = Usage(provider=p.id, model="", requests=0)

    def emit(event: dict) -> None:
        if verbose:
            print(f"[{event.get('type')}] {event.get('detail', '')}")
        if on_event:
            on_event(event)

    used_model = ""
    for step in range(max_steps):
        try:
            out = chat(messages, provider=p.id, model=model, tools=tool_schemas,
                       temperature=temperature, endpoint=endpoint,
                       label=f"agent[{step}]")
        except ProviderError as exc:
            emit({"type": "error", "detail": str(exc), **exc.to_dict()})
            return {"final": "", "steps": step, "trace": trace,
                    "usage": total.to_dict(), "provider": p.id, "model": used_model,
                    "error": exc.to_dict()}
        total = total + out["usage"]
        used_model = out["model"]
        msg = out["message"]

        assistant: dict = {"role": "assistant", "content": msg.get("content") or ""}
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        messages.append(assistant)

        if not tool_calls:
            emit({"type": "answer", "detail": f"final answer at step {step}"})
            return {"final": msg.get("content", ""), "steps": step, "trace": trace,
                    "usage": total.to_dict(), "provider": p.id, "model": used_model}

        for tc in tool_calls:
            name = (tc.get("function") or {}).get("name", "")
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                args = {}
            emit({"type": "tool", "name": name, "args": args,
                  "detail": f"{name}({json.dumps(args, ensure_ascii=False)})"})
            t0 = time.time()
            result = dispatch_tool(name, args)
            trace.append({"tool": name, "args": args,
                          "secs": round(time.time() - t0, 2),
                          "error": result.get("error")})
            messages.append({"role": "tool", "tool_call_id": tc.get("id") or name,
                             "name": name,
                             "content": json.dumps(result, ensure_ascii=False)})

    # Out of steps: one more turn without tools, to force a synthesis.
    try:
        out = chat(messages, provider=p.id, model=model, temperature=temperature,
                   endpoint=endpoint, label="agent[final]")
        total = total + out["usage"]
        final = out["message"].get("content", "")
    except ProviderError as exc:
        emit({"type": "error", "detail": str(exc)})
        return {"final": "", "steps": max_steps, "trace": trace,
                "usage": total.to_dict(), "provider": p.id, "model": used_model,
                "error": exc.to_dict()}
    return {"final": final, "steps": max_steps, "trace": trace,
            "usage": total.to_dict(), "provider": p.id, "model": out["model"]}


def available_providers() -> list[str]:
    return list(PROVIDERS)
