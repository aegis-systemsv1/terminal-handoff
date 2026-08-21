# Controlled live-test evidence

Produced by `scripts/live-handoff-test.py` against the installed Terminal
Handoff 1.1.1 runtime on macOS. No path below is a real one: the run wrote its
state to a throwaway directory under the system temporary directory and removed
it afterwards.

This test is deliberately **not** a unit test. It opens real Apple Terminal
windows through real `osascript`, runs the real launcher, traces real process
ancestry with `ps`, and sends a real `SIGTERM` to a real process. What it does
not do is start a real Claude session or consume a real context window: the
stand-in for `claude` is a small compiled program named `claude`, which is the
identity Terminal Handoff actually binds on, driving the real status-line
command with synthetic status JSON in the official schema.

Reproduce it with:

```sh
python3 scripts/live-handoff-test.py
```

## Result

**31 of 31 checks passed**, against the installed runtime.

| Result | Check | Evidence |
|---|---|---|
| PASS | an unrelated stand-in Claude session is running | pid 40408 |
| PASS | generation 1 handed off | launch records: 2 |
| PASS | Ranger hands off to Ranger 2 | --name 'Ranger 2' |
| PASS | the Terminal window title is the display name | Ranger 2 |
| PASS | the base name came from the live Claude session name |  |
| PASS | no internal chain identifier is exposed in the name |  |
| PASS | Ranger 2 hands off to Ranger 3 | --name 'Ranger 3' |
| PASS | generation 2's base name came from trusted chain state |  |
| PASS | a session named 'Ranger 2' actually started |  |
| PASS | the successor received the exact model | claude-opus-5[1m] |
| PASS | the successor received the exact effort | high |
| PASS | the successor started in the correct working directory |  |
| PASS | the successor knows its generation |  |
| PASS | a session named 'Ranger 3' actually started |  |
| PASS | a transfer record exists for generation 1 |  |
| PASS | the transfer completed | TRANSFER_COMPLETE |
| PASS | the parent was stopped |  |
| PASS | the parent was stopped with SIGTERM and no escalation | {"attempts": 1, "enabled": true, "escalates": false, "pid": 40425, "signal": "SIGTERM"} |
| PASS | continuation ownership moved to the successor |  |
| PASS | the transfer followed the documented state machine | LAUNCHING -> SUCCESSOR_VERIFIED -> PARENT_STOP_REQUESTED -> TRANSFER_COMPLETE |
| PASS | every successor heartbeat check passed | chain_matches, context_percentage_live, cwd_matches, effort_matches, generation_matches, model_matches, session_id_is_fresh, session_id_present, session_id_unused |
| PASS | the generation-1 Claude process is no longer running | pid 40425 |
| PASS | the parent's Terminal still has a live shell on ttys021 | 40417 login / 40418 -zsh |
| PASS | the unrelated Claude session was never touched | pid 40408 |
| PASS | Terminal windows are still open | count: 19 |
| PASS | the generation ceiling stopped the chain | launch records: 2 |
| PASS | the handoff launched a successor |  |
| PASS | the transfer failed closed | no verified successor heartbeat within 25s; the parent session is left fully operational |
| PASS | the parent was never stopped |  |
| PASS | the wrong model was the recorded reason | ["model_matches"] |
| PASS | the parent Claude process is still running | pid 40630 |

## What this proves

1. A session named `Ranger` hands off to `Ranger 2`, and `Ranger 2` hands off to
   `Ranger 3` — in the `claude --name` argument, in the Terminal window title,
   and in the session the successor actually reports.
2. Generation 2's base name came from trusted chain state, not from parsing its
   visible name.
3. No internal chain identifier appears in any human-facing name.
4. The successor received the exact model, the exact effort level and the
   correct working directory.
5. All nine successor heartbeat checks passed before any shutdown was requested.
6. The transfer followed the documented state machine exactly:
   `LAUNCHING -> SUCCESSOR_VERIFIED -> PARENT_STOP_REQUESTED -> TRANSFER_COMPLETE`.
7. The exact bound parent process was stopped with a single `SIGTERM`, with one
   attempt and no escalation.
8. The parent's Terminal window kept a live `login` and `-zsh` on its TTY: the
   window remained open at a shell prompt.
9. An unrelated stand-in Claude process, running the whole time, was never
   touched.
10. When a successor reported a model different from the one it was launched
    with, the transfer failed closed with `model_matches` recorded as the
    reason, and the parent Claude process was still running.
11. The generation ceiling stopped the chain rather than letting it run away.
