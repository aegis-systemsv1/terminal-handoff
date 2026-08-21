# Troubleshooting

Start here:

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py status
tail -20 ~/.claude/terminal-handoff/logs/terminal-handoff.log
```

## The parent session did not stop after a handoff

By design, that means it could not be proved safe to stop it. Look at the
transfer record:

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py status
cat ~/.claude/terminal-handoff/transfers/<parent-session-id>.json
```

The `state` and the last `history` entry give the exact reason:

| State | Meaning |
|---|---|
| `LAUNCHING` | no verified successor heartbeat yet; the parent still owns the work |
| `SUCCESSOR_VERIFIED` | the successor is proved; the shutdown request is next |
| `PARENT_STOP_REQUESTED` | `SIGTERM` sent; waiting for the parent to exit |
| `TRANSFER_COMPLETE` | the parent stopped; the successor owns the work |
| `TRANSFER_FAILED` | the parent is still running, and the reason says why |

Common reasons, all deliberate:

- *no verified successor heartbeat within Ns* — the successor never started, or
  started with the wrong model, effort, directory, chain or generation. Check
  `failed_checks` in the parent's manifest.
- *the parent Claude process was not bound at trigger time* — its ancestry could
  not be traced; see [LIMITATIONS.md](LIMITATIONS.md).
- *parent process identity could not be re-proved* — the PID was reused, the
  terminal changed, or the working directory moved.
- *parent pid N did not exit* — it ignored `SIGTERM`. Terminal Handoff does not
  escalate. Close that session yourself.

To stop trying entirely, `export CLAUDE_TERMINAL_HANDOFF_STOP_PARENT=0`.

## The successor is named `Terminal Handoff <hex> 2`

That is the documented fallback used when the status-line JSON carried no
`.session_name`. Name the session (`/rename`) before it hands off and the chain
will use that name instead. Terminal Handoff will not substitute a repository or
directory name.

## No status line appears at all

1. Confirm the setting exists:
   ```sh
   python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json'))).get('statusLine'))"
   ```
2. Confirm the settings file is valid JSON. Claude Code silently ignores
   settings that fail validation in non-interactive mode.
3. Confirm you started a **new** session. Settings are read at session start.
4. Run the command by hand with a fixture:
   ```sh
   python3 ~/.claude/terminal-handoff/terminal-handoff.py statusline < tests/fixtures/below_threshold.json
   ```

## The status line shows another project's output but no `TH` badge

That project defines its own `statusLine` and overrides the global one. Check:

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py coverage
```

Anything listed as `OVERRIDE - NOT COVERED` needs integrating:

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py install \
    --settings /path/to/project/.claude/settings.json --skip-claude-md
```

## `TH ready` never becomes a percentage

`.context_window.used_percentage` is `null` until the first API response, and
again immediately after compaction. This is expected. Send a message and it will
populate.

## `TH blocked`

Validation failed. The log records exactly which check:

```sh
grep '"event": "blocked"' ~/.claude/terminal-handoff/logs/terminal-handoff.log | tail -5
```

Common causes:

| Reason | Meaning |
|---|---|
| `transcript_path does not exist` | the transcript was moved or deleted |
| `transcript is not JSONL` | the file is not JSON Lines |
| `model.id missing or invalid` | the model ID contains characters outside the allow-list |
| `effort.level invalid` | an unexpected effort value was reported |
| `workspace.current_dir ... unsafe` | the working directory contains a quote, backslash or control character |

Terminal Handoff blocking is the safe outcome; it will not trigger on data it
cannot verify.

## No Terminal window opened at the threshold

Check the sequence in the log:

```sh
tail -30 ~/.claude/terminal-handoff/logs/terminal-handoff.log
```

- `trigger_claimed` absent → a gate stopped it; run `status` and check the
  breaker, cooldown and `MIN_OBSERVATIONS`.
- `trigger_claimed` present, `launch_begin` absent → the detached launcher did
  not start; check that the runtime file is still executable.
- `launch_failed` with `reason: osascript` → Automation permission. See below.
- `launch_failed` with `claude executable not found` → set
  `CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN`.

## macOS Automation permission

Opening a window requires permission for the controlling process to control
Terminal. Test it:

```sh
osascript -e 'tell application "Terminal" to count windows'
```

- A number → permission granted.
- An error mentioning `-1743` or "Not authorised" → denied. Grant it in
  **System Settings → Privacy & Security → Automation**, enabling Terminal
  under the controlling application.

Terminal Handoff will not attempt to work around this.

## `TH circuit open`

The storm breaker tripped: three launches inside ten minutes. It expires on its
own after thirty minutes, or:

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py reset-circuit
```

Then look at *why* three launches happened in ten minutes — that is usually the
real bug.

## `TH handed off` but I want another successor

One trigger per session, by design. Continue in the successor, or start a fresh
session. Deleting the marker in `triggered/` is possible but defeats the
protection that stops runaway windows.

## The successor started with the wrong model or effort

Check what it observed:

```sh
python3 -c "
import json, glob, os
for p in sorted(glob.glob(os.path.expanduser('~/.claude/terminal-handoff/handoffs/*.json'))):
    s = json.load(open(p))['successor']
    print(s.get('model_matches'), s.get('effort_matches'), s.get('observed_model_id'))
"
```

If `model_matches` is false, the model ID reported by the parent was not
accepted by the CLI. Terminal Handoff does not fall back silently — the
mismatch is recorded and the successor is instructed to stop and report it.

If you were running `ultracode`, `xhigh` is expected: ultracode is not
detectable from status-line data. See [LIMITATIONS.md](LIMITATIONS.md).

## The successor read the whole transcript into its context

It should not. Its instructions require delegating to a subagent. If it did,
check that the prompt file is intact:

```sh
grep -c "context-isolated subagent" ~/.claude/terminal-handoff/prompts/successor-*.md
```

## Everything is broken; how do I turn it off now?

```sh
export CLAUDE_TERMINAL_HANDOFF_DISABLED=1
```

in any shell before starting `claude`, or uninstall entirely — see
[UNINSTALLATION.md](UNINSTALLATION.md). Uninstalling never touches your
transcripts or repositories.
