# 5. Preserve the user's Claude permission mode; never choose or override one

Status: accepted

## Context

Remote sessions run with `--setting-sources ""` so a per-session profile is the only permission source.
That also drops the user's own settings, including their permission mode (for example Auto Mode). An
early design disabled Auto Mode outright; the user's chosen mode must instead be preserved.

## Decision

* Terminal Handoff never passes `--permission-mode` or `--dangerously-skip-permissions` (both stay on
  the forbidden-argument list) and never selects a mode of its own.
* The mode to keep is the gateway's configured `permission_mode` (`remote configure --permission-mode`)
  or, failing that, the user's own `permissions.defaultMode`. Only `auto`, `default`, `acceptEdits` and
  `plan` can be carried; `bypassPermissions` and `dontAsk` never are.
* It is written as `permissions.defaultMode` in the per-session settings file that successors inherit,
  so it survives every handoff. For `auto`, the user's `autoMode` classifier context is copied too, and
  `disableAutoMode` is not set (it would contradict the user's choice).
* Terminal Handoff STOP and `WAITING_FOR_HUMAN` remain cooperative gates that take precedence over any
  mode.

## Consequences

In Auto Mode Claude's classifier, not the allow list, decides most prompts, so a project profile is a
narrower default rather than a hard boundary, and it is not an OS sandbox. A launched session's mode can
still be changed at runtime by Claude itself or by clients attached through Remote Control; Terminal
Handoff has no code path that does so.
