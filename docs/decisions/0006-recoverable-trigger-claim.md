# 6. The automatic trigger claim must be recoverable

Status: accepted

## Context

The automatic trigger takes a one-shot claim (`triggered/<session>`, created with `O_CREAT|O_EXCL`) and
then spawns a detached launcher. In acceptance testing the status-line process died between the claim and
the launch, leaving a claim with no launch behind it: the session reported "already handed off" forever and
could never trigger again.

## Decision

* Bind the parent process **before** taking the claim, so the window between claim and spawn is a single
  spawn rather than process inspection.
* Release an **orphaned** automatic claim on the next status-line run when all hold: it is at least 45 s
  old; its claimant process is gone (or it is over 5 minutes old); and no launch left any trace
  (manifest, transfer record, launch script, completed or failed record).
* Bound recovery to three attempts per session, apply it only to automatic claims (never manual ones), and
  skip it for `evaluate --no-record`.

## Consequences

A crashed trigger self-heals without weakening duplicate-launch protection: `O_CREAT|O_EXCL` remains the
launch boundary, and a claim with any launch trace is never released.
