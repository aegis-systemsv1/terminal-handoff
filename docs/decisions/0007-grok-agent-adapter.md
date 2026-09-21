# 7. Grok as a second agent, through an ACP bridge

Status: accepted

## Context

Terminal Handoff controlled one agent, Claude Code, and its design leans on Claude specifics: the status line
proves an owner, the agent pulls instructions by running CLI commands, and Claude's process is bound by name.
Grok exposes the Agent Client Protocol (`grok agent stdio`), a JSON-RPC interface with sessions, streamed
updates, permission requests and cancellation, which is a better integration surface than a terminal.

## Decision

* A logical session records `agent_type` (`claude` | `grok`). A record with none is Claude. The agent is
  fixed at creation; there is no mid-session switch.
* Grok is driven by a **bridge**: a detached Terminal Handoff process that is the ACP client and the session's
  sole writer. It reuses the existing single-use launch token, owner epoch fencing, durable inbox, STOP,
  approvals, transcript and archive unchanged. Its liveness is its own re-proved pid and start time.
* Delivery is push (`session/prompt` per claimed message). STOP is `session/cancel`, then the Grok process is
  ended if it does not stop. A Grok permission request becomes a Terminal Handoff approval answered with
  `allow_once` or `reject_once`.
* The exact Grok session id is stored once. Reconnect is `session/load` of that id, and it fails rather than
  start a new conversation. Replayed history is discarded.
* **Permissions fail closed.** Terminal Handoff cannot verify that Grok enforces ask mode, and Grok's user
  config can make every ACP session always-approve with no environment override. So Terminal Handoff refuses
  to start Grok while that config selects always-approve unless the user deliberately opts in for Terminal
  Handoff. It never passes `--always-approve` by default.
* No Terminal Handoff A to B context handoff for Grok: Grok's persisted sessions and compaction apply.
* The Claude code path is untouched: every Grok branch is guarded by `agent_type`, and the create path
  reaches Claude-specific code only after the agent has been resolved to Claude.

## Consequences

* Grok is opt-in per project and is not at parity with Claude (no Remote Control, no automatic handoff).
* An instruction in flight when Grok's process dies is offered again once (at-least-once). One interrupted by
  STOP is acknowledged, not re-run.
* Enforcement of ask mode depends on Grok. The refusal is deliberately conservative and may inconvenience a
  user whose Grok is set to always-approve for interactive use.
* Everything stays in one installed module (`core.py`), as before.
