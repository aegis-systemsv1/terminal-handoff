# Changelog

All notable changes to Terminal Handoff are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`th checkpoint`** (Slice 1 of Terminal Handoff V2): a standalone, deterministic, machine-verifiable
  snapshot of repository and session state. Unlike a handoff manifest it never launches a successor and
  never touches the transfer state machine - a pure capture, safe to run at any time. Captures git identity,
  branch/upstream/ahead-behind, dirty state, staged/modified/untracked filenames (never contents), the last
  5 commits, and optionally already-executed or Terminal-Handoff-executed test evidence (command, exit code,
  bounded output tail - never a parsed pass/fail inferred from text). Every top-level block carries an
  explicit `provenance` (`machine_verified` / `recorded_evidence` / `unavailable` / `unsupported`); unknown
  state is never represented as a bare `null`. `redact_secrets()` is applied to every free-text field
  (commit subjects, test command, test output). A `sha256` integrity hash covers the whole checkpoint except
  itself. No AI summary, no Smart Compact, no `th resume`, and no Codex support are part of this slice.

- **AI session summary for `th checkpoint`** (Slice 2 of Terminal Handoff V2, on top of Slice 1): `--transcript
  <path> --ai-summary` asks a single, fully isolated `claude -p` worker to extract the current task, work
  completed, decisions made, files in progress, known problems, tests performed, outstanding work, explicit
  user instructions/constraints, and a recommended next action from a transcript. The worker's only permission
  is reading that one exact file (`--setting-sources ""` plus a `Read(<path>)`-only settings file). It runs with
  a minimal explicit environment, never the caller's full one. **Security note, found and fixed during
  acceptance testing, not by unit tests alone**: `--add-dir <dir>` makes the whole named directory readable
  regardless of any narrower `Read()` rule - confirmed live against the real CLI, where a worker asked (with a
  plain, non-adversarial prompt) to read a sibling file sitting next to the real transcript could do so. The fix
  is structural, not a permission rule: the transcript is copied into a directory created fresh for this one
  call, containing nothing else, ever, and `--add-dir` is granted to that directory instead of the transcript's
  real (potentially multi-session) one. Re-verified live afterward that the same sibling-file attempt fails
  closed. Its entire output is treated as untrusted text - parsed as JSON, every string passed
  through `redact_secrets()`, file paths reused through the existing sensitive-filename classifier - never
  executed and never treated as an instruction, regardless of what the transcript it read contained. The
  summary block always carries `provenance: ai_generated`, is never upgraded to `machine_verified`, and any
  missing or undeterminable field is reported as an explicit `"unresolved"` marker rather than guessed. If the
  transcript is missing, unreadable, or the worker fails or returns malformed output for any reason, the block
  degrades to `provenance: unavailable` with a reason - the deterministic Slice 1 checkpoint is always still
  produced. `th checkpoint` never loads the transcript itself; only the isolated worker subprocess does. No
  Smart Compact, no `th resume`, and no Codex support are part of this slice.

- **Smart Compact for `th checkpoint`** (Slice 3 of Terminal Handoff V2, on top of Slices 1-2): `--compact`
  classifies an already-built checkpoint's own fields into KEEP / COMPRESS / DROP / VERIFY - no second
  transcript read, no second AI call; it operates purely on what Slices 1 and 2 already captured, so it can
  never diverge from the rest of the checkpoint. KEEP always carries the current task, explicit user
  instructions/constraints, and unresolved work verbatim, plus verified git/test facts; these are never
  compressed, dropped, or paraphrased. COMPRESS carries useful history (decisions, completed work, known
  problems, commits condensed to one-liners). DROP removes raw, reconstructible material (test output text,
  machine identity) with a stated reason, never the value itself. VERIFY cross-checks AI-summary claims against
  the checkpoint's own deterministic blocks (tests_performed against the tests block's exit code, files claimed
  "in progress" against the actual working-tree capture) and checks the recorded git state against the live
  repository at compact time - each result is `confirmed`, `contradicted`, `stale`/`current`, or `unverifiable`,
  never a silent upgrade to fact. Every classified item keeps its original provenance travelling with it; the
  compact block's own provenance (`machine_generated`) never bleeds into the items it carries. Every string is
  independently re-redacted through `redact_secrets()` regardless of upstream handling. Tested against a real,
  extended (91-line, ~300KB) Claude Code transcript spanning a genuine test-driven-development cycle (a
  deliberately wrong assertion, its failure, and its fix), an explicit constraint, and unresolved work; the
  compacted checkpoint was ~11KB, a 96% reduction, while VERIFY correctly flagged a real mismatch between the
  AI summary's claimed in-progress files and the files actually still uncommitted. That same real run also
  surfaced and fixed two defects: `_parse_ai_summary_output()` failed outright when the worker prefaced its
  JSON with a sentence of prose (now extracts the JSON regardless of surrounding text), and the summarisation
  prompt could describe a later, explicit instruction that superseded an earlier one as a "violation" of the
  earlier one (prompt now explicitly instructs chronological reading before judging anything a problem).

