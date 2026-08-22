# Decision 3: Manual handoff is a runtime recovery operation

**Status:** accepted  
**Date:** 2026-08-22

## Context

An automatic handoff can fail after claiming its parent session: Terminal may
reject Automation, Claude may reject a model, a successor heartbeat may time
out, or the supervisor may record a terminal failure. The user still needs a
single command that transfers the same session without constructing launch
commands by hand.

A prompt-only skill is not sufficient. The model must not guess the current
session ID, model, effort, process, chain or transfer state, and it must not
decide whether an earlier automatic attempt is safe to reuse.

## Decision

Install a personal, user-invocable-only Claude Code skill at
`~/.claude/skills/handoff/`. `/handoff` passes Claude Code's own
`${CLAUDE_SESSION_ID}` substitution to a non-shell helper, which calls the
Terminal Handoff runtime's `manual-handoff` command.

The status line stores one minimal private snapshot for each live session. The
runtime accepts the manual request only when that exact snapshot is fresh and
valid, the current Claude ancestor is exactly bound, the generation ceiling
permits another generation, and no live or completed transfer exists. A failed
attempt is archived before a replacement claim is made under the same lock used
by automatic triggers.

After that claim, manual and automatic handoffs use the same manifest, launch,
successor heartbeat, ownership state machine, parent-stop and notification
code. The current parent remains owner until verification completes.

## Consequences

- `/handoff` works in every local project without adding files to application
  repositories.
- Claude cannot invoke it automatically.
- Recovery cannot create a duplicate beside an active or completed transfer.
- The runtime, not the model, decides whether retry is safe.
- A stale status line or unprovable parent process blocks recovery visibly.
- A user-owned skill named `handoff` is never overwritten.
