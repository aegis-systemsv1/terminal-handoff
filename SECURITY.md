# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.1.x | Yes |
| 1.0.x | Security fixes only |

## Reporting a vulnerability

Please report security issues privately through GitHub's **Report a
vulnerability** flow on this repository's Security tab, rather than opening a
public issue.

Include the version, macOS and Claude Code versions, a description of the
issue, and a reproduction if you have one. Please do not include real
transcripts, manifests or session identifiers in a report; a redacted synthetic
example is always sufficient.

## Threat model in brief

Terminal Handoff runs on every status-line refresh, receives JSON produced by
another process, reads a transcript path, and constructs a command that opens a
Terminal window. Every one of those is treated as an untrusted boundary.

- **Status-line JSON is untrusted data.** Every field is validated before use.
  A missing, null, malformed or out-of-range context percentage never triggers a
  handoff.
- **The transcript is never executed.** It is validated (absolute path, exists,
  regular file, readable, non-empty, no traversal, parses as JSON Lines) and
  then referenced by path only. It is never sourced, never interpolated into a
  shell command, never passed as an argument, never copied to the clipboard and
  never written to a log.
- **Model IDs are untrusted input.** They are allow-listed against
  `[A-Za-z0-9._:@/\[\]-]` and passed as separate argv elements, never as shell
  text.
- **Effort levels are allow-listed** to `low`, `medium`, `high`, `xhigh`, `max`.
- **Paths containing `"`, `\` or control characters are rejected** rather than
  escaped. Everything else is quoted with `shlex.quote` in generated shell
  scripts and escaped for AppleScript.
- **No `eval`**, no `shell=True`, no sourcing of generated content, no clipboard
  handover, no predictable temporary files.
- **Least privilege on disk.** Directories `0700`; manifests, prompts and logs
  `0600`. Files are written atomically via a private temporary file and
  `os.replace`.
- **No secrets are stored.** Manifests and logs contain no credentials, API
  keys, tokens, environment dumps, file contents, transcript contents or shell
  history.
- **No permission escalation.** The successor runs in the user's normal
  permission mode. Terminal Handoff never passes
  `--dangerously-skip-permissions`, never changes the permission mode, and never
  runs a destructive Git command.
- **Session names are untrusted text.** They are stripped of control
  characters, prevented from beginning with `-`, bounded in length, then passed
  as a single argv element and escaped for AppleScript. Unicode is preserved; a
  name can never become a command.
- **Exactly one process may ever be signalled.** Since 1.1.0 Terminal Handoff
  stops the parent Claude session after a verified successor heartbeat. It binds
  that one process by tracing the status-line process's real ancestry, records
  its PID, start time, TTY, UID, executable name and working directory, and
  re-proves every one of them immediately before signalling. It sends one
  `SIGTERM`, at most twice, and **never escalates to `SIGKILL`**. There is no
  `pkill`, no `killall`, no process-name matching, no process-group signalling
  and no Terminal window targeting. If anything cannot be proved, nothing is
  signalled. See [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

## What Terminal Handoff deliberately does not do

- It does not bypass Claude Code's permission system.
- It does not close any Terminal window, and it never sends keystrokes to a
  session.
- It does not terminate any session other than the one parent process it bound
  at trigger time, and only after that successor has been verified. Unrelated
  Claude sessions are never targeted, and a parent that cannot be proved is left
  running.
- It does not modify shell startup files.
- It does not modify application repositories.
- It does not transmit anything off the machine. There is no network code.

## Automation permission

Opening a Terminal window requires macOS Automation permission for the
controlling process. If that permission is denied or revoked, launches fail with
a logged reason and a visible `TH blocked` badge; the outgoing session remains
fully operational. Terminal Handoff never attempts to work around a macOS
security control.
