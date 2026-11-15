# Close the nightly Gitleaks audit and main-red-watch blind spots

Removing the Telegram configuration surface in `#8889` also removed the
`.gitleaks.toml` allowlist for the empty `*_TELEGRAM_BOT_TOKEN=` template
lines. PR-time `secret scan (Gitleaks)` in `ci.yml` scanned only the commits
the pull request added, so it passed, while `scheduled-audits.yml`'s nightly
`secret-scan-history` scanned every ref's history and failed four nights in a
row on the two historical `.env.example` commits
(`40a3e60c3cea5994c402634bde54a5898debe62e` and
`ded34bf876d97fb6c6538bf4c903554ad7e1bb04`) where `generic-api-key` spanned the
newline onto `*_TELEGRAM_OWNER_ID=0`.

Three changes close each escape:

1. **Scoped historical allowlist**: `.gitleaks.toml` adds a dedicated
   `condition = "AND"` allowlist entry pinning those two commit SHAs,
   `^\.env\.example$`, `regexTarget = "match"`, and the exact multiline empty
   placeholder regex rather than widening the top-level `condition = "OR"`
   commit list.
2. **Redacted location metadata and policy-change full-history verification**:
   both `scheduled-audits.yml` and `ci.yml` write a `--redact` JSON report to
   `$RUNNER_TEMP/gitleaks.json` and, on non-zero exit, project only `RuleID`,
   `Commit`, `Date`, `File`, `StartLine`-`EndLine`, and `Fingerprint` (never
   `Secret`, `Match`, or `Message`) to stdout and `$GITHUB_STEP_SUMMARY` before
   removing the temporary report and exiting with Gitleaks' status code. When a
   change touches `.gitleaks.toml` (added, modified, or deleted), `ci.yml`
   fetches all remote heads and tags, unshallows when needed, and runs a
   full-history scan (`timeout-minutes: 35`); ordinary pull requests keep their
   incremental commit-range scan.
3. **`main-red-watch` coverage for `scheduled-audits.yml`**: the watch reads
   `scheduled-audits.yml` alongside its liveness stream (`NIGHTLY_STALE_AFTER`)
   outside `_FULL_SUITE_WORKFLOWS`. Deterministic audit steps
   (`AUDIT_CHECK_STEPS`) classify as `FAILURE_CHECK` and confirm a card on the
   first red run without inflating `unnamed` or waiting in two-strike
   `STATE_SUSPECTED`. Green workflow runs carry their `conclusion == "success"`
   job names (`RunFacts.green_jobs`) so a multi-cron workflow run where another
   job succeeded and `entry.job` was `skipped` never retires the open card.
