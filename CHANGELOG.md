# Changelog

All notable changes to Terminal Handoff are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-08-22

### Added

- **Durable handoff notifications.** A committed `TRANSFER_COMPLETE` or
  `TRANSFER_FAILED` transition now creates one deterministic event in a private
  transactional outbox. Local macOS alerts are enabled by default. Signed HTTPS
  webhooks and Messages/iMessage/SMS relay are explicit opt-ins.
- **Presence-aware routing.** Presence is an explicit `home`, `away` or
  `unknown` state, never inferred from location or activity. Messages can be
  limited to away/critical events while a private webhook gateway routes web
  push, SMS or a messaging app.
- **Delivery guarantees and operations.** Per-channel ledgers, HMAC-SHA256,
  idempotency keys, bounded exponential retry, dead-letter storage, worker
  self-recovery, commit/outbox reconciliation, redacted status, channel tests
  and explicit retry commands.
- **macOS Keychain webhook secrets.** Multi-generation chains can retrieve a
  signing secret by service/account without storing it in configuration or a
  generated successor launch script.
- A package-facing `terminal_handoff.notifications` API and dedicated
  notification, routing, privacy, signing, retry and CLI tests.

### Fixed

- **Closed the ownership-overlap window.** `PARENT_STOP_REQUESTED` previously
  gave ownership to the successor even though the parent could remain alive
  through two graceful-stop periods. It now has owner `none`; both sessions are
  read-only until the parent's exit is confirmed and `TRANSFER_COMPLETE` is
  committed. The successor repeats critical Git checks at that boundary.
- **Supervisor crashes are recoverable.** A permanent `O_EXCL` marker could
  strand a transfer forever. Supervisors now hold a kernel-released `flock`
  lease. Parent and successor status refreshes detect and respawn a missing
  supervisor; concurrent replacements still cannot signal twice. A crash after
  `PARENT_STOP_REQUESTED` is resolved by observing the exact parent through the
  original grace budget, never by sending an unprovable second signal.
- A detached-launcher `Popen` failure is no longer ignored. It produces
  `TH failed`, a durable failure event and a bounded retry instead of leaving a
  session stuck in `launching`.
- A parent that exits naturally between identity verification and `os.kill`
  returning `ESRCH` is recorded as a successful completed transfer.
- Successor-heartbeat exceptions are now visible in the structured log.
- Detached child redirection handles are closed after spawning.

### Changed

- New status-line installs use a five-second `refreshInterval` for responsive
  two-heartbeat verification. Existing `refreshInterval`, `padding` and unknown
  future status-line options are all preserved rather than silently dropped.
- Atomic private writes now `fsync` the containing directory after `os.replace`
  where the platform supports it.
- Runtime, package metadata and documentation advance to 1.2.0.

### Security

- Outbound events deliberately exclude transcript contents and paths, prompt
  and repository paths, file contents, environment dumps, credentials and
  secrets. Webhook delivery requires HTTPS and a signing secret; external
  channels are disabled by default.
- Messages recipient and AppleScript content are escaped as data and passed to
  `osascript` as separate argv elements. No shell is used for notification
  delivery.

## [1.1.1] - 2026-08-22

### Fixed

- **The launch record could appear before the records it summarises.** A
  handoff wrote `completed/<session-id>.launch.json` before updating the
  manifest, the lifecycle state and the transfer record, so anything watching
  for that file - the test suite, and any external tooling - could read a
  manifest still marked `eligible` for a launch that had already happened. The
  launch record is now written last on every path, so its appearance means
  every other record for that handoff is already on disk. Surfaced by an
  intermittent failure of `test_30_test_mode_suppresses_real_terminal_launch`
  on a slow runner.

### Changed

- Documentation states the residual PID-reuse window plainly: re-proving the
  binding immediately before signalling reduces the window to microseconds and
  makes a substituted process detectable in the general case, but POSIX signals
  name a PID rather than a process and macOS offers no handle that closes the
  gap entirely.
- The upgrade snapshot and rollback procedure are documented in
  `docs/INSTALLATION.md`.

---

## [1.1.0] - 2026-08-22

Behaviour and safety release. Two production defects reported from live use are
fixed: successors were named with an internal identifier instead of the user's
own session name, and a parent session kept running after its successor was
launched, so two agents could work on the same repository at once.

### Fixed

