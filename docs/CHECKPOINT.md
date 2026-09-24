# Checkpoints (`th checkpoint`)

A checkpoint is a standalone, point-in-time snapshot of repository and session state, written to
`checkpoints/<checkpoint_id>.json`. It exists so a future session — or a human — can understand exactly
where a previous session left off, without loading that session's full transcript.

**A checkpoint is not a handoff.** It never launches a successor, never writes to the transfer state
machine, and can be created any number of times, at any point, safely. Nothing about the automatic
A→B handoff flow described in [ARCHITECTURE.md](ARCHITECTURE.md) is affected by checkpoints, and vice
versa. This document covers checkpoint creation; reading one back into a fresh session is
[`th resume`](RESUME.md).

Checkpoints were built in four slices, each strictly additive over the last:

1. **Deterministic capture** — repository, working tree, test evidence. No AI involved.
2. **AI session summary** (`--ai-summary`) — an isolated AI worker's account of a transcript.
3. **Smart Compact** (`--compact`) — classifies what slices 1–2 already captured into
   KEEP / COMPRESS / DROP / VERIFY.
4. **[Resume](RESUME.md)** (`th resume`) — reads a checkpoint back and launches a fresh Claude Code
   successor session with a trust-labelled continuation brief built from what slices 1–3 captured. No
   new capture happens here; see RESUME.md for the trust model and fail-closed behaviour.

## Command

```
th checkpoint [--repo PATH]
              [--session-id ID] [--agent-type claude|grok|codex]
              [--test-command CMD] [--run-tests | --test-exit-code N [--test-output TEXT | --test-output-file F]]
              [--transcript PATH] [--ai-summary] [--ai-model MODEL]
              [--compact]
              [--json]
```

