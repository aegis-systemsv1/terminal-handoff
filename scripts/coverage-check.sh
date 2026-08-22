#!/bin/sh
#
# Privacy, secret and personal-path scan for the Terminal Handoff source tree.
# Exits non-zero if anything that must never be published is present.

set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

FAILURES=0
report() { printf '  FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
ok()     { printf '  ok    %s\n' "$1"; }

# Assembled from fragments so this script contains no literal match of its own
# denylist.
U="/""Users/"
H="/""home/"
K1="sk-""ant-"
K2="gh""p_"
K3="github""_pat_"
K4="xo""xb-"
K5="AKI""A"
K6="BEGIN RSA PRIVATE"" KEY"
K7="BEGIN OPENSSH PRIVATE"" KEY"

scan() {
    needle=$1
    label=$2
    hits=$(grep -rIl -- "$needle" . \
        --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=node_modules \
        --exclude-dir=.ruff_cache --exclude-dir=.mypy_cache --exclude-dir=.pytest_cache \
        --exclude-dir=.venv --exclude-dir=venv --exclude-dir=build --exclude-dir=dist \
        2>/dev/null || true)
    if [ -n "$hits" ]; then
        report "$label found in:"
        printf '        %s\n' $hits
    else
        ok "$label absent"
    fi
}

printf 'Terminal Handoff - privacy and secret scan\n'
printf '==========================================\n\n'

printf 'Personal paths\n'
scan "$U" "absolute user-home paths"
scan "$H" "absolute linux-home paths"

printf '\nCredentials\n'
scan "$K1" "Anthropic-style API keys"
scan "$K2" "GitHub personal access tokens (classic)"
scan "$K3" "GitHub personal access tokens (fine-grained)"
scan "$K4" "Slack bot tokens"
scan "$K5" "AWS access key IDs"
scan "$K6" "RSA private keys"
scan "$K7" "OpenSSH private keys"

printf '\nOperational data that must never be committed\n'
for pattern in handoffs triggered launching completed failed logs prompts state backups outbox notifications livetest; do
    if [ -d "$pattern" ]; then
        report "runtime directory present: $pattern/"
    fi
done
found=$(find . -name '*.jsonl' -not -path './.git/*' 2>/dev/null || true)
if [ -n "$found" ]; then
    report "transcript-like .jsonl files present:"
    printf '        %s\n' $found
else
    ok "no .jsonl transcript files"
fi
found=$(find . \( -name '*.bak' -o -name '*.log' \) -not -path './.git/*' 2>/dev/null || true)
if [ -n "$found" ]; then
    report "backup or log files present:"
    printf '        %s\n' $found
else
    ok "no backup or log files"
fi

printf '\n'
if [ "$FAILURES" -eq 0 ]; then
    printf 'PASS - the tree contains no personal paths, credentials or runtime data.\n'
    exit 0
fi
printf 'FAILED - %s problem(s) found. Do not publish until these are resolved.\n' "$FAILURES"
exit 1
