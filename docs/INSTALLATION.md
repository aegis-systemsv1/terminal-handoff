# Installation

## Requirements

| Requirement | Why |
|---|---|
| macOS | `osascript` and Apple Terminal are used to open the successor window |
| Apple Terminal | the only terminal this release drives |
| Claude Code with `--model` and `--effort` | the exact model and effort must be passed to the successor |
| Python 3.9+ | the runtime; the macOS system Python is sufficient |
| Automation permission for Terminal | required to open a window |

There are no third-party Python dependencies.

## Install

```sh
git clone https://github.com/aegis-systemsv1/terminal-handoff.git Terminal-Handoff
cd Terminal-Handoff

./install.sh              # dry run - prints every proposed change, changes nothing
./install.sh --apply      # install, with a confirmation prompt
./install.sh --apply --yes
```

### What the installer checks

It fails closed if any of these is not satisfied:

- the platform is macOS
- a Python 3.9+ interpreter is available
- `claude` is on `PATH`
- `claude --help` advertises both `--model` and `--effort`
- `/usr/bin/osascript` exists
- Apple Terminal is installed
- the existing `~/.claude/settings.json`, if present, is valid JSON and its
  `statusLine` (if any) is a `command` type it can safely wrap

### What the installer changes

| Path | Change |
|---|---|
| `~/.claude/terminal-handoff/` | runtime installed here (`0700`) |
| `~/.claude/terminal-handoff/notifications.json` | private channel configuration; local alerts enabled (`0600`) |
| `~/.claude/settings.json` | `statusLine` added, or an existing one wrapped |
| `~/.claude/CLAUDE.md` | Terminal Handoff instructions appended, idempotently |

Every file is backed up to `~/.claude/terminal-handoff/backups/` with a
timestamped name and a `.source` sidecar before it is touched. Unrelated keys in
your settings are preserved exactly, including non-ASCII characters and the
file's existing trailing-newline convention.

It does **not** modify shell startup files, application repositories, or any
permission mode.

### Custom locations

```sh
TERMINAL_HANDOFF_PREFIX=~/tools/terminal-handoff \
CLAUDE_SETTINGS=~/.claude/settings.json \
PYTHON=/opt/homebrew/bin/python3 \
./install.sh --apply
```

## Verify

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py status
python3 ~/.claude/terminal-handoff/terminal-handoff.py coverage
python3 ~/.claude/terminal-handoff/terminal-handoff.py notifications test --channel local
```

`coverage` lists every Claude settings file on the machine and whether Terminal
Handoff is active there. Anything reported as `OVERRIDE - NOT COVERED` is a
project defining its own `statusLine`; see below.

Then start a new Claude Code session. The status line should end with a badge
such as `TH 12%`.

The notification test should also create one local macOS alert. Signed webhook,
presence-aware routing and Messages/SMS relay remain disabled until you opt in;
configure them with [NOTIFICATIONS.md](NOTIFICATIONS.md).

## Projects with their own status line

Project settings override user settings, so a repository with its own
`statusLine` bypasses the global installation until it is integrated. Integrate
it by wrapping, never replacing:

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py install \
    --settings /path/to/project/.claude/settings.json --skip-claude-md
```

The existing command is preserved and run with the identical stdin bytes; its
output is preserved byte-for-byte and only a short badge is appended. Re-run
`coverage` afterwards to confirm.

## Controlled live test

The supported way to exercise the whole thing — real Terminal windows, real
`osascript`, real process ancestry, a real `SIGTERM` — without starting a Claude
session or consuming a context window:

```sh
python3 scripts/live-handoff-test.py
```

It runs a three-generation chain in a throwaway state directory, proves
`Ranger -> Ranger 2 -> Ranger 3`, proves the exact parent process is stopped
while its Terminal window stays open at a shell prompt, proves an unrelated
Claude process is untouched, and proves that an invalid successor heartbeat
leaves the parent running. A recorded run is in
[LIVE_TEST_EVIDENCE.md](LIVE_TEST_EVIDENCE.md).

### Doing it by hand

To exercise the real launcher once, manually:

1. Create a throwaway git repository and a synthetic transcript somewhere
   temporary. The transcript must be JSON Lines and named `<session-id>.jsonl`.
2. Write a synthetic status payload using the official schema, with
   `used_percentage` above your threshold, pointing `workspace.current_dir` at
   the throwaway repository. `tests/fixtures/at_threshold.json` is a starting
   point.
3. Feed it to the installed status-line command **twice** - the first invocation
   satisfies the stability gate, the second triggers:

   ```sh
   CMD=$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.claude/settings.json")))["statusLine"]["command"])')
   sh -c "$CMD" < payload.json
   sh -c "$CMD" < payload.json
   ```