- **Successor naming exposed an internal identifier.** A session named `Ranger`
  produced a successor named `terminal-handoff-7a282bd6-g2`. Successors are now
  named `Ranger 2`, `Ranger 3`, `Ranger 4`, and so on, in both the Claude
  session name and the Terminal window title. Generation 1 keeps its name
  unchanged; no `1` is ever appended.

  The base name is captured once, from `.session_name` in the official
  status-line JSON, and stored as explicit chain metadata in
  `chains/<chain-id>.json`. Later generations read it from that trusted state,
  never from the visible session name, and the generation number comes from
  chain state rather than from parsing trailing digits — so `Project 42` hands
  off to `Project 42 2`, not `Project 43`. The machine-safe `chain_id` is still
  used for state keying and is never shown as a session name. When no session
  name is available, the documented fallback is
  `Terminal Handoff <chain-id[:8]>`; no repository or directory name is ever
  invented.

- **The parent session kept running after a handoff.** Once the successor has
  proved itself, the exact parent Claude process is now asked to exit
  gracefully, so one session continues the work. Its Terminal window stays open
  at a shell prompt.

### Added

- **A transfer-of-ownership state machine** with one boundary: `LAUNCHING` ->
  `SUCCESSOR_VERIFIED` -> `PARENT_STOP_REQUESTED` -> `TRANSFER_COMPLETE`, and
  `TRANSFER_FAILED` from any non-terminal state. Before the stop request the
  parent owns continuation; after it, the successor does. Transitions are taken
  under an exclusive lock, refused when illegal, and appended to an auditable
  history with a reason and the requesting PID. Records live in
  `transfers/<parent-session-id>.json` and are summarised by `status`.

- **A nine-point successor heartbeat gate.** The transfer is only verified when
  the successor reports, from its own live status-line JSON across two
  heartbeats: a fresh session ID, a session ID not already used elsewhere in the
  chain, the required model, the required effort level (including "none"), the
  required working directory, the correct chain ID, the correct generation and
  its own live context percentage. Any failure records `successor_mismatch` and
  leaves the parent running.

- **Exact parent-process binding.** The Claude Code process is bound inside the
  status-line process at trigger time, where its real ancestry is visible.
  Claude Code runs the status line through a shell, so the ancestry is traced
  with `ps` rather than assumed. PID, process start time, controlling TTY, UID,
  executable name, process working directory, session ID, chain ID and
  generation are recorded, and every one is re-proved immediately before any
  signal is sent.

- **A detached shutdown supervisor**, claimed per transfer with
  `O_CREAT|O_EXCL`, so duplicate status-line invocations cannot produce a second
  shutdown attempt. Restart from any transfer state is deterministic.

- **`supervise` subcommand** (internal) and new configuration:
  `CLAUDE_TERMINAL_HANDOFF_STOP_PARENT`,
  `CLAUDE_TERMINAL_HANDOFF_HEARTBEAT_TIMEOUT`,
  `CLAUDE_TERMINAL_HANDOFF_STOP_GRACE`,
  `CLAUDE_TERMINAL_HANDOFF_STOP_ATTEMPTS`,
  `CLAUDE_TERMINAL_HANDOFF_STOP_DRY_RUN` and
  `CLAUDE_TERMINAL_HANDOFF_TRANSFER_POLL`.

- **Terminal Handoff configuration is inherited by successors.** Apple Terminal
  starts a fresh login shell that does not inherit the launcher's environment,
  so every `CLAUDE_TERMINAL_HANDOFF_*` variable set at trigger time is now
  written into the successor's launch script. A chain keeps the settings it was
  started with.

- **82 new tests** across `tests/test_naming.py`, `tests/test_transfer.py` and
  `tests/test_parent_stop.py`, plus a controlled live test,
  `scripts/live-handoff-test.py`, that drives real Terminal windows, real
  `osascript`, real process ancestry and real signals.

- **Two new facade modules**, `naming` and `transfer`.

### Security

- **No SIGKILL path exists.** One signal type, `SIGTERM`, to one PID, at most
  twice, with no escalation. A parent that does not exit is recorded as
  `parent_stop_unconfirmed` and left running.
- **No broad process targeting.** No `pkill`, no `killall`, no process-name
  pattern matching, no process groups, no unverified PID files, no Terminal
  front-window assumptions, no generated shell commands. The suite enforces this
  statically against every shipped source file, with comments and string
  literals stripped so documentation cannot satisfy the check.
- **Wrong-session protection.** A bound candidate is rejected unless it is
  within six ancestry levels, owned by the same UID, not the signalling process
  itself, and working in the same directory as the session in the status-line
  JSON. Binding a Claude process from an unrelated session would otherwise stop
  the wrong work.
- **Session names are untrusted text.** Control characters are stripped,
  whitespace collapsed, leading dashes removed so a name cannot look like a
  flag, and length bounded to 64 characters. The name is then passed as a single
  argv element, `shlex.quote`d in the launch script and escaped for AppleScript.
  Unicode is preserved. Tests plant metacharacters, command substitutions,
  backticks and canaries and prove nothing executes.
