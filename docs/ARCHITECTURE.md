# Architecture

> Behavioural decisions with a rationale worth keeping are recorded in
> [decisions/](decisions/0001-parent-shutdown-and-successor-naming.md).

Terminal Handoff has one job: notice that a Claude Code session is nearly full, and hand its work to a fresh session that can verify what it inherits.

Everything below follows from two constraints:

1. **The status line runs constantly and must stay fast.** Anything slow has to happen somewhere else.
2. **Nothing may be trusted.** Not the JSON, not the transcript, not the parent's claims about its own work.

---

## 1. Status-line monitoring

Claude Code invokes the configured `statusLine` command on every status refresh and writes the official status-line JSON to its stdin. Terminal Handoff is that command.

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant TH as terminal-handoff statusline
    participant W as wrapped status line (optional)
    participant D as detached launcher

    CC->>TH: status-line JSON on stdin
    opt an existing status line is configured
        TH->>W: the identical stdin bytes
        W-->>TH: stdout, preserved byte-for-byte
    end
    TH->>TH: parse, validate, decide
    alt below threshold
        TH-->>CC: "<wrapped output> · TH 42%"
    else at or above threshold
        TH->>TH: atomic one-shot claim
        TH->>D: spawn detached, return immediately
        TH-->>CC: "<wrapped output> · TH launching"
    end
