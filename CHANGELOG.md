# Changelog

All notable changes to Terminal Handoff are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
