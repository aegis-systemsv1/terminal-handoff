"""Human-facing successor session names and trusted chain metadata.

A Claude session named `Ranger` hands off to `Ranger 2`, which hands off to
`Ranger 3`. The base name is captured once, from the official status-line
JSON's `session_name`, and preserved as explicit chain metadata; the generation
number comes from that trusted chain state, never from parsing trailing digits
off a visible session name. The machine-safe chain identifier stays separate
and is never shown as a session name.
"""

from terminal_handoff.core import (  # noqa: F401
    DISPLAY_NAME_CONVENTION,
    DISPLAY_NAME_MAX,
    chain_generation_for_session,
    chain_state_path,
    fallback_base_name,
    generation_display_name,
    read_chain_state,
    record_chain_generation,
    resolve_base_display_name,
    sanitize_display_name,
    successor_session_name,
)

__all__ = [
    "DISPLAY_NAME_CONVENTION",
    "DISPLAY_NAME_MAX",
    "chain_generation_for_session",
    "chain_state_path",
    "fallback_base_name",
    "generation_display_name",
    "read_chain_state",
    "record_chain_generation",
    "resolve_base_display_name",
    "sanitize_display_name",
    "successor_session_name",
]
