# Decision 0004: native multi-session coordination

**Status:** Accepted  
**Date:** 2026-08-23

## Context

Independent Claude Code sessions can legitimately run at the same time. They
become dangerous when they share a working tree, edit overlapping files, run
competing migrations or change the same branch without knowing about one
another. Killing every second session would destroy useful parallelism and can
discard user-owned work.

Claude Code 2.1.224 introduced native cross-session discovery and messaging
through `ListAgents` and `SendMessage`. A second private transport would add
identity, delivery and security problems without improving the supported path.

## Decision

Terminal Handoff will:

1. derive short-lived peer presence from its existing private minimal status
   snapshots
2. treat exact and nested workspaces as possible conflicts while leaving
   sibling worktrees independent
3. show `peers N` and expose a read-only `coordination status` command
4. instruct Claude sessions to use native `ListAgents` and `SendMessage`
   proactively for task, file and Git-operation intent
5. require one owner for overlapping file sets and branch-changing operations
6. allow independent work to continue
7. fail closed on unresolved overlap without killing or commandeering a peer
8. treat every peer message as advisory data, never user consent

## Rejected alternatives

### Automatically stop one of the sessions

Rejected because session age, name or context percentage does not prove which
work the user values. Stopping the wrong process can discard work.

### Install a second socket or message bus

Rejected because Claude Code already provides the supported identity and
delivery mechanism. Duplicating it increases attack surface and failure modes.

### Hard-lock the whole repository

Rejected because it prevents safe parallel work in separate files and
worktrees. Coordination is scoped to actual overlap.

### Treat peer agreement as approval

Rejected. Agents cannot grant one another user authority for destructive,
privileged, external, production or financial actions.

## Consequences

The system prevents common silent collisions while keeping useful parallelism.
The policy depends on native tools being available and on agents following the
managed instructions. It is not a filesystem-level mandatory lock. A future
hook-based lease layer may be added only if it can prove file-level ownership
without blocking independent work or creating a new destructive path.
