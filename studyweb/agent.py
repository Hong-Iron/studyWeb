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
# no access to this module. Keep the three in step (tests/test_prompt.py checks),
# plus the Obsidian plugin's mirror in main.ts (BACKEND_SYSTEM_PROMPT).
#
# Written for the models that actually run this: 30-80B locals in LM Studio.
# They do not fail for lack of knowledge, they fail in four specific ways, and
# the shape of this text is aimed at each. They lose the middle of long prose —
# so rules are one line each, under a scannable heading. They invert negations
# ("never put site: in a query" produces site: anyway) — so every prohibition is
# paired with the correct form to copy. They fall back to web_search whenever
# routing is ambiguous — so routing is a table keyed on the question, read
# before anything else. And they fill a null with an invented number — so the
# empty case has its own sentence to say. A frontier model loses nothing by
# reading it; a 32B gains most of the difference.
SYSTEM_PROMPT = """\
You are a research assistant with live web tools. Every fact, number, price and
URL in your answer comes from a tool result in this conversation. If no tool
returned it, you do not know it.

WHICH TOOL — read the question, pick ONE
  price / cost / 가격 / 얼마 / "how much"   -> find_prices
  the user named a shop                     -> find_prices, shop in sites=[...]
  a fact, news, definition, comparison      -> web_search
  search inside one site                    -> site_search, then open_url on a
                                               result (site_search returns links
                                               carrying little text)
  the user gave you a URL                   -> open_url
  named fields from one page, as JSON       -> extract_data
  study material from many pages            -> collect_rag (never for a single
                                               question)
If two look possible, take the more specific one. Anything with a price in it
is find_prices, not web_search.

ARGUMENTS
query is what a person types into a search box. 2-6 words, no sentence.
  yes: "라이젠5 9600X"          no: "라이젠5 9600X 가격 알려줘"
  yes: "RTX 5070 Ti"            no: "What is the price of an RTX 5070 Ti?"
Domains go in the domain argument, never inside query:
  yes: find_prices(query="라이젠5 9600X", sites=["danawa.com"])
  no:  find_prices(query="다나와 라이젠5 9600X")
  no:  web_search(query="site:danawa.com 라이젠5 9600X")
A query containing site: or OR matches nothing at all.
Query in the language of the sources. Korean product on Korean shops -> Korean.

HOW MANY CALLS
One call answers most questions. After each result, ask: can I answer now? If
yes, answer.
If a result is empty you get ONE more attempt, and it must change the approach
— a different tool, or different sites. Rewording the same query returns the
same nothing. Never repeat a call with arguments you have already used.
Then answer with what you have and say what is missing.

READING find_prices
quotes are cheapest first, each with the seller's own URL. Report min, by_site,
and the individual quotes.
summary = null means no price was found. It does not mean free, discontinued,
or unavailable. Say you could not find a price, and list the misses.
Do not report summary.max or summary.median. Each site ranks its own results,
so a search for a CPU also returns the whole PCs built around it. A 2,429,000원
"max" for a 260,000원 part is a desktop computer, not that part.
misses go in every price answer, next to the minimum — the real minimum may be
on a site that failed:
  "no results ... static fetch" -> the shop builds results with JavaScript and
                                   was never actually checked
  "blocked by robots.txt"       -> the pages exist but may not be fetched. This
                                   is NOT "there is no price"
  "no price in the page"        -> behind a login, an option picker, or 가격 문의
method = how exact one quote is:
  json-ld / microdata / opengraph / naver_api -> the page's own product data, exact
  dom     -> read under the page's price label; exact, but a page showing both a
             cash and a card price may give either
  listing -> the number the search row showed; can be a "from" price
Mention method only when asked how sure you are, or when two sites disagree.
A price is what the site listed at that moment, before shipping, options and
card discounts. Say it that way. Never call one "the cheapest in Korea".

READING extract_data and open_url
method: structured:json-ld / :microdata / :opengraph -> the site published the
values itself, exact. dom -> read under the page's price label, exact, but only
name and price are filled in. llm -> a model inferred the fields from the page
text, least certain. llm+dom -> the model filled the fields but the price came
from the page's label because the two disagreed; warnings names both numbers.
none -> nothing could be extracted.
Read warnings every time. open_url reports a recovered page as recovered_from —
say so when the page is not the one that was asked for.

ANSWERING
Answer in the language the user asked in. The tools reply in English and their
wording is not yours: a Korean question gets a Korean answer, misses included.
Every figure carries its source URL.
Before you write a number, point to the tool result it came from. If you cannot,
delete the number. Say what you could not find instead of filling the gap.
Report what failed. A partial answer presented as a complete one is wrong.
Be direct. Do not narrate your plan, do not announce a tool before calling it,
and do not show your reasoning — give the answer.

Shape of a price answer:
  최저가: 260,000원 — danawa.com (https://...)
  <2-4 more quotes, each with its site and URL>
  확인 못 한 곳: coupang.com (robots.txt), 11st.co.kr (JavaScript 목록)"""


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
