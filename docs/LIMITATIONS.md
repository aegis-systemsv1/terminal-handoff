# Limitations

Stated plainly. A tool that automates opening agent sessions is only worth
trusting if it is honest about what it cannot do.

## 0. The parent is only stopped when its process can be proved

Terminal Handoff binds the exact Claude Code process by walking the status-line
process's real ancestry, and re-proves that binding immediately before
signalling it. If it cannot — an unusual host topology, a Claude process more
than six ancestry levels away, one whose working directory does not match the
session's, or a PID that has been reused — the handoff still happens and the
parent is **left running**.

That is the safe direction, but it means two sessions can coexist. The successor
is told explicitly not to mutate anything until the transfer state authorises
it, and `terminal-handoff.py status` shows every transfer's state.

There is also no `SIGKILL` path. A parent that ignores `SIGTERM` is recorded as
`parent_stop_unconfirmed` and left alone; you close it yourself.

One window cannot be closed entirely. POSIX signals address a PID, not a
specific process, and macOS offers no handle equivalent that would let a process
be signalled by identity. Terminal Handoff re-proves the PID, start time, UID,
executable name, terminal and working directory with no wait between the check
and the signal, which reduces the window to microseconds and makes any
substituted process detectable in the general case — but it does not reduce it
to zero. Recording the start time is what makes PID reuse detectable at all;
without it, the reuse would be silent.

The practical exposure is small: PIDs are not reused that quickly on macOS, and
the replacement would have to be another process of yours whose executable is
also named `claude`, on the same TTY, in the same directory. It is stated here
because "we check immediately before signalling" is a mitigation, not a proof.

## 0a. A chain's base name is immutable

The human-facing base name is captured once, at generation 1, from
`.session_name`. Renaming a successor mid-chain does not rewrite it: generation
4 of a chain created as `Ranger` is `Ranger 4`, whatever you renamed generation
3 to. Generation numbers are never inferred from a visible name, so a session
legitimately called `Project 42` hands off to `Project 42 2`.

## 0b. Manual `/handoff` requires a fresh status refresh

The fail-safe does not ask Claude to reconstruct its own launch facts. It uses
the latest minimal snapshot written by the status line for Claude Code's exact
`${CLAUDE_SESSION_ID}`. A snapshot older than 30 seconds is refused. Wait for
the status line to refresh and invoke `/handoff` again.

Unlike the automatic path, manual recovery also refuses to open a successor if
the exact parent Claude process cannot be bound. This avoids turning a recovery
command into an unmanaged duplicate session. `/handoff` is a local personal
skill; Claude cloud sessions do not read skills from your Mac.

## 1. `ultracode` cannot be preserved

`claude --effort ultracode` is accepted by the CLI, but it is not an effort
level. It resolves to `xhigh` **plus a separate internal boolean**, and that
boolean is never exposed in the status-line JSON — the schema documents
`.effort.level` as one of `low`, `medium`, `high`, `xhigh`, `max`.

Consequence: a parent running ultracode reports `xhigh`, so its successor starts
at `xhigh` with ultracode off. Every manifest records
`effort.ultracode: "undetectable"`.

Terminal Handoff will not guess. Inventing an effort value would be worse than
the honest downgrade, because you would not know which you had.

## 2. Only officially exposed effort values are preserved

`low`, `medium`, `high`, `xhigh`, `max`. Anything else reported in
`.effort.level` is treated as invalid and blocks the handoff rather than being
passed through.

## 3. Model validity cannot be checked before launch

`claude --model <invalid>` exits 0 at argument-parse time; an invalid model only
fails once the session starts, after the window has opened.

Mitigation: a launch is recorded as `launched`, never `completed`, until the
successor's own heartbeat confirms its model, effort, working directory and
fresh session ID. If they do not match, the manifest records the mismatch and
the successor is instructed to stop and report it rather than carry on.

## 4. A natural 80% live trigger has not been exercised

