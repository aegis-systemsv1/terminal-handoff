#!/bin/sh
#
# Terminal Handoff installer
#
# Installs Terminal Handoff into the current user's Claude Code configuration.
# Portable: no hard-coded usernames, home directories or project paths.
#
# Usage:
#   ./install.sh                 dry run - show every proposed change, change nothing
#   ./install.sh --apply         perform the installation
#   ./install.sh --apply --yes   perform it without the interactive confirmation
#   ./install.sh --help
#
# Environment:
#   TERMINAL_HANDOFF_PREFIX   install directory (default: $HOME/.claude/terminal-handoff)
#   CLAUDE_SETTINGS           user settings file (default: $HOME/.claude/settings.json)
#   PYTHON                    interpreter to use (default: first suitable python3 found)
#
# This script never uses eval, never modifies a shell startup file, never
# modifies an application repository, and never enables a permission bypass.

set -eu

APPLY=0
ASSUME_YES=0

for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --dry-run) APPLY=0 ;;
        -h|--help)
            sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'Terminal Handoff: unknown argument: %s\n' "$arg" >&2
            exit 2
            ;;
    esac
done

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
PREFIX=${TERMINAL_HANDOFF_PREFIX:-"$HOME/.claude/terminal-handoff"}
SETTINGS=${CLAUDE_SETTINGS:-"$HOME/.claude/settings.json"}

fail() {
    printf 'Terminal Handoff: %s\n' "$1" >&2
    printf 'Installation aborted; nothing was changed.\n' >&2
    exit 1
}

note() { printf '  %s\n' "$1"; }

printf 'Terminal Handoff installer\n'
printf '==========================\n\n'

# ---------------------------------------------------------------- prerequisites
printf 'Checking prerequisites\n'

[ "$(uname -s)" = "Darwin" ] || fail "macOS is required (this release targets macOS and Apple Terminal)."
note "macOS $(sw_vers -productVersion 2>/dev/null || echo 'detected')"

if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -x /usr/bin/python3 ]; then
    PY=/usr/bin/python3
else
    PY=$(command -v python3 || true)
fi
[ -n "$PY" ] && [ -x "$PY" ] || fail "python3 was not found. Python 3.9 or later is required."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail "Python 3.9 or later is required (found $("$PY" -V 2>&1))."
note "python $("$PY" -V 2>&1 | awk '{print $2}') at $PY"

CLAUDE_BIN=$(command -v claude || true)
[ -n "$CLAUDE_BIN" ] || fail "the 'claude' executable was not found on PATH. Claude Code is required."
note "claude at $CLAUDE_BIN"

CLAUDE_VERSION=$("$CLAUDE_BIN" --version 2>/dev/null | head -1 || echo 'unknown')
note "claude version: $CLAUDE_VERSION"

CLAUDE_HELP=$("$CLAUDE_BIN" --help 2>/dev/null || true)
case "$CLAUDE_HELP" in
    *--model*) note "claude supports --model" ;;
    *) fail "this Claude Code build does not advertise --model; the exact model could not be preserved." ;;
esac
case "$CLAUDE_HELP" in
    *--effort*) note "claude supports --effort" ;;
    *) fail "this Claude Code build does not advertise --effort; the exact effort could not be preserved." ;;
esac

[ -x /usr/bin/osascript ] || fail "/usr/bin/osascript was not found; Terminal Handoff cannot open a Terminal window."
note "osascript present"

if [ -d /System/Applications/Utilities/Terminal.app ] || [ -d /Applications/Utilities/Terminal.app ]; then
    note "Apple Terminal present"
else
    fail "Apple Terminal was not found. This release supports Apple Terminal only."
fi

# The status-line contract this release depends on.
printf '\nRequired status-line fields (Claude Code supplies these on stdin)\n'
for field in \
    ".context_window.used_percentage" \
    ".session_id" \
    ".transcript_path" \
    ".workspace.current_dir" \
    ".model.id" \
    ".effort.level"
do
    note "$field"
done
printf '  Terminal Handoff fails closed if any of these is missing or unverified.\n'

