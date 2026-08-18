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
git clone <your-repo-url> Terminal-Handoff
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
```

`coverage` lists every Claude settings file on the machine and whether Terminal
Handoff is active there. Anything reported as `OVERRIDE - NOT COVERED` is a
project defining its own `statusLine`; see below.

Then start a new Claude Code session. The status line should end with a badge
such as `TH 12%`.

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

Automated tests never open a window. To exercise the real launcher once:

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

4. Exactly one Terminal window should open, titled
   `Terminal Handoff, Generation 2`.
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
   should all be true, and `launch_state` should reach `completed`.

To rehearse without opening anything, set
`CLAUDE_TERMINAL_HANDOFF_TEST_MODE=1` first; the launch is simulated and the
constructed command is written to `completed/<session-id>.launch.json`.

## Upgrading

Pull, then re-run `./install.sh --apply`. The installer is idempotent: it
recognises its own status-line command under any module name and will not wrap
itself.

## Uninstall

See [UNINSTALLATION.md](UNINSTALLATION.md).
