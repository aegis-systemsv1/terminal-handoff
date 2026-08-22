# 1. Stop the parent session, and name successors after it

- **Status:** accepted
- **Date:** 2026-08-22
- **Release:** 1.1.0
- **Supersedes:** the 1.0.x statement "The original Terminal is never closed"

> Ownership timing and supervisor recovery are amended by
> [decision 2](0002-exclusive-ownership-and-notification-outbox.md):
> `PARENT_STOP_REQUESTED` is now ownerless, and only confirmed
> `TRANSFER_COMPLETE` gives ownership to the successor.

## Context

Two defects were reported from live use of 1.0.1.

**Naming.** A session named `Ranger` produced a successor named
`terminal-handoff-7a282bd6-g2`. That string is an internal chain identifier. It
tells a human nothing, it is unreadable in a Terminal tab, and it discards the
name the user chose. Chains of three or four generations became impossible to
tell apart at a glance.

**Two live agents.** After a successor was launched, the parent Claude session
kept running. Both sessions were pointed at the same repository, both had
authority to change it, and the successor's instructions told it to continue the
work. The failure mode is concurrent edits by two agents that cannot see each
other — worse than the full context window the handoff exists to solve.

## Decision

### Naming

The human-facing name is the chain's base name plus the generation number:
`Ranger`, `Ranger 2`, `Ranger 3`. Generation 1 is never renamed.

- The base name is captured **once**, at generation 1, from `.session_name` in
  the official status-line JSON, and written to `chains/<chain-id>.json` as
  explicit chain metadata. It is not rewritten by later generations, so renaming
  a successor mid-chain does not change the chain's identity.
- The generation number comes from that trusted chain state, which records which
  session ID occupies which generation. It is **never** derived by parsing
  trailing digits off a visible name. `Project 42` therefore hands off to
  `Project 42 2`, not `Project 43`.
- The `chain_id` remains a machine-safe hex identifier for state keying and is
  never shown as a session name.
- When no session name is available, the fallback is
  `Terminal Handoff <chain-id[:8]>`. A repository, directory or project name is
  never substituted: inventing an identity is worse than an obviously synthetic
  one.

### Parent shutdown

After a successor is launched, ownership of the work transfers exactly once,
through an auditable state machine: `LAUNCHING` -> `SUCCESSOR_VERIFIED` ->
`PARENT_STOP_REQUESTED` -> `TRANSFER_COMPLETE`, with `TRANSFER_FAILED`
reachable from any non-terminal state.

The parent Claude process is asked to exit only after the successor has proved,
from its own live status-line JSON across two heartbeats, that it has a fresh
and unused session ID, the required model, the required effort, the required
working directory, the correct chain and the correct generation.

## Alternatives considered

**Leave both sessions running and tell the successor to be careful.** Rejected:
the successor cannot see the parent's in-flight edits, and "be careful" is not a
guarantee. This is what 1.0.x did, and it is the defect.

**Close the Terminal window.** Rejected: the window is the user's, may hold
scrollback they want, and closing it is not reversible. Stopping the *session*
and leaving the *window* at a shell prompt gives the same isolation without
taking anything away.

**Kill by process name (`pkill claude`).** Rejected outright. A developer may
have a dozen Claude sessions open. Name matching cannot distinguish them, and
the blast radius of getting it wrong is every session on the machine.

**Use the status-line process's immediate parent.** Rejected: it is a shell.
Claude Code runs the status-line command through `/bin/sh -c`, so the real
ancestry must be traced rather than assumed.

**Escalate to `SIGKILL` if `SIGTERM` is ignored.** Rejected. A Claude session
that has not exited may be mid-write. An un-exited parent is recorded as
`parent_stop_unconfirmed` and left alone; the user decides.

**Derive the generation by parsing the visible name.** Rejected. It breaks on
every legitimate name containing a number, and it makes a security-relevant
decision from a field a user can set to anything.

## Consequences

**Good.** One agent continues the work. Sessions are identifiable at a glance.
The transfer is auditable after the fact, in `transfers/<parent-session-id>.json`
and the log.

**Accepted costs.**

- The parent is only stopped when its process can be proved, so on unusual host
  topologies two sessions may coexist. The successor is told not to mutate
  anything until the transfer state authorises it, and the failure is visible in
  `status`.
- A chain's base name is immutable after generation 1. That is deliberate, and
  documented.
- A parent that ignores `SIGTERM` is left running. That is deliberate, and
  documented.
- The manifest schema moves to version 2.

## Verification

Automated: 82 tests across `tests/test_naming.py`, `tests/test_transfer.py` and
`tests/test_parent_stop.py`, including real macOS processes, real ancestry
traversal, real signals, and a static scan proving no `pkill`, `killall`,
process-group signalling or `SIGKILL` path exists in any shipped source file.

Controlled live test: `scripts/live-handoff-test.py` opens real Terminal windows
through real `osascript`, runs three generations of a chain, proves
`Ranger -> Ranger 2 -> Ranger 3`, proves the exact parent process is stopped
with one `SIGTERM` while its Terminal keeps a live shell, proves an unrelated
Claude process is untouched, and proves that a successor reporting the wrong
model leaves its parent running. See
[../LIVE_TEST_EVIDENCE.md](../LIVE_TEST_EVIDENCE.md).