Reaching 80% legitimately consumes roughly 160,000 tokens. That was not done.

**What is tested with synthetic payloads** (built from the official schema):
threshold comparison at 79%, 79.99%, 80% and 81%; null and missing percentages;
rate-limit versus context percentage; every validation and safety gate.

**What was live-tested end to end**: the real `osascript` Terminal launch;
exactly one window opening; a genuinely fresh Claude session; exact model
preservation including a bracketed ID; exact effort preservation; the correct
working directory; the manifest being read; transcript analysis through an
isolated subagent with zero transcript content entering the successor's main
context; independent repository verification; the continuation report; and the
successor heartbeat completing the lifecycle.

The gap is narrow but real, and it is this: the *arithmetic* of the threshold
has not been observed against a genuinely full context window, only against
payloads asserting one.

## 5. Project-level status lines override the global one

Claude Code resolves project settings over user settings. A repository defining
its own `statusLine` bypasses the global installation until integrated
explicitly. Run `coverage` after adding one; it reports every configuration and
whether Terminal Handoff is active there.

## 6. Apple Terminal only

The launcher drives Apple Terminal through AppleScript. iTerm2, Ghostty,
WezTerm, Warp, Alacritty, Kitty and tmux are **not implemented**. Nothing about
the detector is macOS-specific, but the launch step is.

## 7. Claude Code and Grok only (Grok is not at parity)

Terminal Handoff depends on Claude Code's status-line JSON contract and its
`--model` / `--effort` flags. **Codex and other agent CLIs are not implemented
and not verified.** Any claim of compatibility with them would be unfounded.

## 8. Claude Code auto-updates

The status-line schema is a documented interface, not a stable API contract. If
a future version renames or restructures a field, Terminal Handoff fails
closed — `TH blocked`, never a wrong trigger. Re-run the suite after a major
upgrade.

## 9. macOS Automation permission

Opening a window requires Automation permission. If it is denied or revoked,
launches fail with a logged reason and a visible badge, and the outgoing session
keeps working. Terminal Handoff never attempts to work around a macOS security
control.

## 10. The successor's judgement is not guaranteed

Terminal Handoff guarantees the *mechanism*: a fresh session, the same model and
effort, a validated manifest, an isolated transcript analysis and instructions
to verify before acting. It cannot guarantee that the successor reasons
perfectly about what it finds. The instructions require it to verify
independently, treat live state as authoritative, treat every existing change as
user-owned, and stop and ask when evidence conflicts or authority is missing —
but that is a strong prompt, not an enforcement mechanism.

Treat a successor like a capable colleague who has read a good handover note:
worth trusting, still worth checking.

## 11. Notification acceptance is not human receipt

The outbox can prove that macOS, Messages or an HTTPS endpoint accepted a
delivery request. It cannot prove a carrier delivered an SMS, that a push
provider reached a device, or that a person read the alert. Webhook delivery is
at least once and consumers must deduplicate `event_id`; a crash after provider
acceptance but before the local ledger write can repeat an event.

Presence is explicit rather than inferred. Set it through the CLI or connect an
existing presence service. Terminal Handoff does not inspect GPS, Wi-Fi, camera,
keyboard activity or private home-automation state.

Messages delivery depends on macOS Automation permission and, for non-iMessage
recipients, iPhone Text Message Forwarding, carrier and region support. A signed
webhook to a private gateway is the more controllable option for production SMS
or messaging apps.

## What Terminal Handoff explicitly does not do

- bypass Claude Code permissions
- close, terminate or send keystrokes to the original session
- execute transcript contents
- run destructive Git commands
- modify shell startup files
- modify application repositories
- transmit anything off the machine unless the user explicitly enables the
  signed webhook or Messages notification adapter

## Remote control

