# Configuration

Handoff behaviour is configured through environment variables. Notification
routing is kept in a private configuration file managed by the
`notifications` CLI. Terminal Handoff never edits your shell startup files.

## Variables

| Variable | Effect | Default |
|---|---|---|
| `CLAUDE_TERMINAL_HANDOFF_DISABLED` | `1` disables all triggering | unset |
| `CLAUDE_TERMINAL_HANDOFF_THRESHOLD` | trigger percentage (0-100) | `80` |
| `CLAUDE_TERMINAL_HANDOFF_TEST_MODE` | `1` simulates the launch; no window opens | unset |
| `CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS` | stop automatic launching after generation N | unlimited |
| `CLAUDE_TERMINAL_HANDOFF_MIN_OBSERVATIONS` | own non-null readings required before eligibility | `2` |
| `CLAUDE_TERMINAL_HANDOFF_COOLDOWN` | seconds between launches | `45` |
| `CLAUDE_TERMINAL_HANDOFF_STORM_MAX` | launches allowed inside the storm window | `3` |
| `CLAUDE_TERMINAL_HANDOFF_STORM_WINDOW` | storm window in seconds | `600` |
| `CLAUDE_TERMINAL_HANDOFF_CIRCUIT_SECONDS` | how long the breaker stays open | `1800` |
| `CLAUDE_TERMINAL_HANDOFF_HOME` | state directory | `~/.claude/terminal-handoff` |
| `CLAUDE_TERMINAL_HANDOFF_CLAUDE_BIN` | explicit `claude` executable | auto-detected |
| `CLAUDE_TERMINAL_HANDOFF_STOP_PARENT` | `0` never stops the parent session after a handoff | enabled |
| `CLAUDE_TERMINAL_HANDOFF_HEARTBEAT_TIMEOUT` | seconds to wait for a verified successor heartbeat | `300` |
| `CLAUDE_TERMINAL_HANDOFF_STOP_GRACE` | seconds to wait for the parent to exit after each `SIGTERM` | `20` |
| `CLAUDE_TERMINAL_HANDOFF_STOP_ATTEMPTS` | `SIGTERM` requests before giving up; never escalates | `2` |
| `CLAUDE_TERMINAL_HANDOFF_STOP_DRY_RUN` | `1` runs the whole shutdown path but sends no signal | unset |
| `CLAUDE_TERMINAL_HANDOFF_TRANSFER_POLL` | supervisor poll interval in seconds | `2` |
| `CLAUDE_TERMINAL_HANDOFF_DISABLE_NOTIFICATIONS` | `1` leaves events queued but does not spawn the delivery worker | unset |
| `TERMINAL_HANDOFF_PRESENCE` | notification routing state: `home`, `away`, `unknown` | presence file, then `home` |
| `TERMINAL_HANDOFF_WEBHOOK_SECRET` | ephemeral webhook signing secret | Keychain lookup |

Invalid values fall back to the default rather than failing: a non-numeric
threshold, or one outside 0-100, is ignored and 80 is used.

## Parent shutdown

Since 1.1.0, once a successor has proved it started correctly, Terminal Handoff
asks the parent Claude session to exit so that only one session continues the
work. The Terminal window itself is never closed; it returns to its shell
prompt.

If verification fails, times out, or the parent process cannot be re-proved, the
parent is left fully operational and the transfer is recorded as
`TRANSFER_FAILED`. To turn the behaviour off entirely and keep both sessions
running:

```sh
export CLAUDE_TERMINAL_HANDOFF_STOP_PARENT=0
```

To exercise the whole path without ever signalling anything — useful when
validating an upgrade:

```sh
export CLAUDE_TERMINAL_HANDOFF_STOP_DRY_RUN=1
```

## Configuration is inherited by successors

Apple Terminal starts a fresh login shell that does not inherit the launcher's
environment, so every `CLAUDE_TERMINAL_HANDOFF_*` variable that is set when a
handoff triggers is written into the successor's launch script explicitly. A
chain therefore keeps the settings you started it with, for every generation.

Webhook secrets use the separate `TERMINAL_HANDOFF_WEBHOOK_SECRET` name and are
never copied into a generated launch script. Store a multi-generation secret in
macOS Keychain; see [NOTIFICATIONS.md](NOTIFICATIONS.md).

## The scope gotcha

The status-line process inherits the environment of the Claude Code session that
spawned it. That has one consequence worth stating plainly:

> **Exporting a variable in one shell does not affect Claude Code sessions that
> are already running**, and does not affect sessions started from a different
> shell or launched from an application icon.

To change behaviour for a session, set the variable **before** starting `claude`
in that shell:

```sh
export CLAUDE_TERMINAL_HANDOFF_THRESHOLD=70
claude
```

Or restart the session after exporting it.

## Enabling and disabling

```sh
export CLAUDE_TERMINAL_HANDOFF_DISABLED=1     # kill switch
unset  CLAUDE_TERMINAL_HANDOFF_DISABLED       # re-enable
```

When disabled, the status line shows `TH disabled` and no trigger evaluation
occurs. Nothing else about your session changes.

## Persisting a setting

Terminal Handoff will not edit `~/.zshrc` or any other startup file for you. If
you want a permanent change, add the line yourself. For example, to make the
threshold 75% for all future shells:

```sh
echo 'export CLAUDE_TERMINAL_HANDOFF_THRESHOLD=75' >> ~/.zshrc
```

Review that line before running it, and restart your shell afterwards.

## Choosing a threshold

- **80% (default)** leaves useful room to finish the current thought and write a
  clean handoff.
- **Lower (70-75%)** suits long agentic runs with large tool outputs, where the
  last 20% fills quickly.
- **Higher (85-90%)** squeezes more from each session but risks the parent
  hitting compaction before the successor is useful.

The threshold is compared against `.context_window.used_percentage` only.

## Generation limits

Unlimited by default. To cap a chain:

```sh
export CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS=10
```

When generation N is reached, launching stops and the status line shows
`TH blocked`. The session keeps working normally; it simply will not spawn a
successor.

## Storm protection tuning

The defaults allow 3 launches in 10 minutes before the breaker opens for 30
minutes. Loosen them only if you understand what a runaway loop would cost:

```sh
export CLAUDE_TERMINAL_HANDOFF_STORM_MAX=5
export CLAUDE_TERMINAL_HANDOFF_STORM_WINDOW=900
```

Clear an open breaker with:

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py reset-circuit
```

This clears both the flag and the counting window; clearing the flag alone would
let already-counted launches re-trip it immediately.

## Inspecting state

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py status
python3 ~/.claude/terminal-handoff/terminal-handoff.py coverage
python3 ~/.claude/terminal-handoff/terminal-handoff.py notifications status
```

## Notification configuration

The installer creates `~/.claude/terminal-handoff/notifications.json` with
mode `0600`. Local macOS alerts are enabled; webhook and Messages delivery are
disabled until explicitly configured.

```sh
TH="$HOME/.claude/terminal-handoff/terminal-handoff.py"
python3 "$TH" notifications configure --enable-local
python3 "$TH" notifications presence --presence away
python3 "$TH" notifications test --channel local
```

Use the CLI instead of placing credentials in the file. It records only a
Keychain service/account reference or an environment-variable name, never the
webhook secret itself. Full channel, retry, presence and SMS setup is in
[NOTIFICATIONS.md](NOTIFICATIONS.md).