4. Exactly one Terminal window should open. Its title, and the successor's
   Claude session name, are your session's name plus the generation number —
   `Ranger 2` for a session named `Ranger`. If your payload carried no
   `session_name`, the documented fallback
   `Terminal Handoff <chain-id[:8]> 2` is used instead.
5. Confirm the handoff:

   ```sh
   python3 -c "
   import json, glob
   for path in glob.glob(__import__('os').path.expanduser('~/.claude/terminal-handoff/handoffs/*.json')):
       m = json.load(open(path))
       print(m['successor'])
   "
   ```

   `session_id_is_fresh`, `model_matches`, `effort_matches` and `cwd_matches`
   should all be true, `checks` should be all-true, and `launch_state` should
   reach `completed`.

6. Once the successor is verified, the parent session is asked to exit. Confirm
   what happened:

   ```sh
   python3 ~/.claude/terminal-handoff/terminal-handoff.py status
   ```

   The `transfers` entry should read `TRANSFER_COMPLETE` with `parent_stopped`
   true. The parent's Terminal window stays open at its shell prompt. If it
   reads `TRANSFER_FAILED`, the parent is still running on purpose and the
   record says why — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

   To rehearse without ever signalling anything, set
   `CLAUDE_TERMINAL_HANDOFF_STOP_DRY_RUN=1`, or disable the behaviour with
   `CLAUDE_TERMINAL_HANDOFF_STOP_PARENT=0`.

To rehearse without opening anything, set
`CLAUDE_TERMINAL_HANDOFF_TEST_MODE=1` first; the launch is simulated and the
constructed command is written to `completed/<session-id>.launch.json`.

## Upgrading

Pull, then re-run `./install.sh --apply`. The installer is idempotent: it
recognises its own status-line command under any module name and will not wrap
itself.

Before upgrading, take a snapshot you can return to. The installer backs up
settings files, but it overwrites the runtime in place:

```sh
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$HOME/.claude/terminal-handoff/backups/pre-upgrade-$STAMP"
mkdir -p "$BACKUP" && chmod 700 "$BACKUP"
cp "$HOME/.claude/terminal-handoff/terminal-handoff.py" "$BACKUP/"
cp "$HOME/.claude/terminal-handoff/successor-prompt.md" "$BACKUP/"
cp "$HOME/.claude/terminal-handoff/uninstall.sh" "$BACKUP/"
cp "$HOME/.claude/terminal-handoff/VERSION" "$BACKUP/" 2>/dev/null
cp "$HOME/.claude/settings.json" "$BACKUP/"
cp "$HOME/.claude/CLAUDE.md" "$BACKUP/"
echo "$BACKUP"
```

After upgrading, confirm what is actually installed:

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py version
python3 ~/.claude/terminal-handoff/terminal-handoff.py status
python3 ~/.claude/terminal-handoff/terminal-handoff.py coverage
```

Running Claude Code sessions pick up the new runtime on their next status
refresh. There is nothing to restart.

## Rolling back

Three levels, from least to most disruptive.

**1. Turn off the new behaviour, keep the version.** If parent shutdown is the
only thing you want to undo, set this before starting `claude` — no reinstall
needed:

```sh
export CLAUDE_TERMINAL_HANDOFF_STOP_PARENT=0
```

`CLAUDE_TERMINAL_HANDOFF_DISABLED=1` turns off triggering entirely.

**2. Restore the previous runtime from your snapshot.** The runtime is a single
file plus its prompt template, so this is a copy:

```sh
BACKUP="$HOME/.claude/terminal-handoff/backups/pre-upgrade-<stamp>"
cp "$BACKUP/terminal-handoff.py" "$HOME/.claude/terminal-handoff/terminal-handoff.py"
cp "$BACKUP/successor-prompt.md" "$HOME/.claude/terminal-handoff/successor-prompt.md"
cp "$BACKUP/uninstall.sh"        "$HOME/.claude/terminal-handoff/uninstall.sh"
cp "$BACKUP/VERSION"             "$HOME/.claude/terminal-handoff/VERSION" 2>/dev/null
chmod 700 "$HOME/.claude/terminal-handoff/terminal-handoff.py" \
          "$HOME/.claude/terminal-handoff/uninstall.sh"
chmod 600 "$HOME/.claude/terminal-handoff/successor-prompt.md"
python3 ~/.claude/terminal-handoff/terminal-handoff.py version
```

Or reinstall a published version from source:

```sh
cd /path/to/Terminal-Handoff
git checkout v1.0.1
./install.sh --apply
```

The status-line command in `settings.json` names a path, not a version, so it
keeps working across either route. Existing manifests and chain state stay on
disk; an older runtime ignores the fields it does not know.

**3. Remove it entirely.** See [UNINSTALLATION.md](UNINSTALLATION.md).

## Uninstall

See [UNINSTALLATION.md](UNINSTALLATION.md).
