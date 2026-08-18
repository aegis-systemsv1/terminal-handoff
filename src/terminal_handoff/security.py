"""Validation and quoting boundaries.

Every field of the status-line JSON is untrusted input. Model IDs are
allow-listed (brackets permitted so that IDs such as `claude-opus-5[1m]`
survive intact) and passed as separate argv elements, never as shell text.
Effort levels are allow-listed. Paths bearing quotes, backslashes or control
characters are rejected outright. The transcript is validated and then
referenced by path only: never executed, interpolated, argv-passed or logged.
"""

from terminal_handoff.core import (  # noqa: F401
    ALLOWED_EFFORT_LEVELS,
    CHAIN_ID_RE,
    MODEL_ID_RE,
    SESSION_ID_RE,
    UNDETECTABLE_EFFORT_ALIASES,
    UNSAFE_PATH_RE,
    applescript_quote,
    assert_launch_argv_safe,
    is_terminal_handoff_command,
    validate_transcript,
    write_json_private,
    write_private,
)

__all__ = [
    "ALLOWED_EFFORT_LEVELS",
    "CHAIN_ID_RE",
    "MODEL_ID_RE",
    "SESSION_ID_RE",
    "UNDETECTABLE_EFFORT_ALIASES",
    "UNSAFE_PATH_RE",
    "applescript_quote",
    "assert_launch_argv_safe",
    "is_terminal_handoff_command",
    "validate_transcript",
    "write_json_private",
    "write_private",
]
