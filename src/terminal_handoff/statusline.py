"""Status-line rendering, including wrapping a pre-existing status line.

When another status-line command is already configured, Terminal Handoff runs
it with the identical stdin bytes and preserves its stdout byte-for-byte,
appending only a short badge.
"""

from terminal_handoff.core import (  # noqa: F401
    WRAPPED_STATUSLINE_TIMEOUT,
    cmd_statusline,
    render_default_statusline,
    run_wrapped_statusline,
    successor_heartbeat,
)

__all__ = [
    "WRAPPED_STATUSLINE_TIMEOUT",
    "cmd_statusline",
    "render_default_statusline",
    "run_wrapped_statusline",
    "successor_heartbeat",
]