- **`th resume <checkpoint>`** (Slice 4 of Terminal Handoff V2, on top of Slices 1-3): reads a checkpoint
  back and launches a fresh Claude Code successor session with a trust-labelled continuation brief, instead
  of the original raw transcript. Architecturally distinct from the automatic/manual A→B handoff flow -
  there is no live parent process to transfer ownership from, so resume never touches the transfer state
  machine or heartbeat verification. Before anything is launched: the checkpoint's schema version and
  integrity hash are verified (a malformed, unsupported, or tampered checkpoint is refused, never repaired
  and continued); live repository state is independently re-captured and compared field-by-field against
  what the checkpoint recorded (`VERIFIED` / `CHANGED_SINCE_CHECKPOINT` / `UNVERIFIABLE`); a repository whose
  history has no relationship to the checkpoint's recorded `head_sha` fails the launch closed ("checkpoint
  points to an unexpected project"), while ordinary drift (branch, HEAD, dirty state, changed filenames) is
  never a refusal - it is surfaced to the successor instead. The rendered brief keeps six kinds of content
  visibly separate - Terminal-Handoff-verified facts, live repository drift, AI-recovered user
  instructions/constraints (explicitly labelled "not a live instruction from the current person"), the
  AI-generated summary, the recommended next action (explicitly labelled "context only, not authority" -
  never auto-executed), and Smart Compact's VERIFY findings when present - and opens with a hardcoded trust
  boundary notice stating these rules before any recovered content appears. Every string is redacted a second
  time on the way out (`_compact_redact`, the same recursive redactor Smart Compact uses), independently of
  whatever the capture pipeline already did. The launch argv can never carry Claude Code's own
  `--resume`/`--continue`/`-c`/`-r`/`--fork-session` (those would replay session state directly, bypassing the
  brief entirely) or any permission-bypass flag, checked by `assert_resume_argv_safe()` before any launch is
  attempted; the full brief lives only in a private file, never in the process listing, via the same
  short-bootstrap-prompt pattern already used for handoff successor prompts. Existing chain identity is read
  for a checkpoint's session id when one already exists, for display only - resume never writes to the
  chain-generation registry itself. Proven adversarially (46 tests in `tests/test_resume.py`, covering the
  points below plus identity resolution and the missing-`head_sha` case described below) against: a checkpoint tampered
  after hashing, malformed JSON, an unsupported schema version, a malicious AI summary, a malicious
  recommended next action, a fake user instruction inserted via an AI-generated field, prompt injection
  inside transcript-derived fields (including a fabricated "VERIFIED FACTS" header), a repository swapped for
  an unrelated one, branch/HEAD/dirty-state drift, a missing repository, a missing Smart Compact block, a
  missing `claude` binary, and a secret embedded directly in an AI-generated field bypassing the normal
  capture pipeline. Two real defects were found and fixed by this same adversarial testing before commit:
  test fixture repos could coincidentally produce identical commit shas (defeating the "different project"
  test), and the structured (JSON) brief was not redacted, only the rendered text. Demonstrated end-to-end
  against a real repository, a real transcript, and a real `--ai-summary --compact` checkpoint, with a real
  commit landed on the repository after the checkpoint to force genuine drift: the resulting brief, fed to a
  real one-shot `claude -p` call standing in for the successor, correctly named the task, what was completed,
  what remained, quoted the constraints verbatim, identified the recommended action as unverified AI content
  and declined to execute it automatically, and specifically named every changed field. The structured brief
  is also persisted to disk (`prompts/resume-<checkpoint_id>.json`), not just built and discarded in memory,
  after independent review found the first version never actually wrote it despite documenting it as the
  ground truth a successor should prefer. A second independent review then found a genuine gap in the
  "different project" fail-closed check: a hand-crafted checkpoint (bypassing the capture pipeline, with a
  correctly recomputed hash) that records no `head_sha` at all bypassed the refusal entirely, since resume
  only treated an explicit mismatch as disqualifying, not an absent one - `resume_preflight` now fails
  closed on a missing `head_sha` too, with a dedicated regression test; a second, more minor coverage gap
  (the positive identity-resolution path through `cmd_resume` was previously exercised only by manual
  verification) was also closed with two new tests.