```

Only these fields are read:

| Field | Use |
|---|---|
| `.context_window.used_percentage` | **the sole threshold input** |
| `.session_id` | one-shot identity, state keying |
| `.transcript_path` | validated, recorded by path, never read into context |
| `.workspace.current_dir` / `.project_dir` | successor working directory |
| `.model.id` / `.model.display_name` | exact model to preserve |
| `.effort.level` | exact effort to preserve (optional field) |
| `.session_name` | **the chain's base display name**, captured once at generation 1 |
| `.version`, `.workspace.git_worktree` | recorded in the manifest |

Usage is never estimated from transcript size, line count, elapsed time, message count, token guesses or session cost. `.rate_limits.five_hour.used_percentage` and `.rate_limits.seven_day.used_percentage` are **never** read: account rate limits are a different measurement from context usage, and confusing the two would fire handoffs at random.

## 2. Threshold detection

The decision runs nine gates in order. Any gate can stop it; none can be skipped.

```mermaid
flowchart TD
    S[status JSON] --> G1{kill switch set?}
    G1 -- yes --> DIS[TH disabled]
    G1 -- no --> G2{used_percentage present,<br/>numeric, 0-100?}
    G2 -- no --> RDY[TH ready — never trigger]
    G2 -- yes --> REC[record this session's observation]
    REC --> G3{>= threshold?}
    G3 -- no --> PCT[TH nn%]
    G3 -- yes --> G4{session, model, effort,<br/>transcript all valid?}
    G4 -- no --> BLK[TH blocked]
    G4 -- yes --> G5{already handed off?}
    G5 -- yes --> HO[TH handed off]
    G5 -- no --> G6{generation ceiling reached?}
    G6 -- yes --> BLK
    G6 -- no --> G7{circuit breaker open?}
    G7 -- yes --> CIR[TH circuit open]
    G7 -- no --> G8{cooldown elapsed?}
    G8 -- no --> RET[TH retrying]
    G8 -- yes --> G9{>= MIN_OBSERVATIONS<br/>of its own readings?}
    G9 -- no --> RDY
    G9 -- yes --> TRIG[trigger]
```

`used_percentage` is legitimately `null` in two states — before the first API response, and after compaction. Both are non-triggering. Terminal Handoff never triggers on a missing, null, non-numeric or out-of-range value.

Gate 9 is what stops a newborn successor from firing on a stale or inherited reading: a session must have produced at least two of its *own* non-null percentage readings, keyed by its own `session_id`.

## 3. Atomic trigger claim

The status line can run concurrently. The claim is therefore a filesystem primitive, not a lock held in memory:

```python
os.open(th_path("triggered", session_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
```

`O_EXCL` gives exactly one winner. Every loser gets `EEXIST` and renders `TH handed off`.

The key is `session_id` — never PID, shell PID, process start time, working directory or repository name. A successor has a different session ID, so it cannot inherit its parent's claim; that single choice is what makes "one trigger per session" and "unlimited generations per chain" coexist.

## 4. Manifest creation

The detached launcher builds the manifest. It records identity, exact model, exact effort, the trigger percentage, chain and generation, applicable `CLAUDE.md` paths, and a repository snapshot: root, branch, HEAD, `origin/main`, ahead/behind, staged, modified and untracked files, recent commits, and any active merge, rebase, cherry-pick, revert or bisect.

It records **no** credentials, tokens, environment dumps, file contents, transcript contents or shell history. Files are `0600`; directories `0700`.

Git capture never happens on the status-line path — only here, in the detached process.

## 5. Terminal launch

```mermaid
flowchart LR
    M[manifest] --> P[render successor prompt<br/>to a 0600 file]
    P --> A[build argv:<br/>--model, --effort, --name, bootstrap]
    A --> V{argv safe?<br/>no --continue/--resume/<br/>--fork-session/bypass}
    V -- no --> F[fail_launch]
    V -- yes --> SH[generate launch script,<br/>every value shlex-quoted]
    SH --> OS[osascript: one Terminal window]
    OS --> T[claude, fresh session]
```

Three quoting boundaries, each handled explicitly:

- **argv** — the model ID and effort go in as separate list elements, never as shell text.
- **the generated shell script** — every path and value passes through `shlex.quote`. Bracketed model IDs such as `claude-opus-5[1m]` are a real hazard: unquoted under zsh, `[1m]` is a glob pattern and the command fails with `no matches found`.
- **AppleScript** — backslashes and double quotes escaped; paths containing quotes or control characters are rejected before this point rather than escaped around.

The full successor instructions live in a `0600` file. Only a short bootstrap pointer is passed as an argument, so the instructions never appear in a process listing.

## 6. Fresh successor creation

The successor is an ordinary interactive session. It never uses `--continue`, `-c`, `--resume`, `-r`, `--fork-session`, `--session-id`, the parent's session ID, compaction, or transcript replay. A denylist is asserted against the constructed argv as defence in depth before anything is executed.

## 7. Transcript-analysis subagent

```mermaid
flowchart TD
    S[successor main context] -->|path only| A[temporary subagent]
    A -->|reads JSONL| T[(parent transcript)]
    A -->|concise structured brief| S
    S -->|independently| R[(live repo + filesystem)]
    T -.->|never enters| S
```

An 80%-full transcript would consume most of a fresh window and trigger another handoff almost immediately, so the successor's main context never receives it. The subagent returns objective, instructions, constraints, decisions, completed work, files touched, commands run, commits, tests and their exact results, approvals given and still required, blockers, the proposed next action, and — importantly — the claims that need independent verification.

## 8. Repository verification

The successor re-derives `pwd`, repository root, branch, HEAD, `origin/main`, ahead/behind, status, staged and untracked files, and any in-progress merge, rebase, cherry-pick or revert. Where the live state disagrees with the transcript, **the live state wins**. Every existing change is treated as user-owned: no reset, clean, discard, overwrite, revert, amend, force-push, delete or stash without clear authority.

## 9. Successor heartbeat — the transfer gate

A window opening is not proof that a session started correctly — an invalid model only fails at startup, after `claude` has already been exec'd. So the parent's manifest is not closed out, and the parent is not stopped, until the successor reports back.

The launcher exports `CLAUDE_TERMINAL_HANDOFF_MANIFEST`, `..._CHAIN_ID`, `..._GENERATION`, `..._BASE_NAME` and `..._TRANSFER` into the successor's Terminal window. The successor's own status line sees them and writes back its session ID, model, effort, working directory, chain, generation and live context percentage.

Nine checks must all pass, across two heartbeats:

| Check | Proves |
|---|---|
| `session_id_present` | there is a session at all |
| `session_id_is_fresh` | it is not the parent |
| `session_id_unused` | it is not a session already recorded elsewhere in this chain |
| `model_matches` | the exact model was preserved |
| `effort_matches` | the exact effort — including "none" — was preserved |
| `cwd_matches` | it started in the right directory |
| `chain_matches` | it belongs to this chain |
| `generation_matches` | it is the generation that was launched |
| `context_percentage_live` | it is running, not merely spawned |

Any failure records `successor_mismatch` in the manifest, `successor_rejected` in the transfer, and leaves the parent alone.

```mermaid
stateDiagram-v2
    [*] --> eligible
    eligible --> launching: atomic claim won
    launching --> launched: osascript returned 0
    launched --> successor_started: first heartbeat<br/>(fresh session ID)
    successor_started --> completed: second heartbeat,<br/>all nine checks pass
    successor_started --> successor_mismatch: any check fails
    launching --> failed: launch error
    launched --> failed: launch error
    failed --> eligible: bounded retry (max 2)<br/>after cooldown
    failed --> [*]: retries exhausted
    completed --> [*]
```

The heartbeat also refuses to treat a session as its own successor, which would otherwise be possible if a manifest path were passed to the wrong session.

## 9a. Successor naming

The chain carries a **base display name** and a **chain ID**, and they are different things.

- The base display name is human-facing: `Ranger`. It is captured once, from `.session_name`, when the chain is created, written to `chains/<chain-id>.json`, and never rewritten. Generation *n* is displayed as `<base> <n>`; generation 1 keeps the base name unchanged.
- The chain ID is machine-safe hex used to key state. It never appears as a session name.

The generation number is read from trusted chain state — `chains/<chain-id>.json` records which session ID occupies which generation — and only falls back to the launcher's exported `CLAUDE_TERMINAL_HANDOFF_GENERATION` when chain state does not yet know the session. It is never derived by parsing digits off a visible name, so `Project 42` hands off to `Project 42 2`.

If no session name is available, the documented fallback is `Terminal Handoff <chain-id[:8]>`. No repository, directory or project name is ever substituted.

## 9b. Transfer of ownership and parent shutdown

Two agents in one repository is the failure this prevents. There is exactly one boundary.

```mermaid
stateDiagram-v2
    [*] --> LAUNCHING: successor launched
    LAUNCHING --> SUCCESSOR_VERIFIED: all nine heartbeat checks pass
    SUCCESSOR_VERIFIED --> PARENT_STOP_REQUESTED: parent process identity re-proved
    PARENT_STOP_REQUESTED --> TRANSFER_COMPLETE: parent exited
    LAUNCHING --> TRANSFER_FAILED: heartbeat timeout,<br/>launch failure, unbound parent
    SUCCESSOR_VERIFIED --> TRANSFER_FAILED: identity could not be re-proved
    PARENT_STOP_REQUESTED --> TRANSFER_FAILED: parent did not exit<br/>(never escalated)
    TRANSFER_COMPLETE --> [*]
    TRANSFER_FAILED --> [*]
```

`LAUNCHING` and `SUCCESSOR_VERIFIED` mean the **parent** owns continuation. `PARENT_STOP_REQUESTED` and `TRANSFER_COMPLETE` mean the **successor** does. `TRANSFER_FAILED` returns ownership to the parent, which is still running.

Transitions happen under an exclusive lock, are refused when illegal, and append to a history carrying the reason and the requesting PID. Repeating a transition is refused, which is precisely what stops a duplicate status-line invocation or a second supervisor from requesting a second shutdown; a supervisor is additionally claimed per transfer with `O_CREAT|O_EXCL`.

**Binding the parent.** The Claude process is bound inside the status-line process at trigger time, because the detached launcher starts its own session and has no ancestry to trace. `ps` walks upward to the nearest process whose executable file is named exactly `claude`; the candidate must be within six levels, owned by the same UID, not this process, and working in the same directory as the session in the payload. PID, start time, TTY, UID, executable name, working directory, session, chain and generation are all recorded.

**Stopping it.** A detached supervisor waits for `SUCCESSOR_VERIFIED`, re-proves every recorded value with no wait between the check and the signal, then sends one `SIGTERM` — at most twice, never `SIGKILL`, never to a process group, never by name. The parent's shell and Terminal window are untouched, so the window stays open at a prompt.

**Restart is deterministic.** Running the supervisor again from any state does the same thing: it resumes waiting from `LAUNCHING`, stops from `SUCCESSOR_VERIFIED`, and exits without signalling from `PARENT_STOP_REQUESTED`, `TRANSFER_COMPLETE` or `TRANSFER_FAILED`.

## 10. Multi-generation continuation

```mermaid
flowchart LR
    G1["gen 1<br/>session A"] -->|A's one-shot| G2["gen 2<br/>session B"]
    G2 -->|B's one-shot| G3["gen 3<br/>session C"]
    G3 -->|C's one-shot| G4["gen 4<br/>…"]
    G1 -.->|chain abc123| G4
```

A chain ID and a base display name are generated once and inherited through environment variables and `chains/<chain-id>.json`; the generation number increments, and so does the visible name — `Ranger`, `Ranger 2`, `Ranger 3`. Each successor's manifest points at its parent's. **One trigger per session; unlimited generations per chain.** `CLAUDE_TERMINAL_HANDOFF_MAX_GENERATIONS` imposes a ceiling if you want one; unset means unlimited, and there is no hidden stop.

## 11. Failure and retry

Every launch failure — no `claude` executable, unsafe or missing working directory, prompt render failure, unpreservable model or effort, unsafe argv, osascript failure, circuit open — routes through one function, `fail_launch()`. It records a failure state with diagnostics, never marks the handoff completed, applies the cooldown, and releases the one-shot claim for at most **2** retries before giving up and leaving the claim in place.

Notably, an unpreservable model or effort is a *failure*, not a fallback. Terminal Handoff will not open a successor with different settings.

## 12. Circuit breaker

Three launches within ten minutes trips the breaker: launching is suspended for thirty minutes, the event is logged, and the status line shows `TH circuit open`. Activation is never hidden. `reset-circuit` clears both the flag and the counting window — resetting the flag alone would let already-counted launches re-trip it immediately.

---

## Why the implementation is a single core module

The repository presents `core.py` plus eight thin facade modules (`detector`, `manifest`, `launcher`, `naming`, `statusline`, `security`, `state`, `transfer`) that re-export the public API by concern.

`core.py` is the exact implementation that was built, tested and live-verified for 1.0.0, and hardened in place since. Mechanically splitting a 3,000-line module across six files for appearance would mean rewriting import graphs and shared state handling — real risk, no behavioural gain, against code whose value is precisely that it has been proven end to end.

So the split is deliberately a **naming layer**, not a rewrite:

- `core.py` holds the implementation and stays behaviourally identical to the verified version.
- The facades give tests and callers a stable, concern-oriented API surface, and document what belongs where.
- If a later release genuinely needs the physical split, the facades are already the seams to split along, and their public names will not change.

This is recorded here rather than left implicit, because a reader who opens `detector.py` and finds only imports deserves to know it was a choice.
