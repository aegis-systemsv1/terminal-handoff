# Acceptance evidence: persistent remote session control

**Milestone:** Terminal Handoff 1.4.0. **Final physical acceptance: 2026-09-20.**

This records how the remote session-control system was accepted, what was found and fixed on the way,
and what remains known. It contains no hostnames, credentials, enrollment codes or private paths.

## Method

Acceptance used a **disposable scratch project** (a throwaway Git repository with no remote and no
credentials) and a **physical iPhone**. No real project was enabled for remote launch. The remote gateway
listened on loopback only and was published through a tailnet-only Tailscale mapping on its own HTTPS
port; the pre-existing mappings were left untouched and verified unchanged before and after. Earlier
stages used a dedicated state directory and a controlled dry run with no external exposure.

## Result

| Check | Result |
|---|---|
| Remote session launch from the iPhone (project choice, task, name) | Passed |
| Disconnect and reconnect (session unaffected, same logical session) | Passed |
| Remote instruction delivery (durable queue, delivered to the current owner) | Passed |
| Automatic A → B handoff | Passed |
| Sole-owner / sole-writer verification (predecessor gone before adoption; stale owner refused) | Passed |
| Successor continuation | Passed |
| STOP from the iPhone | Passed |
| Queued work blocked while STOPPED | Passed |
| Deliberate resume with a reason | Passed |
| Queued work executed after resume, in order | Passed |
| Session identity and custom name preserved across the handoff | Passed |
| Remote Control healthy, and rechecked for the new owner | Passed |
| iOS text entry and native copy/paste | Passed |
| Session naming at creation and rename | Passed |
| Human approval: APPROVE, DENY, stale and forged attempts, duplicate decision | Passed (earlier dry run, real agent) |
| Dead owner detected, `ORPHANED`, not relaunched, abandon | Passed (earlier dry run, real agent) |
| Gateway restart with a live session (STOP preserved, owner not orphaned) | Passed |
| Automated suite | **581 tests passed** |
| `scripts/verify-release.sh` | **PASS** |

## Defects found in acceptance testing, and fixed

Each was found by using the system for real, then fixed and covered by tests.

1. **iOS textarea polling and paste.** The session page rebuilt everything, including the instruction
   box, on every poll, dismissing the iOS paste menu and losing focus. The box is now created once and
   never rebuilt; polling redraws only what changed; non-urgent redraws are held while the box has
   focus and for a short quiet period after (iOS can blur it during its paste dialog); urgent changes
   (state, approvals, STOP) still show at once; Send clears the draft only on success. The page has no
   clipboard, paste, `preventDefault`, `focus()` or selection code, and a static test enforces that.
   The remaining "Pasting from <device>..." delay seen on one iPhone was Apple's Universal Clipboard,
   not the page (local paste worked).
2. **Session naming.** Sessions could not be named or renamed. Added an optional name at creation and a
   Rename action (display metadata only, persisted on the logical session, audited). The rename panel
   first opened below the fold on a phone and now opens at the top.
3. **Stranded automatic-handoff claim.** The automatic trigger claimed its one-shot marker, and its
   status-line process then died before launching, so the session reported "already handed off" and
   never launched. The parent is now bound before the claim, and an orphaned claim (old, claimant gone,
   no launch trace) is recovered automatically, bounded to three attempts. The live session recovered
   on its own.
4. **Halted-agent wait / resume.** After a STOP the real agent ended its turn, so a remote resume could
   never reach it (nothing supported can wake an idle session). Agents are now told to keep blocking in
   `session wait` while halted, and a wait reports a STOP the moment it begins.
5. **Successor scoped-read rules.** In an isolated session a successor's reads of its manifest and
   transfer files would raise permission prompts. Remote sessions now get narrow read rules for
   Terminal Handoff's own manifest, transfer and prompt files (never the launch-token files) and the
   project's own transcripts.
6. **Successor Python path.** The successor prompt named a different interpreter than the allow rules
   covered. The prompt now uses the interpreter and script recorded for the logical session.
7. **Auto Mode preservation.** Isolated sessions dropped the user's chosen mode. The mode is now carried
   as `permissions.defaultMode` in the per-session settings (never on the command line), together with
   the user's Auto Mode classifier context, and inherited by successors. Terminal Handoff never picks or
   overrides a mode.
8. **Invalid profile rules.** Claude ignores `Write(...)` allow rules (only `Edit(path)` counts); the
   validator now rejects them so a profile cannot look narrower than it behaves.
9. **Serve mapping guard.** The gateway's own Tailscale mapping wrongly blocked its restart; the guard
   now exempts a mapping on the explicitly configured public port.
10. **Workspace trust.** Claude's one-time folder-trust question cannot be answered unattended and
    Terminal Handoff does not answer it; this is documented as a manual prerequisite per project.

## Known limitations

See [README.md](../README.md#known-limitations). None was reopened as a blocker:
Claude's runtime permission mode can change independently; Auto Mode profiles are not an OS sandbox; the
interactive Stop hook is unverified; new projects need one-time Claude folder trust; wake latency varies
while Claude is busy; Mac reboot recovery is not implemented.

---

# Grok acceptance (1.5.0): procedure and status

**Status: pending. Not yet accepted.** The implementation is verified by automated tests against a scripted ACP
agent and by a live protocol probe (initialize, session/new) against the installed Grok CLI. A live model turn
was not possible when this was written (the Grok account reported an exhausted balance), so the steps below have
not been run on a phone.

Use the registered **scratch** project only; enable Grok for it with `th project enable-grok scratch`, and make
sure `~/.grok/config.toml` does not select always-approve.

1. Open Terminal Handoff on the iPhone; New Session; Project **scratch**; Agent **Grok**; Name **Grok iPhone Test**.
2. Task: a harmless request that creates one small file in the project. Start Session.
3. Confirm Grok starts and the transcript updates (replies and tool-call status; no reasoning).
4. Disconnect the phone; confirm Grok keeps working. Reconnect; confirm the same logical session and the same
   Grok session.
5. Send a follow-up; confirm it runs in the same Grok session.
6. STOP; queue an instruction while stopped; confirm it does not run. Resume; confirm it then runs.
7. If Grok asks permission, confirm it appears as an approval on the phone and that approve and deny both work.
8. Stop the session, wait for ORPHANED (or abandon), archive it; confirm the project's files and Grok's own
   history are intact.

Record the result here, with the date, when it has been run.
