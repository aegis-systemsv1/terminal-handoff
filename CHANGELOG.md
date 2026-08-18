# Changelog

All notable changes to Terminal Handoff are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/aegis-systemsv1/terminal-handoff/releases/tag/v1.0.0