- Test mode and `CLAUDE_TERMINAL_HANDOFF_STOP_DRY_RUN` run the entire shutdown
  path and never signal a real process.

### Changed

- `MANIFEST_SCHEMA_VERSION` is now `2`: manifests carry a `display` block
  (base name, its source, this generation's name, the successor's name) and the
  `successor` block records every heartbeat check.
- The documented statement "The original Terminal is never closed" has been
  removed. The Terminal window is still never closed, but the parent Claude
  session is now stopped once the successor is verified, and the documentation
  says so precisely.
- The successor prompt states the ownership boundary explicitly: read, search
  and verify freely, but mutate nothing until the heartbeat is validated,
  repository verification is complete, and the transfer state authorises it.

### Migration

No action is required. Existing chains keep working; manifests written by 1.0.x
are still readable. To keep the previous behaviour of leaving the parent
running, set `CLAUDE_TERMINAL_HANDOFF_STOP_PARENT=0` before starting `claude`.

---

## [1.0.1] - 2026-08-18

Hardening and documentation release. **No behavioural change to the 80% handoff
mechanism**: threshold detection, model and effort preservation, manifest
creation, transcript isolation, the successor lifecycle and every safety guard
are byte-for-byte the behaviour shipped in 1.0.0.

### Added

- **Stronger `--wrap` security documentation.** `docs/SECURITY_MODEL.md` gains a
  prominent section covering what the mechanism does, why wrapping an existing
  status line is required rather than replacing it, where the trust boundary
  sits, that the command originates from the user's own Claude settings and is
  granted no privilege it did not already have, that no status JSON, transcript
  path, transcript content, model ID, effort value or manifest data can enter
  it, the real risk of an already-malicious pre-existing command, safe and
  unsafe examples, how to inspect a command before installing, how to refuse
  wrapping, how to use dry-run mode, and how to uninstall and restore.
- **Wrapped-command integrity tests** (17). Shell metacharacters and a
  filesystem canary are planted in every payload field, in transcript contents
  and in a tampered manifest; the wrapped command is proven to run with a
  byte-identical invocation and zero arguments, receiving the status JSON only
  as stdin, with no canary ever created.
- **Unusual-layout install and uninstall hardening tests** (19), covering
  installation as `core.py`, `terminal-handoff.py`, `terminal_handoff.py` and a
  neutrally-named executable in a neutrally-named directory; package-module
  invocation; symlinked and space-bearing installation directories; a
  third-party status line; repeated installation; an interrupted installation
  whose registry was lost; a missing backup; multiple backups; malformed
  settings JSON; settings without a `statusLine`; a legacy Terminal Handoff
  command; a partial installation; a renamed executable; uninstall after module
  relocation; and a full install / reinstall / uninstall round trip.
- **Recursive self-wrap regression protection.** Generated status-line commands
  now carry an explicit `--marker terminal-handoff` token, so the installer
  recognises its own command regardless of module filename or directory name.
  Previously, detection relied on the path containing `terminal-handoff`; a
  runtime renamed and placed in a neutrally-named directory would not have been
  recognised and would have been wrapped recursively.

### Fixed

- **Uninstall could discard a third-party status line** when the install
  registry was missing (an interrupted install, a deleted state directory). The
  original command is now recovered from the installed command's own `--wrap`
  argument and restored, rather than the `statusLine` key being removed.
- **Settings files using non-ASCII escapes were rewritten with literal
  characters.** 1.0.0 preserved a file's trailing-newline convention but always
  wrote non-ASCII literally, so a file written with backslash-u escapes had
  unrelated keys rewritten. The escaping convention of the original file is now
  recorded at install time and reproduced on both install and uninstall.
- **Correct standalone MIT licence detection.** `LICENSE` contains only the MIT
  text so GitHub and licence scanners detect it; the independence statement,
  trademark attribution and third-party code status moved to `NOTICE.md`.

### Unchanged

The handoff mechanism itself. The threshold remains 80% by default, read only
from `.context_window.used_percentage`; the successor still receives the exact
model and effort, a fresh session ID and a clean context window; the parent
transcript is still analysed by an isolated subagent; and one trigger per
session with unlimited generations per chain is unaltered. All 1.0.0 tests pass
unmodified.

## [1.0.0] - 2026-08-18

First release.

### Added

- **Global status-line monitoring.** Terminal Handoff installs as the Claude Code
  `statusLine` command and observes every session on the machine, using only the
  official status-line JSON.
