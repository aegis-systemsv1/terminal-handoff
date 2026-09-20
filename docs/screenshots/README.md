# Screenshots

This folder is for documentation screenshots of the mobile interface. It is deliberately empty of
images until they have been checked, because this repository is **public**.

## Wanted

| File | Shows |
|---|---|
| `session-list.png` | The session list: names, state and Remote Control health |
| `session-running.png` | A session that is `RUNNING`, Remote Control **Healthy**, with recent output |
| `session-stopped.png` | A `STOPPED` session and the "Resume..." panel |
| `session-generation-2.png` | The same session after a handoff, showing **Owner generation: 2** |
| `approval-required.png` | The APPROVAL REQUIRED card (use a harmless simulated action) |

## Rules: a screenshot must not contain

- a **Tailscale hostname** or any address bar showing one (crop the browser chrome or use a redacted copy);
- an **enrollment code**, device credential, cookie or token;
- a **personal filesystem path** or user name;
- the name, output or task of a **private project** (use a disposable scratch project).

Before committing an image, open it at full size and read every pixel of it, then run
`scripts/coverage-check.sh`. If in doubt, leave it out.
