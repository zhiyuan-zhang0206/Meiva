---
type: doc
title: C9 daily report
description: Window, ledger idempotency and failure handling of the daily CI-minute reconciliation.
tags:
- schedules
- telemetry
---

# C9 daily report

The schedule fires at 05:00 cluster time, reconciles the trailing 24 hours of CI
runs into the attribution ledger owned by `scripts/ci/pull_requests/accounting.py`,
and emits one `ci_usage_daily` telemetry event carrying the day's totals. The
per-agent breakdown stays in the ledger; `ci_utils.py --ci-usage` reads it on
demand.

The reconciliation window is the claimed slot handed to the callback, so a
catch-up boot that fires several missed slots reconciles each slot's own
05:00-05:00 day: windows stay gapless and each day emits exactly one event.
Idempotency has two layers — the slot claim in `schedules/catchup.py` is
at-most-once, and the ledger append is keyed by run id — so re-running a window
is a no-op for runs already recorded. A failed reconciliation does not retry
(the claim stays committed); its message names the manual backfill command.

The manifest entry carries the schedule's user-facing description; the claim
semantics every template shares are in `schedules/README.md`.
