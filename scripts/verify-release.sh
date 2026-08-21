#!/bin/sh
#
# Release verification for Terminal Handoff: version consistency, syntax,
# metadata, documentation links and the full test suite.

set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

PY=${PYTHON:-$(command -v python3)}
FAILURES=0
report() { printf '  FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
ok()     { printf '  ok    %s\n' "$1"; }

printf 'Terminal Handoff - release verification\n'
printf '=======================================\n\n'

printf 'Version consistency\n'
V_FILE=$(cat VERSION 2>/dev/null || echo missing)
V_PY=$("$PY" -c "
import re
body = open('src/terminal_handoff/core.py').read()
m = re.search(r'TERMINAL_HANDOFF_VERSION = \"([^\"]+)\"', body)
print(m.group(1) if m else 'missing')
")
V_TOML=$("$PY" -c "
import re
body = open('pyproject.toml').read()
m = re.search(r'^version = \"([^\"]+)\"', body, re.M)
print(m.group(1) if m else 'missing')
")
printf '        VERSION=%s  core.py=%s  pyproject.toml=%s\n' "$V_FILE" "$V_PY" "$V_TOML"
if [ "$V_FILE" = "$V_PY" ] && [ "$V_PY" = "$V_TOML" ] && [ "$V_FILE" != "missing" ]; then
    ok "all three agree"
else
    report "version strings disagree"
fi

if grep -q "\[$V_FILE\]" CHANGELOG.md 2>/dev/null; then
    ok "CHANGELOG.md documents $V_FILE"
else
    report "CHANGELOG.md has no entry for $V_FILE"
fi

printf '\nSyntax\n'
if "$PY" -m compileall -q src tests >/dev/null 2>&1; then
    ok "python sources compile"
else
    report "python compilation failed"
fi
if "$PY" -m compileall -q scripts >/dev/null 2>&1; then
    ok "helper scripts compile"
else
    report "helper script compilation failed"
fi
for script in install.sh uninstall.sh scripts/coverage-check.sh scripts/verify-release.sh; do
    interpreter=sh
    head -1 "$script" | grep -q zsh && interpreter=zsh
    if command -v "$interpreter" >/dev/null 2>&1 && "$interpreter" -n "$script" 2>/dev/null; then
        ok "$script parses"
    else
        report "$script failed to parse"
    fi
done

printf '\nRequired files\n'
for f in README.md LICENSE CHANGELOG.md SECURITY.md CONTRIBUTING.md CODE_OF_CONDUCT.md \
         pyproject.toml .gitignore .editorconfig install.sh uninstall.sh \
         src/terminal_handoff/core.py src/terminal_handoff/templates/successor-prompt.md \
         src/terminal_handoff/naming.py src/terminal_handoff/transfer.py \
         scripts/live-handoff-test.py docs/LIVE_TEST_EVIDENCE.md \
         docs/decisions/0001-parent-shutdown-and-successor-naming.md; do
    if [ -f "$f" ]; then ok "$f"; else report "missing: $f"; fi
done

printf '\nDocumentation links\n'
"$PY" - <<'PYEOF' || FAILURES=$((FAILURES + 1))
import os, re, sys
missing = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'node_modules')]
    for name in files:
        if not name.endswith('.md'):
            continue
        path = os.path.join(root, name)
        with open(path) as handle:
            body = handle.read()
        for target in re.findall(r'\]\(([^)#:]+\.md)(?:#[^)]*)?\)', body):
            resolved = os.path.normpath(os.path.join(root, target))
            if not os.path.exists(resolved):
                missing.append('%s -> %s' % (path, target))
if missing:
    print('  FAIL  broken documentation links:')
    for item in missing:
        print('        ' + item)
    sys.exit(1)
print('  ok    all relative documentation links resolve')
PYEOF

printf '\nPrivacy scan\n'
if ./scripts/coverage-check.sh >/dev/null 2>&1; then
    ok "no personal paths, credentials or runtime data"
else
    report "privacy scan failed - run ./scripts/coverage-check.sh"
fi

printf '\nTest suite\n'
if "$PY" -m unittest discover -s tests >/tmp/th-verify-tests.log 2>&1; then
    ok "$(tail -3 /tmp/th-verify-tests.log | grep -o 'Ran [0-9]* tests.*' || echo 'suite passed')"
else
    report "test suite failed - see /tmp/th-verify-tests.log"
    tail -15 /tmp/th-verify-tests.log | sed 's/^/        /'
fi

printf '\n'
if [ "$FAILURES" -eq 0 ]; then
    printf 'PASS - release verification succeeded.\n'
    exit 0
fi
printf 'FAILED - %s check(s) did not pass.\n' "$FAILURES"
exit 1
