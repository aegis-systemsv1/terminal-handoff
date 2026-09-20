# 4. Remote session control sits above, and never replaces, the transfer state machine

Status: accepted

## Context

Handoff left the successor waiting, offered no remote reachability, and gave a
phone no way to start or steer work. We wanted that without weakening the
single-owner guarantee or the permission model.

## Decision

* Ownership stays decided only by the existing transfer state machine.
  Continuation and logical sessions are layered on it and cannot move ownership.
* A **logical session** is the durable control object. Remote clients address it,
  never a PID. Ownership changes are fenced by an epoch.
* **Tailscale** is the only network path; the gateway binds loopback and fails
  closed. A per-device token is a second factor.
* **Approvals are cooperative Terminal Handoff gates**, bound to session,
  request, exact action, epoch, nonce and expiry. Claude's native permission
  prompts are never answered programmatically: no keystroke injection, no PTY
  writes, no use of Claude's undocumented socket.
* **Waking** uses a durable inbox as the source of truth plus a bounded
  long-poll from inside the agent (and a documented `Stop` hook), because Claude
  offers no supported external wake interface.
* **Permission isolation:** `--settings` merges with other sources, so remote
  sessions also use `--setting-sources ""`. That flag is undocumented, so it is
  proven per Claude version by `remote verify-isolation`, and remote launch fails
  closed otherwise.
* **Dead owners** become `ORPHANED` only on sustained, independent evidence, and
  are never replaced automatically.

## Consequences

Remote sessions load only the Terminal Handoff profile (not project or user
settings). Wake latency is bounded by the agent's next check. Full Mac reboot
recovery and relaunching orphaned sessions are deferred.
