"""Threshold detection against Claude Code's official status-line JSON.

The trigger decision reads only `.context_window.used_percentage`. It never
estimates usage from transcript size, line count, elapsed time, message count,
token guesses or session cost, and it never reads the `.rate_limits.*`
percentages, which measure account limits rather than context usage.
"""

from terminal_handoff.core import (  # noqa: F401
    BADGES,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_THRESHOLD,
    Facts,
    badge_for,
    decide,
    extract_facts,
    max_generations,
    threshold,
)

__all__ = [
    "BADGES",
    "DEFAULT_MIN_OBSERVATIONS",
    "DEFAULT_THRESHOLD",
    "Facts",
    "badge_for",
    "decide",
    "extract_facts",
    "max_generations",
    "threshold",
]
