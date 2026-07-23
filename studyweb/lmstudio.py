"""Drive a local LM Studio model through the web tools via OpenAI-style tool
calling. This is the "plug the tool into the LMS (LM Studio) model" piece.

    from studyweb.lmstudio import run_agent
    out = run_agent("Compare Galaxy Tab S11 prices on Samsung and Danawa",
                    model="gemma-4-e4b-uncensored-hauhaucs-aggressive")
    print(out["final"])
"""

from __future__ import annotations

import json
import time

import requests

from .lms import TOOL_SCHEMAS, dispatch_tool

DEFAULT_BASE = "http://localhost:1234/v1"

SYSTEM_PROMPT = (
    "You are a research assistant with live web tools. When the user asks for "
    "facts, data, or prices, you MUST use the tools to look them up rather than "
    "guessing. Cite the source URL for any figure you report. Answer directly "
    "and concisely; do not narrate your reasoning."
)


def _chat(messages, model, base_url, temperature, tools=None, timeout=180):
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "stream": False}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    r = requests.post(f"{base_url}/chat/completions", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def run_agent(user_msg: str, *, model: str, base_url: str = DEFAULT_BASE,
              temperature: float = 0.0, max_steps: int = 6,
              system: str = SYSTEM_PROMPT, verbose: bool = True) -> dict:
    """Run an agentic tool-calling loop and return {final, steps, trace}."""
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user_msg}]
    trace = []
    for step in range(max_steps):
        msg = _chat(messages, model, base_url, temperature, tools=TOOL_SCHEMAS)
        # Normalise the assistant turn we append back.
        assistant = {"role": "assistant", "content": msg.get("content") or ""}
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        messages.append(assistant)

        if not tool_calls:
            if verbose:
                print(f"[step {step}] final answer\n")
            return {"final": msg.get("content", ""), "steps": step, "trace": trace}

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if verbose:
                print(f"[step {step}] tool -> {name}({json.dumps(args, ensure_ascii=False)})")
            t0 = time.time()
            result = dispatch_tool(name, args)
            trace.append({"tool": name, "args": args, "secs": round(time.time() - t0, 2)})
            messages.append({"role": "tool", "tool_call_id": tc.get("id", name),
                             "name": name, "content": json.dumps(result, ensure_ascii=False)})
    # Ran out of steps: ask once more without tools for a final synthesis.
    msg = _chat(messages, model, base_url, temperature)
    return {"final": msg.get("content", ""), "steps": max_steps, "trace": trace}
