---
name: handoff
description: Manually transfer this exact Claude Code session to a verified fresh successor when an automatic Terminal Handoff did not complete.
disable-model-invocation: true
argument-hint: ""
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/manual-handoff.py *)
---

<!-- Terminal Handoff managed /handoff skill. -->

Execute exactly one fail-closed manual Terminal Handoff for this session.

!`${CLAUDE_SKILL_DIR}/manual-handoff.py "${CLAUDE_SESSION_ID}" || true`

Report the result above in one short paragraph. Do not open another Terminal,
start Claude directly, retry the command, alter a repository, or stop a process.
The runtime owns launching, verification, parent shutdown, and notifications.

Arguments are unsupported and must be ignored: $ARGUMENTS
