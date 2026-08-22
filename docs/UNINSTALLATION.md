# Uninstallation

## Quick

```sh
~/.claude/terminal-handoff/uninstall.sh            # dry run: shows every change
~/.claude/terminal-handoff/uninstall.sh --apply    # apply, with confirmation
~/.claude/terminal-handoff/uninstall.sh --apply --yes
```

The dry run is the default. It changes nothing and prints exactly what it would
do.

## What it removes

| Target | Action |
|---|---|
| `~/.claude/settings.json` | removes the `statusLine` key, or restores whatever was there before |
| project `settings.json` files it integrated | restores the original `statusLine` exactly |
| `~/.claude/CLAUDE.md` | removes only the delimited Terminal Handoff block |

Restoration is exact. The original `statusLine` object is recorded at install
time, including its key order and the file's trailing-newline convention, so an
uninstall reproduces the original file byte-for-byte. The resulting JSON is
validated before it is written.

## What it keeps

- handoff manifests (`handoffs/`)
- logs (`logs/`)
- configuration backups (`backups/`)
- notification configuration and presence (`notifications.json`, `notifications/`)
- notification delivery history (`outbox/`)

These are retained deliberately: they are the record of what happened and what
your configuration looked like beforehand. Remove them yourself when you are
satisfied:

```sh
rm -rf ~/.claude/terminal-handoff
```

## What it never touches

- Claude transcripts
- any application repository
- unrelated Claude settings
- shell startup files

## Manual rollback

If you prefer to do it by hand, or the uninstaller is unavailable:

1. **Global settings** — delete the `"statusLine"` key from
   `~/.claude/settings.json`, or restore it from
   `~/.claude/terminal-handoff/backups/`. Each backup has a `.source` sidecar
   naming the file it came from.

2. **Project settings** — for any project you integrated, set
   `statusLine.command` back to its original value. The original is recorded in:
   ```sh
   python3 -m json.tool ~/.claude/terminal-handoff/state/installed.json
   ```
   under `targets.<path>.original_statusLine`.

3. **`CLAUDE.md`** — delete everything between and including:
   ```
   <!-- BEGIN TERMINAL HANDOFF -->
   <!-- END TERMINAL HANDOFF -->
   ```

4. **Validate** the JSON you edited:
   ```sh
   python3 -m json.tool ~/.claude/settings.json > /dev/null && echo valid
   ```

5. **Remove the runtime**, optionally:
   ```sh
   rm -rf ~/.claude/terminal-handoff
   ```

Start a new Claude Code session afterwards; settings are read at session start.

## Verifying removal

```sh
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json'))).get('statusLine'))"
grep -c "TERMINAL HANDOFF" ~/.claude/CLAUDE.md
```

The first should print `None`; the second `0`.

## Temporarily disabling instead

If you only want it off for a while, do not uninstall:

```sh
export CLAUDE_TERMINAL_HANDOFF_DISABLED=1
```

set before starting `claude`. The status line shows `TH disabled` and nothing
triggers.
