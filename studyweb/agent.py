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

SYSTEM_PROMPT = (
    "You are a research assistant with live web tools. When the user asks for "
    "facts, data, or prices, you MUST use the tools to look them up rather than "
    "guessing. Use find_prices for 'how much does X cost' questions. Never put "
    "site: or OR operators in a query — the tools take domains as an argument. "
    "Cite the source URL for any figure you report, and when a tool reports "
    "sources it could not read, say so rather than presenting a partial answer "
    "as a complete one. Answer directly and concisely; do not narrate your "
    "reasoning."
)


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