- **Configurable threshold**, default 80%, read exclusively from
  `.context_window.used_percentage`. Account rate-limit percentages are never
  read; usage is never estimated from transcript size, message count or elapsed
  time. A missing, null, non-numeric or out-of-range value never triggers.
- **Fresh successor session.** A new macOS Terminal window runs an ordinary
  interactive `claude` with a new session ID and a clean context window. Never
  `--continue`, `--resume`, `--fork-session`, the parent session ID, compaction
  or transcript replay.
- **Model and effort preservation.** The successor is launched with the exact
  `.model.id` and `.effort.level` reported by the outgoing session, passed as
  separate argv elements. No silent fallback to a cheaper, faster or default
  model, and no substituted effort level. Bracketed model IDs such as
  `claude-opus-5[1m]` are preserved intact through shell and AppleScript
  quoting. When `.effort` is genuinely absent, `--effort` is omitted and the
  fact is recorded.
- **Secure handoff manifest** per outgoing session: identity, model, effort,
  trigger percentage, chain and generation, applicable `CLAUDE.md` paths, and a
  full repository snapshot including merge, rebase, cherry-pick and revert
  state. Written `0600`, containing no credentials, tokens, environment dumps,
  file contents or transcript contents.
- **Isolated transcript analysis.** The successor delegates parent-transcript
  analysis to a temporary subagent and receives only a concise continuation
  brief, so an almost-full parent transcript never enters the successor's main
  context.
- **Repository verification.** The successor independently re-derives working
  directory, root, branch, HEAD, `origin/main`, ahead/behind, status and any
  in-progress Git operation, and treats the live state as authoritative over the
  transcript. Existing changes are treated as user-owned.
- **Continuous handoff generations.** One chain ID spans the sequence; each
  successor may hand off once, so the chain continues indefinitely.
  `CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS` sets an optional ceiling; unset
  means unlimited.
- **One-shot session protection** via an atomic `O_CREAT|O_EXCL` claim keyed by
  `session_id`, so concurrent status-line processes produce exactly one launch
  and a successor never inherits its parent's claim.
- **Bounded retry and storm protection.** Every launch failure records
  diagnostics, applies a cooldown and permits at most two retries. A circuit
  breaker trips after three launches in ten minutes, is logged, is visible in
  the status line, and can be reset.
- **Successor heartbeat.** A launch is not marked completed until the successor
  reports its own fresh session ID, model, effort, working directory and live
  context percentage back into the parent's manifest.
- **Status-line wrapping.** An existing status-line command is wrapped, not
  replaced: it receives the identical stdin bytes and its stdout is preserved
  byte-for-byte, with only a short badge appended.
- **Installer and uninstaller.** The installer verifies macOS, Python 3.9+,
  Claude Code, `--model`/`--effort` support, `osascript` and Apple Terminal, and
  fails closed. It backs up before modifying, merges into existing settings
  without disturbing unrelated keys, validates the resulting JSON, and offers a
  dry-run mode. The uninstaller restores any pre-existing status line exactly
  and retains manifests, logs and backups.
- **Automated test suite** covering threshold behaviour, malformed input, model
  and effort preservation, generations, concurrency, kill switch, circuit
  breaker, cooldown, retry, repository capture, status-line wrapping, transcript
  privacy, filesystem permissions and uninstall restoration. It opens no
  Terminal window, starts no Claude session and consumes no context window.

### Known limitations

- `ultracode` cannot be preserved: it resolves to `xhigh` plus a hidden internal
  flag that the status-line JSON never exposes. Recorded as
  `effort.ultracode: "undetectable"`.
- Only effort values exposed by official status-line data are preserved:
  `low`, `medium`, `high`, `xhigh`, `max`.
- Model validity cannot be verified before launch; the heartbeat is what
  confirms a successor actually started correctly.
- A genuine natural 80% live trigger was not exercised, because doing so
  consumes roughly 160,000 tokens of context. Threshold logic is tested with
  synthetic payloads built from the official schema. The real Terminal launch,
  fresh session, model, effort, working directory, transcript isolation,
  repository verification and successor heartbeat were live-tested.
- macOS and Apple Terminal only. Other terminal emulators are not implemented.
- Claude Code only. Codex and other agent CLIs are not implemented or verified.
- A project-level `statusLine` overrides the global one and must be integrated
  explicitly; `coverage` reports which configurations are covered.

[1.0.1]: https://github.com/aegis-systemsv1/terminal-handoff/releases/tag/v1.0.1
[1.0.0]: https://github.com/aegis-systemsv1/terminal-handoff/releases/tag/v1.0.0
