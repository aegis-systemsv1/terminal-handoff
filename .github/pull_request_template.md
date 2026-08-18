## What this changes

<!-- A short description of the behaviour change. -->

## Why

<!-- The problem being solved. Link an issue if there is one. -->

## How it was verified

<!-- Which tests were added or changed, and what you ran. -->

```
python3 -m unittest discover -s tests
```

## Checklist

- [ ] The full test suite passes
- [ ] `./scripts/coverage-check.sh` passes (no personal paths, secrets or runtime data)
- [ ] `./scripts/verify-release.sh` passes
- [ ] New behaviour is covered by tests
- [ ] **No test was weakened, skipped or deleted to make the suite pass**
- [ ] No silent fallback to a different model or effort was introduced
- [ ] Behaviour still fails closed on missing, null or malformed input
- [ ] The status-line path remains cheap (expensive work stays in the detached launcher)
- [ ] Documentation updated if behaviour or configuration changed
- [ ] `CHANGELOG.md` updated for a user-visible change
