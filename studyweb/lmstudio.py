"""Drive a local LM Studio model through the web tools.

This is now a thin wrapper over :mod:`studyweb.agent`, which runs the same
tool-calling loop against any provider (LM Studio, OpenAI, Claude, NVIDIA NIM).
Kept because "plug the tool into the LMS model" is the original entry point:

    from studyweb.lmstudio import run_agent
    out = run_agent("Compare Galaxy Tab S11 prices on Samsung and Danawa",
                    model="gemma-4-e4b-uncensored-hauhaucs-aggressive")
    print(out["final"])

For a cloud model, call :func:`studyweb.agent.run_agent` with ``provider=``.
"""

from __future__ import annotations

from .agent import SYSTEM_PROMPT, run_agent as _run_agent

DEFAULT_BASE = "http://localhost:1234/v1"

__all__ = ["run_agent", "SYSTEM_PROMPT", "DEFAULT_BASE"]


def run_agent(user_msg: str, *, model: str | None = None,
              base_url: str = DEFAULT_BASE, temperature: float = 0.0,
              max_steps: int = 6, system: str = SYSTEM_PROMPT,
              verbose: bool = True) -> dict:
    """Run the agentic tool-calling loop against LM Studio.

    Returns ``{final, steps, trace, usage, provider, model}`` — ``usage`` is new
    and reports tokens (and cost, for priced models) for the whole turn.
    """
    return _run_agent(user_msg, provider="lmstudio", model=model,
                      endpoint=base_url, temperature=temperature,
                      max_steps=max_steps, system=system, verbose=verbose)
