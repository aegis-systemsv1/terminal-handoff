#!/bin/zsh
# Terminal Handoff - uninstaller
#
# Removes Terminal Handoff from every Claude settings file it modified,
# restoring any pre-existing status line exactly, and removes the Terminal
# Handoff section from ~/.claude/CLAUDE.md.
#
# Preserved unless you delete them yourself:
#   - handoff manifests   (~/.claude/terminal-handoff/handoffs/)
#   - logs                (~/.claude/terminal-handoff/logs/)
#   - configuration backups (~/.claude/terminal-handoff/backups/)
#   - notification config and delivery history (notifications.json, outbox/)
#
# Never touched: transcripts, repositories, unrelated Claude settings.
#
# Usage:
#   uninstall.sh              dry run: show exactly what would change
#   uninstall.sh --apply      apply the changes (asks for confirmation)
#   uninstall.sh --apply --yes  apply without the interactive prompt

set -u

TH_DIR="${0:A:h}"
PY="/usr/bin/python3"
CORE="$TH_DIR/terminal-handoff.py"

APPLY=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help)
      print "Usage: $0 [--apply] [--yes]"
      exit 0
      ;;
    *)
      print -u2 "Terminal Handoff: unknown argument: $arg"
      exit 2
      ;;
  esac
done

print "Terminal Handoff uninstaller"
print "----------------------------"
print ""
print "Planned changes (dry run):"
"$PY" "$CORE" uninstall || exit 1
print ""

if [[ $APPLY -eq 0 ]]; then
  print "Nothing was changed. Re-run with --apply to perform the uninstall."
  exit 0
fi

if [[ $ASSUME_YES -eq 0 ]]; then
  print -n "Apply these changes? [y/N] "
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) print "Aborted. Nothing was changed."; exit 0 ;;
  esac
fi

"$PY" "$CORE" uninstall --apply
status=$?
print ""
if [[ $status -eq 0 ]]; then
  print "Terminal Handoff removed from Claude configuration."
  print "Retained: manifests, logs, backups and notification history under $TH_DIR"
  print "To remove those too: rm -rf $TH_DIR"
else
  print -u2 "Terminal Handoff: uninstall reported errors (exit $status)."
fi
exit $status
