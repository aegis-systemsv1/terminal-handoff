# Configuration

Everything is configured through environment variables. There is no
configuration file to edit, and Terminal Handoff never edits your shell startup
files.

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

Invalid values fall back to the default rather than failing: a non-numeric
threshold, or one outside 0-100, is ignored and 80 is used.

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
```
