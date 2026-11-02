---
type: doc
title: CI diagnosis and monitor contracts
description: "`scripts/ci/pull_requests/diagnose.py` — the `scripts/ci/cli.py --diagnose` classifier: the ordered failure-signature table, the best-effort read policy, and the PR-level issue rules of the monitor suite."
tags:
- scripts
- ci
---

# CI diagnosis and monitor contracts

`diagnose.py` classifies a PR's failing CI checks from the rollup and job log
tails. Diagnosis only — no repair action is taken here; the operator executes
the suggested action. Evidence sources: the PR's check rollup + job log tails,
and the queue-era synthetic `trunk-merge/pr-<n>/*` test PRs of the PR under
diagnosis — their failed logs are where a queue attempt's failures are read,
which still resolves the history left by the retired queue.

Nothing here queries Trunk. Its API stopped authorizing on 2026-11-02 (401 in
the last export run, 403 to a direct probe), and a probe that can only fail is
worse than no probe: it reads as "temporarily down" instead of "gone".

The suite's usage-ledger fixtures fix their day relative to now: the
`--ci-usage` rollup window is a rolling `--ci-usage-days` x 24h, so a literal
"recent" day ages out (2026-09-13: literal fixtures crossed midnight UTC and
turned every backend shard red).

Every remote read degrades instead of erroring: a failed or timed-out `gh` call, unreadable JSON or a non-zero exit yields None or empty text, never an exception; failed per-job log enrichment leaves the rollup's classification standing, and a PR whose view cannot be read still returns a diagnosis carrying `gh_error`.

Malformed synthetic-PR containers and non-object rows are treated as missing
diagnostic enrichment. Invalid rollup conclusion types are skipped, so an
unhashable value cannot interrupt classification of valid sibling checks.

The adjacent job-rerun reader has a stricter boundary because it can request
recovery actions: malformed response containers or rows, invalid identifiers,
and incorrectly typed run/job fields raise `CiJobRerunError`. It validates all
listed jobs before any rerun POST. Empty run/job lists are successful empty
reads; a missing conclusion is invalid, while an explicit null conclusion is
valid for an unfinished job. A missing job
`run_id` still uses its containing workflow run. Current-head selection,
superseded-run filtering and waiting for a run to complete remain unchanged.

Runner-side install failures are classified as `infrastructure: ...` with a rerun
as the suggested action, never as a code failure: an unreachable archive or a
held dpkg lock fails a check exactly like a red test, and the label is what keeps
the two apart in the report. That family covers both install actions and the
lock wait documented in `scripts/provision/docs/apt-lock.meiva.okf.md`;
`ci-rerun.yml` retries attempt 1 on its own.
