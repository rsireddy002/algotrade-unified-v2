"""
execution/llm_gatekeeper.py — Claude API sanity-check before confirming a signal.

Sits between signal detection (strategies/, signals/) and execution
(execution/paper_trader.py) — a last check for anything that looks like a
data or logic problem (a nonsensical price, a stop on the wrong side of
entry, an obviously duplicated/stale signal) before a signal gets acted
on. This is a sanity check, not a trading-decision AI — it doesn't decide
whether a trade is a good idea, only whether the signal itself looks
internally consistent and well-formed.

Requires ANTHROPIC_API_KEY in the environment. The plumbing here is
tested with a mocked API response (see the module's test suite) — the
actual live call to the Claude API is NOT tested in this build, since no
API key was available in the sandbox this was built in. Run it once
against your real key before trusting it in a live pipeline.
"""

import json
import os
from dataclasses import dataclass

import anthropic

MODEL = "claude-sonnet-5"

SANITY_CHECK_PROMPT = """You are a sanity-checking gate for an automated trading signal, not a trading advisor. Your only job is to flag internal inconsistencies or obviously malformed data in the signal below — NOT to judge whether the trade is a good idea.

Signal:
{signal_json}

Check specifically for:
- A stop price on the wrong side of the entry price for the given direction (e.g. a short with a stop BELOW entry)
- Prices, percentages, or volumes that are zero, negative, or absurdly large in a way that suggests a data error
- A signal_time that looks stale, malformed, or outside plausible market hours
- Any field that's missing or clearly the wrong type

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"approved": true or false, "reasoning": "one sentence explaining your decision"}}
"""


@dataclass
class GatekeeperResult:
    approved: bool
    reasoning: str


def check_signal(signal_dict, client=None):
    """Send a signal (as a plain dict) to Claude for a sanity check.
    Returns a GatekeeperResult. Raises if the API call fails or returns a
    response that can't be parsed as the expected JSON shape — a
    gatekeeper that fails open (silently approving on error) defeats its
    own purpose, so callers should catch exceptions and decide explicitly
    what to do (e.g. treat an error as "don't trade" rather than "trade
    anyway")."""
    if client is None:
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    prompt = SANITY_CHECK_PROMPT.format(signal_json=json.dumps(signal_dict, default=str, indent=2))

    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    # Don't assume content[0] is a text block — a response can include
    # other block types (e.g. a ThinkingBlock) first. Find the actual text
    # block explicitly. This was a real bug caught by live testing: on the
    # first live run, content[0] was a ThinkingBlock with no .text
    # attribute, and blindly indexing into it raised an AttributeError.
    text_block = next((block for block in response.content if hasattr(block, "text")), None)
    if text_block is None:
        raise ValueError(f"No text block found in Claude's response: {response.content}")
    text = text_block.text.strip()
    # Models occasionally wrap JSON in a code fence despite instructions —
    # strip that defensively rather than fail on a cosmetic formatting slip.
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()

    parsed = json.loads(text)
    return GatekeeperResult(approved=bool(parsed["approved"]), reasoning=str(parsed["reasoning"]))
