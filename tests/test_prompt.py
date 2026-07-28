"""The system prompt: it must name the tools that exist, and the copy people
paste into a GUI must be the copy the agent actually runs with."""

import re
from pathlib import Path

from studyweb.agent import SYSTEM_PROMPT
from studyweb.lms import TOOL_SCHEMAS

DOC = Path(__file__).resolve().parent.parent / "docs" / "system-prompt.md"


def _pasted_block() -> str:
    """The first ```text block of docs/system-prompt.md — what a user copies."""
    m = re.search(r"```text\n(.*?)\n```", DOC.read_text(encoding="utf-8"), re.S)
    assert m, "docs/system-prompt.md has no ```text block to paste"
    return m.group(1)


def test_doc_matches_the_constant():
    assert _pasted_block() == SYSTEM_PROMPT, (
        "docs/system-prompt.md has drifted from studyweb.agent.SYSTEM_PROMPT — "
        "see the sync command at the bottom of that file")


def test_prompt_names_every_tool():
    for t in TOOL_SCHEMAS:
        assert t["function"]["name"] in SYSTEM_PROMPT


def test_prompt_carries_the_rules_that_cost_us_answers():
    # Each of these came from a real failure mode; dropping one silently
    # regresses the model's behaviour, so they are pinned here.
    for rule in ("site:",            # operators in a query return nothing
                 "robots.txt",       # blocked != no price
                 "misses",           # a minimum without its misses is dishonest
                 "summary.max",      # the ranking mixes parts and whole machines
                 "llm+dom",          # the label price won; warnings names both
                 "warnings",
                 "source URL"):
        assert rule in SYSTEM_PROMPT, f"the prompt no longer mentions {rule!r}"


def test_prompt_keeps_the_shape_a_30b_model_needs():
    """The layout is load-bearing for the models that actually run this.

    A local 30-80B does not fail for lack of knowledge; it loses the middle of
    long prose, inverts bare prohibitions, and falls back to web_search when
    routing is ambiguous. Each pin below is the countermeasure, not decoration.
    """
    # Routing is a table read before anything else, not prose to infer from.
    assert "WHICH TOOL" in SYSTEM_PROMPT
    # Every prohibition ships with the correct form to copy instead.
    assert 'sites=["danawa.com"]' in SYSTEM_PROMPT, (
        "the site: rule lost its positive example — a bare 'never' gets inverted")
    # The empty case has its own sentence, or the model invents a number.
    assert "summary = null" in SYSTEM_PROMPT
    # Without a stop rule these models reword the same query forever.
    assert "Never repeat a call" in SYSTEM_PROMPT
    # Reasoning models leak their thinking into the final answer.
    assert "do not show your reasoning" in SYSTEM_PROMPT