See [REMOTE_CONTROL.md](REMOTE_CONTROL.md#limitations): waking is a bounded
long-poll rather than a push, native Claude permission prompts cannot be
answered remotely, `--setting-sources` is undocumented and verified per Claude
version, ORPHANED sessions are not relaunched, and Mac reboot recovery is not
implemented.

## Grok (1.5.0)

* **Not at parity with Claude.** No automatic context handoff (Grok's own session/compaction applies), no
  Remote Control, and permissions are Grok's own system.
* **Ask mode is not verifiable from outside Grok.** See the [security model](SECURITY_MODEL.md#grok):
  Terminal Handoff fails closed rather than trust an unverifiable setting.
* **Live acceptance pending.** Tested against a scripted ACP agent, not a live Grok model turn.
* **At-least-once on a crash.** An instruction in flight when Grok's process dies is offered again once the
  session is reloaded. One interrupted by STOP is not re-run.
* **`session/load` replays history**, which Terminal Handoff discards; it relies on Grok honouring ACP
  `loadSession`.
* **Agent switching mid-session, and other agents (for example Codex), are not implemented.**

## Checkpoint (`th checkpoint`)

* **No Codex support.** `--agent-type codex` records a caller-supplied label only - nothing is verified or
  captured about an actual Codex session.
* **`--ai-summary` and `--compact` cost real, billed API calls.** Terminal Handoff was previously
  zero-API-cost; this is a deliberate, opt-in exception.
* **Real LLM output has inherent variance.** The AI summary worker has, in real testing, produced clean
  JSON, JSON prefaced with prose (handled), and occasionally an empty response (degrades to
  `unavailable`, never fabricated). This is not a fixed reliability number.
* **Smart Compact's VERIFY only catches structurally cross-checkable claims** - it compares
  `tests_performed` against a real exit code and `files_in_progress` against the actual working tree,
  both already present elsewhere in the same checkpoint. It cannot catch a narrative misjudgement in the
  AI summary's own prose (confirmed in real testing: an early instruction later legitimately superseded
  was described as "violated"; the prompt was fixed to reduce this, not eliminate the underlying
  limitation - see [CHECKPOINT.md](CHECKPOINT.md#known-limitations)).
* **The automated test suite never starts a real Claude Code session or AI worker**, consistent with the
  rest of Terminal Handoff's tests. Real-worker defects (like the two above) are only caught by manual
  acceptance runs against a real transcript.

## Resume (`th resume`)

* **No integration with the live chain-generation registry.** Resume reads an existing chain record for
  a checkpoint's session id, when one exists, for display only - it never calls
  `record_chain_generation()` and never advances a generation counter. See
  [RESUME.md](RESUME.md#known-limitations) for why this is a deliberate boundary, not an omission.
* **No heartbeat or ownership-transfer verification**, unlike automatic/manual handoff - resume reports
  success based on the Terminal window having been opened, not on the successor proving it is alive.
* **Repository-drift detection compares filenames, never contents.** A file modified without being
  added to or removed from the working tree's tracked/staged/untracked lists will not show up in the
  `working_tree_filenames` drift field specifically (its effect on `dirty` still will).
  `git.branch`/`head_sha`/`dirty` and filename-set drift are the only fields compared.
* **`repository_identity`'s VERIFIED/CHANGED status is object reachability, not path equality** - it
  checks whether the checkpoint's recorded `head_sha` still exists in the target repository's history,
  not whether the recorded and live paths are the same string (which can differ cosmetically, e.g. a
  `/tmp` → `/private/tmp` symlink, for the exact same repository).
* **No Codex integration.** Out of scope for this slice.
* **Real-worker acceptance was demonstrated with a one-shot `claude -p` call standing in for the
  successor**, not a full interactive GUI Terminal.app session, to avoid opening an uncontrolled window
  during automated acceptance testing. The actual window-opening code path
  (`launch_resume_terminal`) is covered by dedicated tests with an injected subprocess double, and by
  manual verification, not by the same real-transcript acceptance run.
