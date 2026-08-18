# Contributing

Thanks for your interest in Terminal Handoff.

## Before you start

This project automates opening Terminal windows and launching agent sessions on
a user's machine. That makes correctness and restraint more important than
features. A change that makes Terminal Handoff trigger more eagerly, fall back
silently, or trust unvalidated input will not be accepted, however convenient it
looks.

## Ground rules

1. **Never weaken a test to make the suite pass.** If a test fails, either the
   implementation is wrong or the test's fixture is wrong. Fix the real cause
   and say which it was.
2. **No silent fallbacks.** If the reported model or effort cannot be preserved,
   the correct behaviour is to fail visibly, log the reason and leave the
   outgoing session working — never to launch something different.
3. **Fail closed.** When input is missing, null, malformed or unverified,
   Terminal Handoff must not trigger.
4. **Nothing personal in the repository.** No absolute home paths, real session
   IDs, chain IDs, transcripts, manifests, logs or private configuration. The
   suite enforces this; see `TestRepositoryPrivacy`.
5. **The status line must stay fast.** Anything expensive belongs in the
   detached launcher, not on the status-line path.

## Development setup

No dependencies are required beyond Python 3.9+.

```sh
git clone <repo-url> Terminal-Handoff
cd Terminal-Handoff
python3 -m unittest discover -s tests -v
```

The suite opens no Terminal window, starts no Claude session, consumes no
context window and modifies no real repository. Git tests build throwaway
repositories under a temporary directory.

## Making a change

1. Open an issue first for anything beyond a small fix, so the behaviour can be
   agreed before it is built.
2. Add or extend tests covering the behaviour you changed.
3. Run the full suite.
4. Run the privacy and secret scans:
   ```sh
   ./scripts/coverage-check.sh
   ./scripts/verify-release.sh
   ```
5. Keep commits focused and their messages descriptive.

## Commit style

Conventional prefixes are used: `feat:`, `fix:`, `test:`, `docs:`, `ci:`,
`chore:`, `refactor:`.

## Reporting bugs

Use the bug report template. Include your macOS version, Claude Code version,
Python version, the relevant lines from
`~/.claude/terminal-handoff/logs/terminal-handoff.log`, and the output of
`terminal-handoff.py status`.

**Redact before posting.** Logs and manifests can contain repository paths and
session identifiers. Replace anything you would not publish.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
