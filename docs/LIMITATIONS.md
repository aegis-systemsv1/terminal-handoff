# Limitations

Stated plainly. A tool that automates opening agent sessions is only worth
trusting if it is honest about what it cannot do.

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

## 7. Claude Code only

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

## What Terminal Handoff explicitly does not do

- bypass Claude Code permissions
- close, terminate or send keystrokes to the original session
- execute transcript contents
- run destructive Git commands
- modify shell startup files
- modify application repositories
- transmit anything off the machine
