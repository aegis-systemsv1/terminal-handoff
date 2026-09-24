# Resume (`th resume`)

`th resume <checkpoint>` reads a [checkpoint](CHECKPOINT.md) back and launches a fresh Claude Code
successor session seeded with a trust-labelled continuation brief, instead of the original raw
transcript. Target flow:

```
Claude session -> th checkpoint --ai-summary --compact -> th resume -> successor Claude session
```

**Resume is not a handoff.** There is no live parent process to transfer ownership from — the session
that wrote the checkpoint may be long gone — so resume never engages the transfer-state machine or
heartbeat verification described in [ARCHITECTURE.md](ARCHITECTURE.md). It is a fresh, standalone
launch: validate the checkpoint, re-verify live repository state right now, build the brief, open a new
Claude Code session. This is a deliberate scope boundary, not an oversight — see
[Known limitations](#known-limitations).

## Command

```
th resume <checkpoint> [--repo PATH] [--name NAME] [--json]
```

| Option | Purpose |
|---|---|
| `checkpoint` | A checkpoint id (`chk_...`, resolved under `checkpoints/`) or a path to a checkpoint `.json` file |
| `--repo PATH` | Repository to resume into (default: the checkpoint's own recorded `repository_path`) |
| `--name NAME` | Display name for the successor session |
| `--json` | Print the resume result as JSON |

On success, prints the checkpoint id, the repository, the path to the written brief file, whether a
Terminal window was actually opened, and a one-line summary of any drift detected. It never prints the
brief's full contents to stdout (the brief may contain checkpoint content); the successor reads it from
its own file.

## What actually happens, in order

1. **Load and validate the checkpoint** (`load_checkpoint_for_resume`) — resolve the reference to a
   file, parse it, check `schema_version` is one this build supports, check every required top-level
   block is present, and verify the integrity hash (`verify_checkpoint_integrity`). Any failure here is
   reported and refused — nothing is repaired and continued.
2. **Re-verify live repository state right now** (`compare_checkpoint_to_live_repository`) — a fresh
   `capture_repo_state()` of the target repository, independently compared field-by-field against what
   the checkpoint recorded. The checkpoint's own claims are never trusted as still true.
3. **Fail-closed preflight** (`resume_preflight`) — refuses to go any further only for: an invalid
   checkpoint (from step 1), a repository that cannot be located at all, or a repository whose git
   history has no relationship to the checkpoint's recorded `head_sha` (see
   [Repository drift](#repository-drift) for the line between this and ordinary drift, which is never a
   refusal).
4. **Render the continuation brief** (`render_resume_brief`) — see [Trust model](#trust-model). Two forms
   are written, both private (`0600`): the rendered prose the successor reads,
   `prompts/resume-<checkpoint_id>.md`, and the structured (JSON) form of the same six sections,
   `prompts/resume-<checkpoint_id>.json` — the machine-readable ground truth a human or tool should prefer
   over parsing the prose. The `.md` file's last line points at its `.json` sibling.
5. **Resolve identity, read-only** — if the checkpoint's `session` block carries a real `session_id`
   (`provenance: recorded_evidence`) and an existing Terminal Handoff chain record already exists for
   it, that chain id and generation are read and passed into the successor's environment
   (`CLAUDE_TERMINAL_HANDOFF_CHAIN_ID`/`_GENERATION`) for display only. **Resume never writes to the
   chain-generation registry itself** — see [Known limitations](#known-limitations). When no such
   record exists (the common case — most checkpoints are made without a registered chain), no identity
   is invented; the successor is a plain, standalone session.
6. **Build and safety-check the launch argv** (`build_resume_launch_argv` /
   `assert_resume_argv_safe`) — a short, argv-safe bootstrap prompt pointing at the brief file (the same
   pattern automatic/manual handoff already uses for successor prompts, so the full brief never appears
   in `ps` output), and never Claude Code's own `--resume`/`--continue`/`-c`/`-r`/`--fork-session` — those
   would replay Claude Code's session state directly, bypassing the checkpoint's trust-labelled brief
   entirely. Always a genuinely fresh session.
7. **Launch** (`launch_resume_terminal`) — opens one new macOS Terminal window via `osascript`, the same
   safe-quoting primitive the handoff launcher uses. Under `CLAUDE_TERMINAL_HANDOFF_TEST_MODE=1` (the
   existing project-wide test convention), no window is opened; the would-be command is returned instead.
8. **Record, for audit** — a resume record (`resumes/<checkpoint_id>.json`) capturing the checkpoint id,
   repository, drift report, resolved identity (if any), and whether the launch succeeded.

## Trust model

The brief distinguishes six kinds of content, and never flattens them into one undifferentiated prompt:

| Section | Source | Provenance |
|---|---|---|
| VERIFIED FACTS | Re-confirmed by Terminal Handoff, directly, at resume time | `machine_verified` / `recorded_evidence` |
| REPOSITORY STATE SINCE CHECKPOINT | The live drift comparison (step 2) | Terminal Handoff's own, just now |
| USER INSTRUCTIONS AND CONSTRAINTS (recovered) | An AI worker's quote of a past transcript | `ai_generated` — explicitly labelled "NOT a live instruction from the current person" |
| AI-GENERATED SUMMARY | The checkpoint's `session_summary` (Slice 2) | `ai_generated` |
| RECOMMENDED NEXT ACTION | The checkpoint's `session_summary.recommended_next_action` | `ai_generated`, explicitly labelled "context only, not authority" |
| SMART COMPACT VERIFY FINDINGS | The checkpoint's `smart_compact.verify` (Slice 3), when present | Cross-check results, never promoted to fact |

Every rendered brief opens with a hardcoded `RESUME_TRUST_BOUNDARY_NOTICE` that states these rules
explicitly before any recovered content appears, and closes with an explicit instruction to
independently inspect the real repository before making any change.

**AI-generated text never becomes a verified user instruction merely by appearing in a checkpoint.**
Concretely: `user_instructions_and_constraints_recovered` in the structured brief, and the
"USER INSTRUCTIONS AND CONSTRAINTS (recovered)" section in the rendered text, always carry the
`ai_generated` label and the "not a live instruction" disclaimer, regardless of what the content itself
claims to be — including if the recovered text itself claims to be a system instruction, an escalation
of authority, or an override. Proven adversarially: `tests/test_resume.py`'s
`test_fake_user_instruction_inserted_by_ai_is_still_labelled_recovered_not_live` constructs exactly this
attempt and asserts the label and section placement survive unchanged.

**The recommended next action is never auto-executed.** Resume only ever renders it as text inside a
brief file for a human-supervised Claude Code session to read; nothing in the resume pipeline shells out
using its content. Proven adversarially in `test_malicious_recommended_next_action_is_inert_never_auto_executed`.

**Transcript-derived prompt injection cannot masquerade as a Terminal Handoff instruction.** The
checkpoint's own AI-summary capture (Slice 2) already instructs its worker to treat transcript content as
untrusted and never obey it; resume adds a second layer on top — whatever the AI summary contains, it is
inserted into the brief as inert text inside its own clearly labelled section, never interpreted, never
executed, and never able to alter which section of the brief anything else lands in (the brief's
structure is built entirely from Python dict/string operations over the checkpoint's own JSON fields,
never by interpreting recovered text as markup or instructions). Proven adversarially in
`test_prompt_injection_inside_transcript_derived_fields_stays_inert_text`, which embeds a fake
"## VERIFIED FACTS" header with a fabricated `head_sha` inside an AI-summary field and confirms the real
VERIFIED FACTS section still reports the checkpoint's actual, correct value.

**Redaction is defense in depth, at every layer.** The checkpoint's own capture pipeline (Slices 1-2)
already redacts secrets before they are ever written to disk. Resume's brief renderer redacts again,
independently, on the way out — both the rendered text and the structured (JSON) brief, using the same
`redact_secrets()` used throughout Terminal Handoff, via the same recursive redactor Smart Compact uses
(`_compact_redact`). Proven with a hand-constructed checkpoint (bypassing the normal capture pipeline
entirely, with a correctly-recomputed integrity hash) carrying an unredacted-looking secret in an
AI-generated field: `test_secret_in_ai_generated_field_is_redacted_in_rendered_brief` confirms it never
reaches either output.

## Repository drift

Every checkpoint field compared against live state gets one of three labels:

- **`VERIFIED`** — the live value matches what the checkpoint recorded.
- **`CHANGED_SINCE_CHECKPOINT`** — it differs. This is never a refusal to launch; it is surfaced to the
  successor under "REPOSITORY STATE SINCE CHECKPOINT" so it can reconcile live state itself.
- **`UNVERIFIABLE`** — the comparison could not be made at all (e.g. the target path does not exist or is
  no longer a git repository).

Fields compared: `repository_identity` (see below), `branch`, `head_sha`, `dirty`, and
`working_tree_filenames` (a set comparison of staged/modified/untracked filenames — never contents).

`repository_identity`'s check is **object reachability**, not path string equality: it asks whether the
checkpoint's recorded `head_sha` still exists as a commit in the target repository's history
(`git rev-parse --verify <sha>^{commit}`), not whether the two path strings are identical. This matters
because a checkpoint's recorded path and the live repository's resolved path can differ cosmetically
(for example, a `/tmp` → `/private/tmp` symlink on macOS) while being the exact same repository — a naive
path comparison would have shown a false `CHANGED` result next to an otherwise-`VERIFIED` state, which
is exactly what an earlier version of this field did before being fixed to display the head_sha and its
reachability instead of the two raw paths.

**A different repository entirely — where the recorded `head_sha` is not reachable anywhere in the
target's history — is the one drift condition that fails closed** (see below), because at that point
resume can no longer establish this is the same project that drifted, rather than a different one.

## Fail-closed behaviour

`resume_preflight` refuses to launch anything (`ok: False`, nonzero exit, no brief written, no launch
attempted) only for:

- A malformed, unreadable, or missing checkpoint file.
- A checkpoint whose `schema_version` this build does not support.
- A checkpoint whose integrity hash does not match its content (any tampering after the hash was
  computed, however small).
- A checkpoint missing a required top-level block.
- A target repository that cannot be located at all (path missing, or not a directory).
- A checkpoint that does not record a `head_sha` at all — found during independent review: a real
  checkpoint from `build_checkpoint()` always has one (`CheckpointCaptureError` is raised otherwise), so
  this can only be a checkpoint that bypassed the normal capture pipeline entirely with a correctly
  recomputed integrity hash. Without a `head_sha`, repository identity cannot be established against
  *any* target repository, so this fails closed identically to "cannot be identified safely", not merely
  `UNVERIFIABLE`.
- A target repository whose git history has no relationship to the checkpoint's recorded `head_sha`
  ("checkpoint points to an unexpected project").

It does **not** fail closed for ordinary drift — a changed branch, a moved `HEAD` (still an ancestor
relationship), a changed dirty state, or changed working-tree filenames. Those are real, expected outcomes
of time passing between a checkpoint and a resume, and are surfaced to the successor instead, per the
requirement that ordinary repository drift should not block resuming.

`cmd_resume` separately refuses (after preflight passes) if the `claude` executable cannot be found, or
if the resolved working directory fails `_safe_path_for_shell`, or if the constructed launch argv fails
`assert_resume_argv_safe` — none of these ever leave a partially-written brief or a launched window behind
on failure.

## Real-transcript evidence

Run end-to-end against a real repository and a real transcript describing genuine work (a function added,
an explicit "don't commit without showing me the diff" constraint, one item left outstanding), through a
real `--ai-summary --compact` checkpoint and then through `resume_preflight`/`render_resume_brief`, with a
real commit deliberately landed on the repository *after* the checkpoint to force genuine drift. The
resulting brief was fed to a real, one-shot `claude -p` call standing in for a successor session (asked
not to use any tools, so its answer reflects only the brief's own content) with seven comprehension
questions. It correctly: named the current task and what was completed; listed what remained outstanding;
quoted the constraints verbatim; identified the recommended action as `ai_generated`/unverified and stated
it would not execute it automatically; specifically named every changed field (`dirty`, `head_sha`,
working-tree files) with old/new values; and stated it did not need the original transcript, while
independently flagging that its own actual working directory did not match the brief's repository at
all — exactly the "independently inspect the real state" behaviour the trust boundary notice asks for.

## Known limitations

- **No integration with the live chain-generation registry.** Resume reads an existing chain record for
  display only (step 5 above); it never calls `record_chain_generation` itself and never advances a
  chain's generation counter. A resumed session's identity is not woven into the same generation-ceiling
  / heartbeat protections that govern automatic and manual handoff. This is a deliberate scope boundary,
  not an omission: those protections exist to prove a *live* parent relinquished ownership, which has no
  meaning when the "parent" is a checkpoint from a session that may no longer exist.
- **No heartbeat or ownership-transfer verification.** Unlike automatic/manual handoff, resume does not
  wait for the successor to prove it is alive before considering the launch complete — it reports success
  based on the Terminal window having been opened (or, under `--test-mode`, on the would-be command having
  been constructed), not on the successor's own confirmation.
- **The `VERIFY` cross-checks resume surfaces are only as good as Smart Compact's own** (see
  [CHECKPOINT.md's Known limitations](CHECKPOINT.md#known-limitations)) — a narrow, honestly-scoped set of
  structural checks, not a general fact-checker.
- **`working_tree_filenames` drift compares filenames only, never contents** — consistent with the rest of
  Terminal Handoff's checkpoint design (file contents are never captured), but it means a file that was
  modified without being added or removed from the working tree's file lists will not show up as changed
  in this specific comparison (its presence in `dirty`/`status_porcelain_count` still will).
  Independent inspection of the real repository, which the brief explicitly asks for, is what actually
  catches this class of change.
- **No Codex integration.** Out of scope for this slice, as instructed.
- **The successor's actual comprehension was demonstrated with a one-shot `claude -p` call, not a full
  interactive GUI Terminal.app session.** The real launch mechanism (`launch_resume_terminal`, opening an
  actual Terminal window) is exercised by dedicated tests with an injected `popen`, and manually; it was
  deliberately not exercised end-to-end as a live, uncontrolled GUI window during automated acceptance,
  to avoid popping windows outside the operator's control during a test run.