## [1.5.0] - 2026-09-21

Grok is now a second supported agent. Claude Code behaviour is unchanged.

### Added

- **Grok as an agent**, driven over the Agent Client Protocol (`grok agent stdio`) by a detached Terminal
  Handoff *bridge* that acts as the ACP client and the session's sole writer. A logical session records
  `agent_type` (`claude` | `grok`); sessions without one are Claude Code. The agent is fixed at creation.
- **New Session > Agent** (Claude Code by default). Session cards and pages name the agent.
- Exact Grok session id stored on the logical session; reconnect uses `session/load` on that id and never
  starts a new conversation.
- STOP for Grok: `session/cancel`, then the Grok process is ended if it does not stop; queued
  instructions are not delivered while stopped, and resume restores delivery.
- Grok permission requests become Terminal Handoff approvals (phone approve/deny answers through ACP,
  `allow_once` only).
- Transcript mapping for Grok: replies, tool-call status, lifecycle events. Reasoning is never shown.
- Per-project opt-in (`th project enable-grok` / `disable-grok`); `/api/v1/projects` also returns the
  agents each project allows.
- `th status` gains a `grok` health block (executable, version, credential presence, permission posture,
  ACP state per session). Credentials are never read.
- `th session recover --recover-action reattach` (and the phone's Re-check) starts a new bridge for an
  orphaned Grok session.

### Security

- Grok is never started with `--always-approve` by default. Terminal Handoff refuses to start Grok while
  `~/.grok/config.toml` selects always-approve, unless `grok_permission_mode` is set to `always-approve`
  in `remote/config.json`, deliberately.
- Grok only starts in a registered, enabled project resolved to its pinned realpath; no path is accepted
  from the phone.

### Not changed

- Claude Code launch, permissions (including Auto Mode), handoff, ownership, STOP, approvals, transcript
  and archive semantics. Grok has **no** automatic A to B handoff: it uses Grok's own persisted session.

## [1.4.2] - 2026-09-21

### Fixed

- **New Session said "project and a non-empty task are required" for a task that was not
  empty.** The task was over the length limit; the server reported that as an empty task.
  It now says `Task is too long: N characters; maximum is 24,000`. Empty tasks and missing
  projects also get their own accurate messages.

### Changed

- The New Session task limit rose from 8,000 to 24,000 characters. Follow-up instructions
  keep their 8,000-character limit, and the 32 KB request-body limit is unchanged.

## [1.4.1] - 2026-09-20

Two things physical iPhone use showed were not good enough. No change to handoff,
ownership, STOP or approval behaviour.

### Added

- **A real session transcript on the phone.** The session page now shows one
  scrollable logical-session history instead of a twenty-line snapshot: Claude's
  output and the lifecycle events (created, owner registered, handoff complete,
  STOPPED, STOP cleared, resumed, orphaned, abandoned, archived) in one ordered
  list, with generation boundaries marked rather than hidden. Retention rose from
  200 to 2000 lines, and older history is fetched lazily, a bounded page at a
  time, when you scroll to the top. New `GET /api/v1/sessions/<id>/transcript`.
- **Live follow, and a reading mode that holds still.** While you are at the
  bottom the newest output stays in view. The moment you scroll up, auto-scroll
  stops and your position is kept exactly: incoming output can no longer drag
  you down or shift what you are reading. A "N new updates ↓" pill and a
  "↓ Latest" button return you to the bottom and re-enable follow.
- **Archive old sessions from the phone.** A finished session (`COMPLETED`,
  `FAILED`, `ORPHANED`) can be removed from the list after a clear confirmation,
  and restored again. The session list is now grouped into Active,
  Recent / closed and Archived.
  New `POST /api/v1/sessions/<id>/archive` and `/restore`.

### Safety

- Archiving is a **soft delete of Terminal Handoff's own record only**: the
  record, its name, project and audit history are kept. No project file,
  repository or other logical session is touched, and nothing is deleted from
  disk. An active session (`RUNNING`, `WAITING_FOR_HUMAN`, `PAUSED`, `STOPPED`,
  `CREATING`) is refused, as is any session whose owner is still verified alive.
- The transcript is drawn from the same already-redacted output the gateway
  served before; it exposes nothing new.

### Fixed

- The transcript is appended to, never rebuilt, so polling no longer disturbs the
  instruction box: focus, draft, selection, the iOS keyboard and native iOS paste
  all behave exactly as they did after the 1.4.0 fix.

## [1.4.0] - 2026-09-20

This is the milestone that turns Terminal Handoff from basic session transfer into persistent remote Claude Code session control: a logical session that outlives individual Claude processes, hands off automatically, and can be started and steered from an enrolled iPhone over Tailscale while Claude keeps running on the Mac. Physical iPhone acceptance completed 2026-09-20 (see docs/ACCEPTANCE.md).

### Added

- **Automatic continuation.** After `TRANSFER_COMPLETE` the successor verifies
  Remote Control and continues the unfinished, already-authorised work instead
  of waiting. New `continuation` command (`wait`, `gate`, `resume`, `status`,
  `remote-check`); machine-readable human gates; lifecycle phases recorded on
  the transfer. Continuation is never automatic approval.
- **Remote Control.** Successors are launched with `--remote-control`; health
  is verified from Claude's live session record and mirrored to the logical
  session. Failure records `DEGRADED_REMOTE`, alerts once and keeps working.
- **Logical sessions.** A durable registry object above disposable Claude
  sessions: fenced owner epoch, STOP/pause that survive handoffs and restarts,
  a durable ordered instruction inbox, approvals, owner health.
- **Remote gateway and mobile UI** (`remote` command): loopback-only HTTP
  gateway behind Tailscale, per-device revocable expiring tokens, CSRF/Origin/
  Host checks, persistent lockouts, a mobile web interface with no inline
  script. See [docs/REMOTE_CONTROL.md](docs/REMOTE_CONTROL.md).
- **Project registry and permission profiles** (`project` command), fail-closed
  validation, per-session `--settings` with `--setting-sources ""`, and
  `remote verify-isolation`.
- **Remote session creation** started from a phone and run on the Mac.
- **Terminal Handoff approvals** bound to session, request, exact action,
  owner epoch, nonce and expiry; one-shot; distinct from Claude permission
  prompts.
- **Dead-owner detection, ORPHANED state and service-restart recovery.**
- New notification kinds: `human_gate`, `remote_degraded`, `owner_lost`.

### Added (mobile)

- Optional session names, set at creation and changed from the session page (display metadata only).
- The instruction box is stable under polling and around iOS focus changes.

### Fixed

- An automatic trigger whose status-line process died after claiming but before launching stranded the session ("already handed off", never launched). The claim is now recoverable and the parent is bound before it is taken.

### Changed

- The single graceful-stop signalling call moved into `send_graceful_stop()`;
  the parent stop and the logical `--hard` backstop both use it.

### Fixed

- A successor launched after a launch-time parent-bind failure (an unbindable
  or wrong-cwd parent process) is now told, on its own status line, that it
  was never granted ownership. Previously `_supervise_transfer_claimed` sent
  the transfer straight to `TRANSFER_FAILED` before the successor's first
  heartbeat, but nothing distinguished that terminal rejection from the
  ordinary retryable `successor_mismatch` state, and nothing surfaced it
  outside the manifest. A real, running successor session could sit
  indefinitely alongside its still-live parent with no visible signal that it
  was never the owner. `successor_heartbeat` now records a distinct
  `rejected_transfer_failed` launch state, and the status-line badge shows
  `TH rejected (not owner)` on every render while that holds.

### Tests

- Added `TestRejectedSuccessorAfterUnboundParent` covering the terminal
  rejection, its status-line visibility, and that the pre-existing retryable
  `successor_mismatch` path is unchanged.

## [1.3.1] - 2026-08-23

### Fixed

- A verified later-generation session renamed from an old internal fallback now
  repairs that fallback before launching its successor. For example, generation
  4 named `DJI Drone 4` now launches both the Claude session and Terminal window
  as `DJI Drone 5`, instead of `Terminal Handoff <chain> 5`.
- The repair is deliberately narrow. It runs only when trusted chain state still
  contains Terminal Handoff's exact fallback and the live name's final number
  exactly matches the trusted generation. Mismatched or ambiguous names do not
  rewrite chain identity.

### Tests

- Added automatic and manual handoff regressions for the observed fallback-chain
  failure, plus a negative test proving a mismatched visible number is refused.

## [1.3.0] - 2026-08-23

### Added

- Private multi-session presence detection from the existing minimal status
  snapshots. Fresh sessions in the same or a nested workspace are reported as
  peers; stale sessions and sibling worktrees are excluded.
- A `coordination status` command for inspecting active sessions and conflicting
  workspaces without reading transcripts, arbitrary status fields or
  environment data.
- A `peers N` status-line indicator when another fresh Claude session can
  conflict with the current workspace.
- A managed multi-session policy that instructs Claude Code 2.1.224 or later to
  coordinate proactively through its native `ListAgents` and `SendMessage`
  tools, establish one owner per overlapping file set or Git operation, and
  continue independent work without unnecessary blocking.

### Safety

- Peer messages are coordination data only and never count as user consent.
  Unresolved overlap fails closed on the conflicting action. Sessions may not
  kill, reset, commandeer or silently overwrite one another.
- Existing managed `~/.claude/CLAUDE.md` blocks are upgraded in place while all
  user-owned content outside the markers is preserved. Malformed managed blocks
  are refused rather than guessed at.

### Tests

- Added coverage for same-workspace and nested-workspace peers, sibling
  worktree isolation, stale-session expiry, CLI output, status-line peer counts,
  consent boundaries and safe managed-instruction upgrades.

## [1.2.3] - 2026-08-23

### Fixed

- A successor already created by the v1.2.1 manual naming bug now reconnects
  to its original verified chain. The repair requires a completed manual
  transfer, the exact verified successor session ID, and exactly one prior
  chain containing the parent session. No visible number is parsed or trusted.
- After the user renames the already-open successor from `DJI Drone 3 2` to
  `DJI Drone 4`, the next automatic or manual successor is `DJI Drone 5`.

### Tests

- Added a regression that recreates the v1.2.1 split-chain records and proves
  the current successor maps back to generation 4 and launches generation 5.

## [1.2.2] - 2026-08-23

### Fixed

- Manual `/handoff` now recovers an existing successor's chain and generation
  from private verified chain state when the Claude Code tool subprocess does
  not retain Terminal Handoff's custom environment variables. A manual rescue
  of `DJI Drone 3` therefore launches and titles `DJI Drone 4`, rather than
  starting a new naming sequence.
- Recovered chain state remains authoritative when it disagrees with an
  inherited chain environment value. The mismatch is recorded without exposing
  an internal chain identifier as a human-facing name.

### Tests

- Added a regression test that removes every handoff environment variable,
  invokes `/handoff` from a verified generation-3 session, and checks the
  manifest, Claude `--name` argument, Terminal title and result all say
  `DJI Drone 4`.

## [1.2.1] - 2026-08-22

### Added

- A global personal Claude Code **`/handoff` skill** for deliberate manual
  recovery after an automatic transfer fails. It is user-invocable only and is
  installed under `~/.claude/skills/handoff/` without modifying application
  repositories.
- A `manual-handoff --session-id` runtime command. Claude Code supplies the
  exact session ID; the runtime reuses the existing manifest, launcher,
  heartbeat, ownership, parent-stop and notification paths.
- Private minimal live-session snapshots and archived failed-attempt records.
  Unknown status fields, transcript contents and environment dumps are not
  copied into the snapshot.
- Per-attempt notification idempotency keys, so a failed automatic attempt and
  a later failed manual retry can each produce their own durable alert.
- Dedicated tests for snapshot privacy and freshness, duplicate refusal,
  failed-attempt archival, exact-parent refusal, safe skill collision handling,
  idempotent installation and managed uninstall.

### Security

- Manual recovery refuses stale or invalid status, a disabled parent-stop
  policy, an unprovable parent process, an active transfer and a completed
  transfer. A terminally failed attempt is archived and replaced under the same
  per-session lock used by automatic triggers.
- A trigger claim with no transfer or launch record is protected through a
  90-second launcher race window, then may be archived and recovered as an
  orphan. The safety floor is 60 seconds.
- The skill helper calls the runtime with `os.execv` and fixed argv. User skill
  arguments never reach a shell or the runtime. A user-owned `/handoff` skill
  is never overwritten or removed.

### Changed

- Runtime, package metadata and documentation advance to 1.2.1.

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
