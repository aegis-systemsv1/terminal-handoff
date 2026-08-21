"""Transfer of continuation ownership from a parent session to its successor.

There is exactly one ownership boundary. Before a verified successor heartbeat
the parent owns continuation; after the shutdown request the successor does.
The parent Claude process is bound by tracing this process's real ancestry at
trigger time, re-proved immediately before it is signalled, and asked to exit
with a single SIGTERM. There is no SIGKILL path, no process-name matching, no
process-group signalling and no escalation: if anything cannot be proved, the
parent is left fully operational and the transfer is recorded as failed.
"""

from terminal_handoff.core import (  # noqa: F401
    PARENT_PROCESS_NAMES,
    PARENT_STOP_SIGNAL,
    PARENT_STOP_SIGNAL_NAME,
    TRANSFER_COMPLETE,
    TRANSFER_FAILED,
    TRANSFER_LAUNCHING,
    TRANSFER_OWNER,
    TRANSFER_PARENT_STOP_REQUESTED,
    TRANSFER_STATES,
    TRANSFER_SUCCESSOR_VERIFIED,
    TRANSFER_TRANSITIONS,
    SUCCESSOR_CHECK_NAMES,
    bind_parent_claude_process,
    build_transfer_record,
    claim_supervisor,
    evaluate_successor_checks,
    heartbeat_timeout,
    parent_binding_path,
    process_ancestry,
    process_cwd,
    process_identity,
    read_transfer,
    request_parent_stop,
    spawn_supervisor,
    stop_attempts,
    stop_grace_seconds,
    stop_parent_enabled,
    supervise_transfer,
    transfer_path,
    transfer_transition,
    update_transfer_fields,
    verify_parent_binding,
)

__all__ = [
    "PARENT_PROCESS_NAMES",
    "PARENT_STOP_SIGNAL",
    "PARENT_STOP_SIGNAL_NAME",
    "TRANSFER_COMPLETE",
    "TRANSFER_FAILED",
    "TRANSFER_LAUNCHING",
    "TRANSFER_OWNER",
    "TRANSFER_PARENT_STOP_REQUESTED",
    "TRANSFER_STATES",
    "TRANSFER_SUCCESSOR_VERIFIED",
    "TRANSFER_TRANSITIONS",
    "SUCCESSOR_CHECK_NAMES",
    "bind_parent_claude_process",
    "build_transfer_record",
    "claim_supervisor",
    "evaluate_successor_checks",
    "heartbeat_timeout",
    "parent_binding_path",
    "process_ancestry",
    "process_cwd",
    "process_identity",
    "read_transfer",
    "request_parent_stop",
    "spawn_supervisor",
    "stop_attempts",
    "stop_grace_seconds",
    "stop_parent_enabled",
    "supervise_transfer",
    "transfer_path",
    "transfer_transition",
    "update_transfer_fields",
    "verify_parent_binding",
]
