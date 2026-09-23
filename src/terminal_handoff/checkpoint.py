"""Deterministic, machine-verifiable session checkpoints (`th checkpoint`).

A checkpoint is a standalone snapshot: unlike a handoff manifest it never
launches a successor and never touches the transfer state machine. See
docs/ARCHITECTURE.md for why the implementation is not mechanically split -
the real logic lives in :mod:`terminal_handoff.core`, alongside the
`capture_repo_state()` and `redact_secrets()` it reuses, because the deployed
runtime is a single self-contained copy of `core.py`.

V2 Slice 2 adds an optional AI-generated session summary, extracted from a
transcript by a fully isolated `claude -p` worker (never the calling process),
always labelled PROVENANCE_AI_GENERATED and never treated as verified fact.
"""

from terminal_handoff.core import (  # noqa: F401
    AI_SUMMARY_MODEL_DEFAULT,
    AI_SUMMARY_TIMEOUT_SECONDS,
    CHECKPOINT_RECENT_COMMIT_LIMIT,
    CHECKPOINT_SCHEMA_VERSION,
    CHECKPOINT_TEST_OUTPUT_LIMIT,
    CHECKPOINT_TEST_TIMEOUT_SECONDS,
    CheckpointCaptureError,
    PROVENANCE_AI_GENERATED,
    PROVENANCE_MACHINE_VERIFIED,
    PROVENANCE_RECORDED_EVIDENCE,
    PROVENANCE_UNAVAILABLE,
    PROVENANCE_UNSUPPORTED,
    build_checkpoint,
    checkpoint_path,
    cmd_checkpoint,
    compute_checkpoint_integrity,
    verify_checkpoint_integrity,
)

__all__ = [
    "AI_SUMMARY_MODEL_DEFAULT",
    "AI_SUMMARY_TIMEOUT_SECONDS",
    "CHECKPOINT_RECENT_COMMIT_LIMIT",
    "CHECKPOINT_SCHEMA_VERSION",
    "CHECKPOINT_TEST_OUTPUT_LIMIT",
    "CHECKPOINT_TEST_TIMEOUT_SECONDS",
    "CheckpointCaptureError",
    "PROVENANCE_AI_GENERATED",
    "PROVENANCE_MACHINE_VERIFIED",
    "PROVENANCE_RECORDED_EVIDENCE",
    "PROVENANCE_UNAVAILABLE",
    "PROVENANCE_UNSUPPORTED",
    "build_checkpoint",
    "checkpoint_path",
    "cmd_checkpoint",
    "compute_checkpoint_integrity",
    "verify_checkpoint_integrity",
]
