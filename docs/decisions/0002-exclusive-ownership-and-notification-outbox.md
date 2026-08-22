# 2. Confirm parent exit before ownership, and notify through an outbox

- **Status:** accepted
- **Date:** 2026-08-22
- **Release:** 1.2.0
- **Amends:** [0001](0001-parent-shutdown-and-successor-naming.md)

## Context

Version 1.1 transferred ownership at `PARENT_STOP_REQUESTED`. That state means
the successor is verified and `SIGTERM` is about to be or has been sent; it does
not mean the parent exited. With two twenty-second grace periods, the parent
could remain live after the successor was told it owned the repository. Prompt
discipline could not remove that overlap.

The supervisor also used a permanent `O_CREAT|O_EXCL` marker. Exclusion was
correct while the process lived, but a crash left the marker behind and blocked
every replacement forever.

Users also need to know a chain moved from one named generation to the next,
especially when away from the Mac. Alert delivery is fallible and external; it
must not become part of the transfer transaction or leak the transcript.

## Decision

### Exclusive ownership

- `LAUNCHING` and `SUCCESSOR_VERIFIED`: owner `parent`.
- `PARENT_STOP_REQUESTED`: owner `none`; both sessions are read-only.
- `TRANSFER_COMPLETE`: owner `successor`, only after parent exit is confirmed.
- `TRANSFER_FAILED`: owner `parent`.

The successor independently repeats `pwd`, branch, HEAD, status and active Git
operation checks after `TRANSFER_COMPLETE` and before its first mutation.

### Recoverable supervision

The supervisor holds a non-blocking `flock` lease for its process lifetime. The
kernel releases the lease on clean exit and process death. Both parent and
successor status refreshes probe that lease for non-terminal transfers and
spawn a replacement when no process owns it. Every replacement still has to
acquire the same lease, and the atomic state transition still prevents a second
signal request. If a crash occurred after `PARENT_STOP_REQUESTED`, recovery only
observes: it completes when the exact parent is gone or fails back to the live
parent after the original grace budget. It never repeats an uncertain signal.

### Transactional notification outbox

The terminal transfer transition is committed first. A deterministic,
privacy-minimised event is then written to a private outbox. A detached worker
delivers local macOS alerts and optional signed webhook or Messages requests.
Failures use per-channel retry and never modify the committed transfer.

The public event is provider-neutral. A private gateway owns presence inference
and provider-specific web push, SMS or messaging routing. The public repository
does not contain provider credentials or depend on one vendor.

## Consequences

- There is a short state with no mutation owner. This is deliberate and safer
  than two owners.
- A supervisor crash delays the handoff until a later status refresh, but no
  permanent marker can strand it and no recovery process can double-signal.
- Notifications provide at-least-once delivery. A webhook consumer must dedupe
  the deterministic event ID. Provider acceptance is not human receipt.
- Messages requires explicit Automation permission; carrier SMS/RCS additionally
  requires iPhone Text Message Forwarding.
- External delivery is disabled by default. Enabling it intentionally expands
  the trust boundary, using HTTPS, HMAC and a secret stored outside generated
  scripts.

## Verification

Automated tests cover the exact ownership map and successor prompt, killed
supervisor recovery, self-healing without duplicate supervision, launcher-spawn
failure, status-line option preservation, event privacy and idempotency,
presence routing, HMAC headers, per-channel retry and configuration redaction.
The macOS CI matrix continues to exercise real process identity and signal
semantics.
