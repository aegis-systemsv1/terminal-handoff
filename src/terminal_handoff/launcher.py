"""Successor launch: argv construction, shell script generation, osascript.

The successor is always an ordinary new interactive session. The launch argv
never contains --continue, -c, --resume, -r, --fork-session, a permission
bypass, a fallback model or the parent's session ID.
"""

from terminal_handoff.core import (  # noqa: F401
    FORBIDDEN_LAUNCH_TOKENS,
    MAX_LAUNCH_RETRIES,
    applescript_quote,
    assert_launch_argv_safe,
    bootstrap_prompt,
    build_launch_argv,
    build_launch_script,
    fail_launch,
    find_claude_executable,
    launch_terminal,
    perform_launch,
    render_successor_prompt,
    spawn_launcher,
    successor_prompt_template_path,
    successor_session_name,
)

__all__ = [
    "FORBIDDEN_LAUNCH_TOKENS",
    "MAX_LAUNCH_RETRIES",
    "applescript_quote",
    "assert_launch_argv_safe",
    "bootstrap_prompt",
    "build_launch_argv",
    "build_launch_script",
    "fail_launch",
    "find_claude_executable",
    "launch_terminal",
    "perform_launch",
    "render_successor_prompt",
    "spawn_launcher",
    "successor_prompt_template_path",
    "successor_session_name",
]
