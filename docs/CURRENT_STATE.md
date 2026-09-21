# Current state

**Version 1.5.0.** Persistent remote session control for Claude Code (accepted on a physical iPhone on
2026-09-20/21) and, new in 1.5.0, Grok (implemented and tested against a mocked ACP agent; **physical iPhone
acceptance pending**). This is a snapshot for people picking the project up; the authoritative description is the
[README](../README.md) and [REMOTE_CONTROL.md](REMOTE_CONTROL.md).

## Capability

- Automatic A → B handoff with sole-owner transfer and automatic continuation.
- A persistent **logical session** (ID, name, project, instruction queue, STOP, approvals, history) above
  replaceable Claude processes, fenced by an ownership epoch.
- Secure remote control from an enrolled device over Tailscale: start, name/rename, instruct, pause,
  STOP, resume, approve, recover.
- Owner-death detection (`ORPHANED`), service-restart recovery, orphaned trigger-claim recovery.
- Claude Remote Control health reporting; your chosen Claude mode carried across handoffs.
- **Grok** as a second agent (`agent_type = grok`): a detached ACP bridge is the sole writer; the exact Grok
  session id is stored and reloaded on reconnect; STOP uses `session/cancel`; Grok permission requests are
  Terminal Handoff approvals; ask mode by default (refuses to start if Grok is configured always-approve);
  no automatic A to B handoff for Grok. Per-project opt-in.

## Production defaults

| Setting | Value |
|---|---|
| Automatic handoff threshold | **80%** (`CLAUDE_TERMINAL_HANDOFF_THRESHOLD`) |
| Generation limit | unlimited (`CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS`) |
| Gateway | loopback only, published on a tailnet-only HTTPS port through `tailscale serve`; never Funnel |
| Device credential lifetime | 14 days, revocable |
| Remote projects | **opt-in** per project; none enabled by default |

Lower thresholds and generation caps were used only as **test overrides** during acceptance and are not
part of normal operation.

## Where state lives

`~/.claude/terminal-handoff/` (mode 0700): `logical/` (logical sessions), `remote/` (gateway config,
project registry, device hashes, signing key, isolation record), `transfers/`, `handoffs/`, `chains/`,
`logs/`. Nothing there is committed to the repository.

## Decisions

See `docs/decisions/`: 0004 (remote session control above the transfer state machine), 0005 (preserve the
user's Claude permission mode), 0006 (recoverable trigger claim).

## Known limitations

See the README. In short: Claude's runtime mode can change independently; Auto Mode profiles are not a
sandbox; the interactive Stop hook is unverified; new projects need manual folder trust; wake latency
varies; no Mac-reboot recovery.
