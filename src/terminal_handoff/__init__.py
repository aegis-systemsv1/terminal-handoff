"""Terminal Handoff.

Hand a long-running Claude Code session over to a fresh successor session in a
new macOS Terminal window, preserving the exact model, the exact effort level
and the verified working state.

The complete implementation lives in :mod:`terminal_handoff.core`. The sibling
modules are thin, explicit facades that name the public API surface by concern;
see docs/ARCHITECTURE.md for why the implementation is not mechanically split.
"""

from terminal_handoff.core import (  # noqa: F401
    TERMINAL_HANDOFF_VERSION,
    MANIFEST_SCHEMA_VERSION,
    ALLOWED_EFFORT_LEVELS,
    main,
)

__version__ = TERMINAL_HANDOFF_VERSION
__all__ = [
    "TERMINAL_HANDOFF_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "ALLOWED_EFFORT_LEVELS",
    "main",
]
