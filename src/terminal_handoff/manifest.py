"""Handoff manifest construction and lifecycle updates.

A manifest records the outgoing session's identity, exact model, exact effort,
context percentage at trigger, chain and generation, and a snapshot of the
repository state. It never records credentials, tokens, environment dumps, file
contents or transcript contents.
"""

from terminal_handoff.core import (  # noqa: F401
    MANIFEST_SCHEMA_VERSION,
    applicable_claude_md,
    build_manifest,
    capture_repo_state,
    manifest_path,
    update_manifest,
)

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "applicable_claude_md",
    "build_manifest",
    "capture_repo_state",
    "manifest_path",
    "update_manifest",
]
