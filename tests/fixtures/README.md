# Test fixtures

Synthetic Claude Code status-line payloads built from the official schema.

Every identifier is fabricated. `<TRANSCRIPT>` and `<WORKDIR>` are placeholders
that the test harness rewrites to paths inside a temporary directory, so the
fixtures contain no machine-specific or personal paths.

| Fixture | Purpose |
|---|---|
| `below_threshold.json` | 79% — must not trigger |
| `at_threshold.json` | 80% — must trigger |
| `null_percentage.json` | `used_percentage: null` — must not trigger |
| `rate_limit_high_context_low.json` | rate limits 92%/88%, context 40% — must not trigger |
| `missing_effort.json` | no `.effort` — must trigger, `--effort` omitted |