| Option | Purpose |
|---|---|
| `--repo PATH` | Repository to checkpoint (default: current directory) |
| `--session-id ID` | Caller-asserted session id, recorded as `recorded_evidence`, never verified |
| `--agent-type claude\|grok\|codex` | A label on the session id above; recording `codex` does **not** mean Codex is supported (it is not — see [Known limitations](#known-limitations)) |
| `--test-command CMD` | A test command to execute (with `--run-tests`) or describe (with `--test-exit-code`) |
| `--run-tests` | Actually execute `--test-command` now (`machine_verified` evidence) |
| `--test-exit-code N` | Record a test already run elsewhere (`recorded_evidence`); requires `--test-command` |
| `--test-output TEXT` / `--test-output-file F` | Inline or file-sourced output for the recorded-evidence path |
| `--transcript PATH` | Path to a Claude Code session transcript (`.jsonl`) to summarise with `--ai-summary` |
| `--ai-summary` | Ask an isolated AI worker to summarise `--transcript`. Requires `--transcript`. Nothing is read unless this is passed |
| `--ai-model MODEL` | Model for the summary worker (default `claude-haiku-4-5-20251001`) |
| `--compact` | Classify this checkpoint's own fields into KEEP/COMPRESS/DROP/VERIFY. No new transcript read, no new AI call |
| `--json` | Print the summary as JSON instead of plain text |

The command only ever prints a short summary (checkpoint id, path, repository, branch, HEAD SHA, dirty
flag, integrity status, whether the AI summary/Smart Compact are present) — never the full checkpoint
contents.

## Schema (schema_version 1)

Every top-level block carries its own `provenance`. Unknown or not-yet-available state is always an
explicit block (`{"provenance": "unavailable", "reason": "..."}`), never a bare `null` and never silently
absent.

| Provenance | Meaning |
|---|---|
| `machine_verified` | Terminal Handoff proved this itself, directly, at checkpoint time |
| `recorded_evidence` | Asserted by the caller (a session id, a test result run elsewhere); not independently verified |
| `unavailable` | This information does not exist for this invocation, with a stated reason |
| `unsupported` | Reserved for an agent/capability this build does not implement |
| `ai_generated` | Produced by the isolated AI summary worker; never upgraded to a verified fact |
| `machine_generated` | Smart Compact's own classification of already-captured fields; not new information |

Top-level blocks: `schema_version`, `checkpoint_id`, `metadata` (creation timestamp only — kept separate
so the rest of the checkpoint compares deterministically across runs against unchanged state),
`environment` (hostname, Terminal Handoff/Python version, platform — nothing else), `session`, `git`,
`working_tree` (staged/modified/untracked **filenames only**, each flagged `sensitive: true/false`, never
contents), `history` (last 5 commits, subjects redacted), `tests`, `session_summary`, `smart_compact`,
`integrity` (a `sha256` over the whole checkpoint except this block itself).

### `session_summary` (added in Slice 2)

Present only if `--ai-summary --transcript <path>` succeeded; otherwise `{"provenance": "unavailable",
"reason": "..."}`. On success:

```json
{
  "provenance": "ai_generated",
  "generated_utc": "...", "model": "claude-haiku-4-5-20251001",
  "note": "Generated by an isolated AI worker...; not independently verified. Treat as a claim to check, not a fact.",
  "current_task": "string or 'unresolved'",
  "work_completed": ["..."], "decisions_made": ["..."], "known_problems": ["..."],
  "files_in_progress": [{"path": "...", "sensitive": false}],
  "tests_performed": ["..."], "outstanding_work": ["..."],
  "user_instructions_and_constraints": ["..."],
  "recommended_next_action": "string or 'unresolved'"
}
```

Any field the worker could not determine is the literal string `"unresolved"` (or an empty list) —
never a guess. Every string is passed through the same `redact_secrets()` used elsewhere in Terminal
Handoff. `files_in_progress` reuses the checkpoint's own sensitive-filename classifier.

### `smart_compact` (added in Slice 3)

Present only with `--compact`; otherwise `{"provenance": "unavailable", "reason": "smart compact was not
requested for this checkpoint"}`. On success:

```json
{
  "provenance": "machine_generated",
  "generated_utc": "...",
  "note": "A classification of this checkpoint's own fields, not new information...",
  "keep":     [{"field": "...", "value": ..., "provenance": "machine_verified|ai_generated"}],
  "compress": [{"field": "...", "value": ..., "provenance": "..."}],
  "drop":     [{"field": "...", "reason": "..."}],
  "verify":   [{"claim": "...", "check": "...", "status": "confirmed|contradicted|stale|current|unverifiable", "detail": "..."}]
}
```

Every `keep`/`compress` item retains the provenance of *where it came from* — an item sourced from
`session_summary` is still `ai_generated` even though it is in `keep`. Smart Compact never relabels an
AI claim as `machine_verified`. `drop` entries never carry the dropped value, only why it was safe to
drop. See [Smart Compact behaviour](#smart-compact-behaviour-keep--compress--drop--verify) below for
what lands in each bucket and why.

## Security model

### Transcript isolation

`th checkpoint` — and any Claude Code session that invoked it as a tool call — **never reads the
transcript itself.** Only a single, disposable `claude -p` subprocess does (`_run_ai_summary_worker` in
`core.py`), adapted from the same "delegate transcript reading to an isolated worker" principle already
used for successor handoffs (see [ARCHITECTURE.md §7](ARCHITECTURE.md#7-transcript-analysis-subagent)).

The worker is sandboxed as narrowly as Claude Code's CLI allows:

- **Permissions**: `--setting-sources ""` (so no project/user/global settings can broaden anything) plus
  a settings file granting exactly one rule: `Read(<path>)` on a **copy** of the transcript.
- **Filesystem reachability**: `--add-dir <dir>` makes a directory reachable to the sandbox at all — this
  is a *separate* control from the permission rule above, and **confirmed live against the real Claude
  CLI to be much broader than the permission rule alone**: a worker asked, with a plain non-adversarial
  prompt, to read a file sitting next to the real transcript could do so, because `--add-dir` exposes the
  whole named directory regardless of any narrower `Read()` rule. The fix is structural, not a tighter
  permission string: the transcript is copied into a directory created fresh for that one call,
  containing nothing else, ever, and `--add-dir` is granted to that directory instead of the transcript's
  real (potentially multi-session) one. Re-verified live afterward that the same sibling-file read
  attempt fails closed. **If this is ever refactored, re-verify live, not just against mocked tests** —
  the settings-file content alone does not prove what the CLI actually enforces.
- **Environment**: an explicit minimal set (`PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`,
  `TMPDIR`, `SHELL`, `CLAUDE_CONFIG_DIR`) — never the calling process's full environment, so unrelated
  secrets sitting in whatever shell invoked `th checkpoint` are never handed to a subprocess whose output
  ends up embedded in a file.
- **No other tools**: the worker's settings grant `Read` only — no `Bash`, `Edit`, `Write`, `WebFetch`,
  or network access of any kind.

### Treating the transcript, and the worker's output, as untrusted

The transcript may contain text that looks like instructions (in tool output, file contents, or quoted
messages). The worker's prompt explicitly instructs it to treat all of that as data, never to act on it,
and to flag any apparent instruction attempt as a `known_problems` entry rather than obeying it. Even in
the worst case where a worker were tricked anyway, its *only* capability is producing a text response —
it has no tool that could act on an injected instruction.

Terminal Handoff's own side of this is symmetric: the worker's entire output is treated as untrusted
text on the way back in. It is parsed as JSON (never `eval`'d or executed), every string is passed
through `redact_secrets()`, and Smart Compact independently re-redacts every value it copies
(`_compact_redact`) rather than trusting that whatever produced the checkpoint already did — defense in
depth, not a single point of trust.

### What redaction does and does not catch

`redact_secrets()` is the same best-effort function used elsewhere in Terminal Handoff (bearer tokens,
`sk-`-style API keys, the tool's own `thd_` prefix, `password|token|secret|api[_-]?key=...` patterns, PEM
private-key blocks in full). It is not a guarantee. Structured fields built from real data — a commit
subject, a file path — get the same treatment as free text, but a secret embedded somewhere redaction's
patterns don't recognise would not be caught. Checkpoint files are `0600`, their directory `0700`,
matching the rest of Terminal Handoff's file-permission convention.

## Smart Compact behaviour: KEEP / COMPRESS / DROP / VERIFY

Smart Compact does not read the transcript and does not make a second AI call — it classifies fields
that slices 1–2 already produced, in the same checkpoint, so it can never disagree with the rest of the
document.

- **KEEP** — never compressed, dropped, or paraphrased further: the current task, explicit user
  instructions/constraints, outstanding (unresolved) work, and the recommended next action, all taken
  verbatim from `session_summary`; plus verified facts (`git.head_sha`, `git.branch`, `git.dirty`, and
  `tests.exit_code` / `successful_execution` when test evidence exists). The recommended next action keeps
  its `ai_generated` provenance in `keep` even when `verify` (below) flags it as contradicting the
  checkpoint's own git state — a flagged claim is never silently dropped or corrected in place, only
  labelled.
- **COMPRESS** — useful history that is already reasonably concise or can be represented more so:
  `work_completed`, `decisions_made`, `known_problems` from the AI summary (unchanged — Smart Compact
  does not further paraphrase them), and recent commits condensed from full objects to `"<sha> <subject>"`
  one-liners.
- **DROP** — raw, reconstructible material, with a stated reason and never the value itself: the test
  command's raw output tail (the pass/fail fact is kept; the log is reconstructible by re-running the
  recorded command) and the `environment` block (machine identity isn't needed to continue the work).
- **VERIFY** — claims that require independent confirmation before being trusted, never resolved by
  assertion:
  - The checkpoint's recorded git state (`head_sha`, dirty) is re-compared against the **live**
    repository at compact time → `current` or `stale`.
  - `session_summary.tests_performed` is cross-checked, by a deliberately simple substring heuristic (not
    semantic understanding), against the checkpoint's own deterministic `tests` block →
    `confirmed`/`contradicted`/`unverifiable` (the last when no deterministic test evidence exists at
    all — the AI may be describing tests run earlier in the session that weren't captured with
    `--run-tests`/`--test-exit-code`).
  - `session_summary.files_in_progress` is cross-checked against the checkpoint's own `working_tree`
    block (same checkpoint, same moment) → `confirmed`/`contradicted`.
  - `session_summary.recommended_next_action` is cross-checked against the checkpoint's own `git.dirty`
    state, by the same kind of narrow substring heuristic as the other VERIFY checks — it only catches a
    specific, explicit self-contradiction (e.g. the recommendation says the working tree is clean while
    `git.dirty` is true, or says changes still need to be committed while `git.dirty` is false) →
    `contradicted`/`unverifiable`. It is not a general fact-checker for arbitrary recommendation text, and
    it produces no entry at all when the field is `"unresolved"` (nothing was claimed).

  A `contradicted` result is reported, never silently resolved in either direction. Nothing in `verify`
  ever gets promoted into `keep` as a fact — a `confirmed` claim is reported as "confirmed", still
  carrying its original `ai_generated` provenance wherever it also appears.

### Real-transcript evidence

Tested against a real, extended (91-line, ~300KB) Claude Code transcript — a genuine test-driven
development cycle (a deliberately wrong test assertion, its failure, and its fix), an explicit
constraint ("do not push, do not add a remote"), and a deliberately unfinished feature. The compacted
checkpoint was ~11KB — a 96% reduction — while `verify` correctly flagged a real contradiction between
the AI summary's claimed `files_in_progress` and the files actually still uncommitted by the end of the
session.

That same real run surfaced two defects that mocked tests alone had not caught, both now fixed:

1. **JSON extraction was too strict.** The worker sometimes prefaces its answer with a sentence before
   the fenced JSON ("Now I have enough information to provide a comprehensive summary. Let me compile
   the JSON output:"). The parser used to require the fence at the very start of the text and failed
   outright. It now tries the whole trimmed text, then a ```` ```json ```` fence found anywhere in the
   text, then the outermost `{...}` span — the first of these that parses as a JSON object wins.
2. **Chronology could be misjudged.** On the same real transcript, the AI summary described a later,
   explicit instruction ("now commit these files") as a "violation" of an earlier one ("don't commit
   yet") that it had legitimately superseded. The summarisation prompt now explicitly instructs reading
   the whole transcript before judging anything a problem, and that a later instruction can supersede an
   earlier one. This measurably reduced the failure (the false "violation" claim in `known_problems`
   disappeared on re-test) but **prompt engineering cannot fully eliminate this class of error** — see
   Known limitations.

## Failure handling

A problem at any optional stage degrades to that block's own `unavailable` state; it never prevents the
rest of the checkpoint from being written, and never fabricates a result:

- Missing/invalid `--transcript`, missing `claude` executable, worker timeout, non-zero exit, or
  unparseable output → `session_summary: {"provenance": "unavailable", "reason": "..."}`.
- A malformed or incomplete checkpoint passed to Smart Compact's classifier → degrades to
  `smart_compact: {"provenance": "unavailable", "reason": "..."}` rather than raising.
- Missing/invalid repository state (not a git repo, no commits) is the one case that still fails the
  **whole** checkpoint outright (`CheckpointCaptureError`, non-zero exit, no file written) — this is
  unchanged from Slice 1 and is deliberate: it is the one thing a checkpoint exists to guarantee.

None of this can interrupt an existing Claude Code session: the AI worker is an independent, disposable
subprocess with no signalling relationship to whatever process invoked `th checkpoint`.

## Recovery behaviour

There is no `th resume` yet, so "recovery" here means: a checkpoint file is a plain, private
(`0600`) JSON file under `checkpoints/`, safe to read with any JSON tool. Its `integrity.content_sha256`
covers the entire document except itself (`verify_checkpoint_integrity()`), so hand-editing or corruption
is detectable — including edits to the `session_summary` or `smart_compact` blocks specifically, both of
which are computed and inserted before the final integrity hash is taken, so both are covered by it.
Nothing about checkpoint creation touches the transfer state machine, logical sessions, or project
registry, so a checkpoint can never leave one of those in an inconsistent state, and deleting a
checkpoint file has no effect on anything else.

## Known limitations

- **No `th resume` yet.** A checkpoint is a snapshot a human or a future tool can read; nothing
  currently reconstructs a session from one automatically.
- **No Codex support.** `--agent-type codex` records a caller-supplied label only; nothing verifies or
  interprets it, and no Codex-specific capture exists.
- **AI summary and Smart Compact cost real API calls.** Every `--ai-summary` invocation is a real, billed
  request (`claude-haiku-4-5-20251001` by default) — Terminal Handoff was previously zero-API-cost.
- **Real LLM output has inherent variance.** Across real test runs the worker has produced clean JSON,
  prose-prefaced JSON (now handled), and — occasionally — an empty response, which correctly degrades to
  `unavailable` rather than crashing. This is expected behaviour of a live worker, not a fixed reliability
  number.
- **VERIFY only catches structurally cross-checkable claims.** It can compare `tests_performed` against
  a real exit code, `files_in_progress` against the actual working tree, or `recommended_next_action`
  against `git.dirty` (and only for the narrow, explicit case of a recommendation claiming the tree is
  clean or dirty when the checkpoint's own git state says otherwise) — because those have
  deterministic counterparts elsewhere in the same checkpoint. It cannot catch a narrative or contextual
  misjudgement in the AI summary's own prose (such as the chronology error described above) — that would
  require re-reading the transcript, which Smart Compact deliberately does not do (see Architecture). The
  chronology prompt fix reduces this failure mode; it does not eliminate the underlying limitation that
  any LLM-based summary can misjudge nuance. This is exactly why no `ai_generated` value is ever promoted
  to `machine_verified` — the system is built assuming this class of error will keep happening
  occasionally, not that it has been solved.
- **`--add-dir`'s directory-level reachability** means the sandboxed worker's *filesystem visibility*
  extends to the isolated copy's directory as a whole, even though nothing else is ever placed there.
  There is no dependency on the transcript's real directory being safe.
- **The unittest suite never starts a real Claude Code session or worker**, matching the rest of
  Terminal Handoff's test philosophy — all AI-worker interaction is exercised through dependency
  injection in tests. Real-worker behaviour (like the two defects above) can only be caught by manual
  acceptance runs against a real transcript, not by the automated suite alone.
