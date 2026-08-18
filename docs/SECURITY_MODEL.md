# Security model

Terminal Handoff sits at an awkward junction: it runs constantly, consumes JSON
produced by another process, handles a path to a file full of conversation
history, and ends by constructing a command that opens a Terminal window and
starts an agent. This document sets out what is trusted, what is not, and what
each boundary does about it.

## Trust boundaries

| Input | Trust | Handling |
|---|---|---|
| Status-line JSON | **Untrusted** | Every field validated before use; unknown fields ignored |
| `session_id` | **Untrusted** | Allow-listed `[A-Za-z0-9._-]{8,128}`; used as a filename component only after validation |
| `model.id` | **Untrusted** | Allow-listed `[A-Za-z0-9._:@/\[\]-]{1,128}`; passed as a separate argv element |
| `effort.level` | **Untrusted** | Allow-listed to exactly five values |
| `transcript_path` | **Untrusted** | Fully validated, then used as a path only — never read into context, never executed |
| `workspace.current_dir` | **Untrusted** | Must exist, be a directory, and contain no quote, backslash or control character |
| A wrapped status-line command | **Trusted-by-configuration** | Comes from a Claude settings file the user already controls; run exactly as Claude Code itself would |
| Environment variables | **Semi-trusted** | Only Terminal Handoff's own variables are read; values are range-checked and fall back to defaults |

## The transcript

The transcript is the most sensitive object Terminal Handoff touches. It is
conversation history, and it may contain anything the user discussed.

Validation, in order: present; absolute; no unsafe characters; no `..`
traversal segments; exists; resolves to a regular file; readable; non-empty;
first line parses as JSON (proving JSON Lines). A basename that does not match
`<session_id>.jsonl` is recorded as a warning rather than trusted silently.

After validation the path — and only the path — is recorded in the manifest.
The transcript is:

- never executed or sourced
- never interpolated into a shell command
- never passed as a command-line argument
- never copied to the clipboard
- never written to a log
- never read into the successor's main context

The successor reads it through a subagent, which returns a summary. The
transcript's bytes never cross into the main context window.

## Command construction

Three distinct quoting boundaries, each handled at its own layer:

1. **argv.** The launch command is built as a Python list. The model ID and
   effort level are separate elements. No string concatenation into a shell
   command occurs at this layer.
2. **The generated shell script.** Every interpolated value passes through
   `shlex.quote`. This matters concretely: real model IDs such as
   `claude-opus-5[1m]` contain brackets, which zsh treats as a glob pattern.
   Unquoted, the launch fails with `no matches found`.
3. **AppleScript.** Backslashes and double quotes are escaped for the string
   literal. Paths containing quotes or control characters never reach this
   layer — they are rejected during validation, because escaping around a
   hostile path is a worse strategy than refusing it.

A denylist is asserted against the finished argv as defence in depth. If
`--continue`, `-c`, `--resume`, `-r`, `--fork-session`, `--permission-mode`,
`--fallback-model` or any skip-permissions flag appears, the launch is refused
and recorded as a failure.

## Never used

- `eval`
- `shell=True`
- sourcing generated content
- clipboard handover
- world-writable files
- predictable temporary filenames
- transcript-derived commands
- automatic permission bypass
- destructive Git operations

The single use of `/bin/sh -c` is for running a status-line command that the
user's own Claude settings already configured — precisely what Claude Code does
with it. Terminal Handoff neither invents that command nor derives it from any
untrusted input.

## Filesystem

- Directories: `0700`
- Manifests, prompts, logs, state: `0600`
- Writes are atomic: private temporary file, `fsync`, then `os.replace`
- The one-shot trigger claim uses `O_CREAT|O_EXCL`, so concurrency cannot
  produce two launches
- Log rotation is bounded: 1 MB, 5 files

## What is never recorded

Credentials, API keys, tokens, environment dumps, full file contents,
transcript contents, shell history, or unrelated private data. Manifests carry
an explicit `security` block asserting this, and the test suite verifies it by
planting a fake key in the environment and a marker in a transcript, then
asserting neither appears in the manifest, the log or the successor prompt.

## Process listing

The successor's full instructions are written to a `0600` file. Only a short
bootstrap prompt naming that file is passed as an argument, so the instructions
do not appear in `ps` output.

## Permissions and escalation

The successor is launched in the user's normal permission mode. Terminal
Handoff never passes `--dangerously-skip-permissions` or
`--allow-dangerously-skip-permissions`, never sets `--permission-mode`, and
never instructs the successor to bypass a permission prompt. The successor's
instructions explicitly forbid destructive Git operations without clear
authority and require it to treat every existing change as user-owned.

## macOS Automation

Opening the Terminal window requires Automation permission. If it is denied,
`osascript` fails, the failure is logged with its reason, the status line shows
a visible warning, and the outgoing session continues working. Terminal Handoff
does not attempt to bypass, suppress or repeatedly re-prompt for a macOS
security control.

## Network

There is none. Terminal Handoff contains no network code and transmits nothing
off the machine.

## Failure philosophy

Fail closed, and fail visibly. When anything is missing, null, malformed or
unverified, the answer is "do not trigger" — never "guess". When a launch cannot
preserve the model or effort, the answer is "fail and tell the user" — never
"launch something else". A circuit-breaker activation is logged and displayed,
never hidden.