# ---------------------------------------------------------------- plan
printf '\nProposed changes\n'
note "install runtime to:   $PREFIX"
note "configure statusLine: $SETTINGS"
note "append instructions:  $HOME/.claude/CLAUDE.md"

if [ -f "$SETTINGS" ]; then
    EXISTING=$("$PY" - "$SETTINGS" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as handle:
        data = json.load(handle)
except Exception:
    print("UNREADABLE")
    raise SystemExit(0)
import re
line = (data or {}).get("statusLine")
if not line:
    print("NONE")
elif isinstance(line, dict) and line.get("type") == "command":
    command = str(line.get("command", ""))
    if re.search(r"terminal[-_]handoff", command, re.IGNORECASE):
        print("ALREADY:" + command)
    else:
        print("COMMAND:" + command)
else:
    print("OTHER")
PYEOF
)
    case "$EXISTING" in
        NONE) note "no existing status line - one will be created" ;;
        UNREADABLE) fail "$SETTINGS is not valid JSON. Fix or move it, then re-run." ;;
        OTHER) fail "$SETTINGS has a statusLine of an unexpected shape; refusing to replace it." ;;
        ALREADY:*) note "Terminal Handoff is already configured here; the status line will be left as it is:"
                   printf '      %s\n' "${EXISTING#ALREADY:}" ;;
        COMMAND:*) note "existing status line will be WRAPPED, not replaced:"
                   printf '      %s\n' "${EXISTING#COMMAND:}" ;;
    esac
else
    note "no $SETTINGS yet - it will be created"
fi

if [ "$APPLY" -eq 0 ]; then
    printf '\nDry run complete. Nothing was changed.\n'
    printf 'Re-run with --apply to install.\n'
    exit 0
fi

if [ "$ASSUME_YES" -eq 0 ]; then
    printf '\nApply these changes? [y/N] '
    read -r reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) printf 'Aborted. Nothing was changed.\n'; exit 0 ;;
    esac
fi

# ---------------------------------------------------------------- install
printf '\nInstalling\n'
umask 077
mkdir -p "$PREFIX"
chmod 700 "$PREFIX"

cp "$SRC_DIR/src/terminal_handoff/core.py" "$PREFIX/terminal-handoff.py"
cp "$SRC_DIR/src/terminal_handoff/templates/successor-prompt.md" "$PREFIX/successor-prompt.md"
cp "$SRC_DIR/uninstall.sh" "$PREFIX/uninstall.sh"
cp "$SRC_DIR/README.md" "$PREFIX/README.md" 2>/dev/null || true
cp "$SRC_DIR/VERSION" "$PREFIX/VERSION" 2>/dev/null || true
cp "$SRC_DIR/CHANGELOG.md" "$PREFIX/CHANGELOG.md" 2>/dev/null || true
chmod 700 "$PREFIX/terminal-handoff.py" "$PREFIX/uninstall.sh"
chmod 600 "$PREFIX/successor-prompt.md"
[ -f "$PREFIX/VERSION" ] && chmod 600 "$PREFIX/VERSION"
[ -f "$PREFIX/CHANGELOG.md" ] && chmod 600 "$PREFIX/CHANGELOG.md"
note "runtime installed to $PREFIX"

mkdir -p "$(dirname "$SETTINGS")"
"$PY" "$PREFIX/terminal-handoff.py" install --settings "$SETTINGS" --tag install || \
    fail "configuration step failed; see the output above."

"$PY" - "$SETTINGS" <<'PYEOF' || exit 1
import json, sys
with open(sys.argv[1]) as handle:
    json.load(handle)
print("  settings JSON validated")
PYEOF

printf '\nInstalled.\n\n'
printf 'Installed version: %s\n' "$(cat "$PREFIX/VERSION" 2>/dev/null || echo unknown)"
printf 'Verify with:\n'
printf '  %s %s/terminal-handoff.py version\n' "$PY" "$PREFIX"
printf '  %s %s/terminal-handoff.py status\n' "$PY" "$PREFIX"
printf '  %s %s/terminal-handoff.py coverage\n' "$PY" "$PREFIX"
printf '\nStart a new Claude Code session to see the status line.\n'
printf 'Uninstall with: %s/uninstall.sh\n' "$PREFIX"
