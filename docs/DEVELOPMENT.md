# Development

## Layout

```
src/terminal_handoff/
  core.py                     the implementation
  __init__.py cli.py detector.py manifest.py naming.py
  launcher.py statusline.py security.py state.py transfer.py   facade modules
  templates/successor-prompt.md
tests/
  _harness.py                 shared base case, fixtures, helpers
  test_detector.py            threshold and malformed input
  test_launcher.py            argv construction, model and effort
  test_naming.py              successor display names across generations
  test_state.py               lifecycle, generations, safety guards
  test_manifest.py            manifest, repository capture, permissions
  test_parent_stop.py         process binding and the shutdown boundary
  test_statusline.py          rendering and wrapping
  test_security.py            config integrity, restoration, boundary units
  test_transfer.py            the heartbeat gate and transfer state machine
  test_wrap_integrity.py      the wrapped command is unreachable from input
  test_install_layouts.py     install and uninstall under unusual layouts
  fixtures/                   synthetic status-line payloads
scripts/
  live-handoff-test.py        controlled live test: real windows, real signals
  coverage-check.sh           privacy, secret and personal-path scan
  verify-release.sh           version consistency, syntax, links, suite
```

`core.py` holds the implementation; the sibling modules are thin re-export
facades that name the public API by concern. The reasoning is in
[ARCHITECTURE.md](ARCHITECTURE.md#why-the-implementation-is-a-single-core-module).

## Running tests

```sh
python3 -m unittest discover -s tests -v     # everything
python3 tests/test_detector.py               # one module
python3 tests/test_state.py TestSafety       # one class
```

The suite:

- opens **no** Terminal window (`CLAUDE_TERMINAL_HANDOFF_TEST_MODE=1`)
- starts **no** Claude session (a stub executable stands in)
- consumes **no** context window (synthetic payloads only)
- touches **no** real repository (throwaway repos under a temp directory)
- isolates state via `CLAUDE_TERMINAL_HANDOFF_HOME`

It takes about three minutes; the Git tests genuinely create merge, rebase,
cherry-pick and revert conflicts rather than faking the marker files, and the
shutdown tests start real short-lived processes and signal them.

Two things the suite does start, deliberately:

- **real processes**, as stand-ins for a Claude session. They are symlinks to
  `/bin/sleep` named `claude`, because macOS records the executed path, so
  `ps -o comm=` reports the symlink's own name — which is the identity Terminal
  Handoff binds on. A copied system binary is killed by the code-signing check,
  and a shell script reports its interpreter, so neither works.
- **real signals**, to those stand-ins only. Every binding a test may signal is
  built from a PID the test started itself; no test signals a process found by
  walking ancestry.

## The controlled live test

```sh
python3 scripts/live-handoff-test.py           # run and clean up
python3 scripts/live-handoff-test.py --keep    # keep the throwaway state
```

This one **does** open real Terminal windows, run real `osascript`, trace real
ancestry and send a real `SIGTERM`. It still starts no Claude session: the
stand-in is a small compiled program named `claude` that drives the real
status-line command with synthetic status JSON. Everything lives in a throwaway
`CLAUDE_TERMINAL_HANDOFF_HOME`.

It runs a three-generation chain, proves `Ranger -> Ranger 2 -> Ranger 3`,
proves the exact parent process is stopped while its Terminal stays open, proves
an unrelated Claude process is untouched, and proves that an invalid successor
heartbeat leaves the parent running. A recorded run is in
[LIVE_TEST_EVIDENCE.md](LIVE_TEST_EVIDENCE.md).

## Running the CLI from a checkout

```sh
python3 src/terminal_handoff/core.py status
python3 src/terminal_handoff/core.py evaluate < tests/fixtures/at_threshold.json
python3 src/terminal_handoff/core.py coverage
```

`core.py` is self-contained and runnable directly, which is also how the
detached launcher re-invokes it.

## Writing a test

Subclass `THTestCase` from `_harness`:

```python
def test_something(self):
    payload = self.payload(percent=90.0, effort="xhigh")
    ok, _ = self.trigger_and_wait(payload)
    self.assertTrue(ok)
    record = json_file(self.launch_record(payload["_session_id"]))
    self.assertEqual(record["argv"][record["argv"].index("--effort") + 1], "xhigh")
```

`self.payload()` builds a valid status-line payload and a matching transcript;
override any field to make it invalid. `trigger_and_wait()` runs the status line
and waits for the detached launcher's record.

## House rules

1. **Never weaken a test to go green.** Diagnose whether the implementation or
   the fixture is wrong, fix that, and say which it was.
2. **No silent fallbacks.** Unpreservable model or effort is a visible failure,
   not a substitution.
3. **Fail closed.** Missing, null or malformed input must never trigger.
4. **Keep the status-line path cheap.** Expensive work belongs in the detached
   launcher.
5. **Nothing personal in the repository.** `TestRepositoryPrivacy` scans the
   whole tree; sentinel strings in tests are assembled from fragments so the
   tree contains no literal match.

## Pre-commit checks

```sh
./scripts/coverage-check.sh     # privacy, secrets, personal paths
./scripts/verify-release.sh     # metadata, version consistency, syntax, tests
```

CI runs the same checks on pull requests and pushes to `main`.

## Debugging a live installation

```sh
python3 ~/.claude/terminal-handoff/terminal-handoff.py status
tail -f ~/.claude/terminal-handoff/logs/terminal-handoff.log
```

To rehearse a launch without opening a window, export
`CLAUDE_TERMINAL_HANDOFF_TEST_MODE=1`; the constructed command lands in
`completed/<session-id>.launch.json`.

## Releasing

1. Update `CHANGELOG.md`.
2. Bump the version in `pyproject.toml`, `VERSION`, and
   `TERMINAL_HANDOFF_VERSION` in `core.py` — `verify-release.sh` checks they
   agree.
3. Run the full suite and both scripts.
4. Tag annotated: `git tag -a v1.x.0 -m "..."`.
5. Push the tag and create the GitHub release.
