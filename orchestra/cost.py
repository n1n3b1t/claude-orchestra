"""Per-million-token pricing + family-extraction regex.

Source: Anthropic public list (snapshot 2026-05-20). Tune if it bites.
"""
from __future__ import annotations

import re
from typing import Final

RATES: Final[dict[str, dict[str, float]]] = {
    "opus":   {"in": 15.0, "out": 75.0},
    "sonnet": {"in":  3.0, "out": 15.0},
    "haiku":  {"in":  1.0, "out":  5.0},
}

FAMILY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[-_/])(opus|sonnet|haiku)(?:$|[-\[_/])",
    re.IGNORECASE,
)


def family_of(model_id: str | None) -> str:
    """Return one of {'opus','sonnet','haiku'}. Unknown → 'opus' (conservative)."""
    if model_id:
        m = FAMILY_RE.search(model_id.lower())
        if m:
            return m.group(1).lower()
    return "opus"


def cost_for(model_id: str | None, input_tokens: int, output_tokens: int) -> float:
    """USD cost for a single turn."""
    rate = RATES[family_of(model_id)]
    return (input_tokens / 1_000_000.0) * rate["in"] + (output_tokens / 1_000_000.0) * rate["out"]


def _fmt(n: int) -> str:
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n // 1_000}k"
    if n < 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n // 1_000_000}M"


def format_tokens(input_tokens: int, output_tokens: int, cache_read_tokens: int) -> str:
    """Return a compact token summary like '42k/8k cache=180k'."""
    return f"{_fmt(input_tokens)}/{_fmt(output_tokens)} cache={_fmt(cache_read_tokens)}"
