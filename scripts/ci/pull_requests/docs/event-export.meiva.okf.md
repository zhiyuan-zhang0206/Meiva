---
type: doc
title: CI Sampler Event Writer
description: Explicit entry image and event-writer ownership for PR-flow and CI-run sampling.
tags: []
---

# CI Sampler Event Writer

Standalone `pr_flow_export.py` and `runs_export.py` capture their source image
after argument validation and before remote collection. Only actual emission
constructs their writer through `export_process.owned_event_pipeline`: one
CodeVersion and nonexempt ProcessDbGate supply its live database factory.
A missing loaded SHA remains unknown; no dial captures the current checkout.

The writer receives a finite two-second stop/join budget. An unfinished receipt
is reported when the writer or exporter has not finished; this is not a drain guarantee;
the telemetry binding retains the actual pipeline for final process draining.
An export error stays primary if writer cleanup also fails, with the original
worker error retained by the pipeline for repeated collection.

The Dev/CI metrics schedule passes its existing ClientSet producer into the
CI-run sampler. The sampler borrows that writer without capturing another image,
creating another gate, or closing the schedule's resources. Both invocation paths
retain the exporter process dimension. Dry runs create neither image nor writer;
failed authoritative collection still aborts before persistence and emission.

PR-flow cache and snapshot replacement use the shared atomic writer. Each new
file is private to the Meiva user (0600); interrupted or failed replacement leaves
no partial report. Consumers run as that same user. This tightens the earlier
umask-dependent temporary-file mode without changing the JSON representation.

GitHub command transport is required at each sampler entry and fetch boundary.
The script and schedule roots supply standard-library subprocess.run, retaining
60-second command timeouts, inherited environment/cwd and each sampler's rc policy.
CI-run retries use the required Timers port with the existing bounded backoff;
the schedule's direct workflow query uses the same raw transport without loading
clock configuration. Page limits remain independent: CI-run cursors continue,
closed-PR population exhaustion fails, and PR-flow timeline exhaustion is reported
as truncation. Collection, cache writes and event emission run their real owners.

CI-run persistence retains its separate fixed `.json.tmp` and replace implementation;
its file permissions follow umask. The PR-flow private-file guarantee above does
not apply to CI-run files. Neither sampler claims a transaction across report files.

`runs_api.py` owns the CI-run sampler's read-only GitHub populations, retry
accounting and wire timestamp decoder. `runs_export.py` retains cache, metric
aggregation and orchestration and calls that owner directly. The schedule's
workflow alert query also calls the real read owner; its daily-loop root binds
the standard process transport and timers without changing the slot callback shape.
