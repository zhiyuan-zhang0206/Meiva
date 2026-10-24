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
`schedules/plans/c9_window.py` decides the window, the day label (the slot's date
in the cluster timezone, which can differ from its UTC date) and the summary;
`schedules/jobs/c9_report.py` reads GitHub through the host, appends the ledger and
emits. The minute rates in the summary are the ones `accounting.py` owns; the shell
hands them in.

Idempotency has two layers — the slot claim in `schedules/jobs/catchup.py` is
at-most-once, and the ledger append is keyed by run id — so re-running a window
is a no-op for runs already recorded. A failed reconciliation does not retry
(the claim stays committed); its message names the manual backfill command and
goes to the report recipient (`MEIVA_CI_USAGE_REPORT_AGENT`, else the agent labelled
for the P0 lead).

The manifest entry carries the schedule's user-facing description; the claim
semantics every template shares are in `schedules/README.md`.
