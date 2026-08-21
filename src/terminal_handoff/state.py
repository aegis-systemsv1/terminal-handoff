"""On-disk state: one-shot claims, lifecycle, cooldown, circuit breaker, logs.

The one-shot trigger claim is keyed by session_id and made with
O_CREAT|O_EXCL, so concurrent status-line processes produce exactly one launch
and a successor never inherits its parent's claim.
"""

from terminal_handoff.core import (  # noqa: F401
    STATE_DIRS,
    chain_generation_for_session,
    chain_identity,
    circuit_is_open,
    claim_trigger,
    cooldown_remaining,
    ensure_dirs,
    log_event,
    observation_count,
    record_launch,
    record_chain_generation,
    record_observation,
    recent_launch_times,
    read_chain_state,
    rotate_logs,
    set_state,
    storm_reset_epoch,
    storm_settings,
    th_home,
    th_path,
    trip_circuit,
    update_json_locked,
)

__all__ = [
    "STATE_DIRS",
    "chain_generation_for_session",
    "chain_identity",
    "circuit_is_open",
    "claim_trigger",
    "cooldown_remaining",
    "ensure_dirs",
    "log_event",
    "observation_count",
    "record_launch",
    "record_chain_generation",
    "record_observation",
    "recent_launch_times",
    "read_chain_state",
    "rotate_logs",
    "set_state",
    "storm_reset_epoch",
    "storm_settings",
    "th_home",
    "th_path",
    "trip_circuit",
    "update_json_locked",
]
