---
type: doc
title: CI Test Infrastructure
description: CI tool responsibilities, explicit evidence contexts and test feedback ownership.
tags: [ci, testing]
---

# CI Test Infrastructure

Collection before sharding has an opt-in, collect-only proof tool:
[[scripts/ci/docs/file-shard-shadow/file-shard-shadow.meiva.okf.md]]. Required backend
shards build fresh per-runner plans and validate whole-file groups before execution.

The weekly dependency report and its single tracked issue are owned by
`dependency_audit.py`: [[scripts/ci/docs/dependency-audit.meiva.okf.md]].

`pull_requests/` holds `accounting.py`, `job_rerun.py`, `runs_export.py` — CI-minute attribution and job/workflow tooling; `status.py` owns reusable GitHub CI verdicts; `monitor.py` queries and waits without queue actions; `commands.py` routes the existing CLI; `owner_operations.py` owns optional authenticated queue submission/eviction and GitHub job re-runs. `scripts/ci/cli.py` preserves the CLI and the public `check_ci`, `CIResult`, `CIStatus` imports. Pending or missing workflow execution stays non-green, including aged zero-job queued runs; no status override exists. Cancelled unexpanded matrix-template names are discarded; concrete-named cancellations remain evidence unless replaced by a newer completed result for the same name; normal conclusion and workflow gates then apply. `pull_requests/trunk_api.py` — the Trunk merge-queue HTTP calls and PR base-freshness read shared by owner tools and `pr_flow_export.py`; `pull_requests/diagnose.py` — the `scripts/ci/cli.py --diagnose` failure classifier; `coverage_gates.py` — backend coverage gates; `test_selector.py` — PR test selection feeding `ci.yml` (shared runtime facts through `test_impact.py`: transitive consumers, fixture scopes and base/head impact, plus package owners; incomplete dynamic inputs explicitly keep FULL and parser failures remain gating; `classify_path` gives every tracked path a class), with test hosts owned by pytest `testpaths`; `refresh_test_durations.py` — complete duration measurement/merge ([how CI gets the timings](duration-refresh.md)); `shard_counts.py` — the executed-test counts (per shard and directory, and the total against the previous queue run) that the backend shards and the `backend test counts (all shards)` job print, plus the root leak guard's findings from the JUnit properties (one annotation and an artifact; a shard that left no JUnit report or ran no test fails there: [[.github/test-gate.meiva.okf.md]]); `migration_smoke.py`, `pgvector_runtime_smoke.py`, `helper_smoke/` (the macOS permissions-helper lane: the signing smoke and the two-section chain smoke) — smoke/verification gates.

CI entry points supply required host, clock and URL ports and the utility
checkout's workflow-presence fact. The commands root captures the process
environment only when an explicit mapping was not supplied; status probes never
discover another checkout implicitly. The CLI composes that fact from its source
root; the watcher uses CI_UTILS rather than the queried REPO_ROOT. The ledger path
is configurable data. Timeouts preserve failed-read and unconfirmed-write outcomes. Stdlib-only tools stay dependency-free.
Coverage reports require integer line counts with `0 <= covered <= total`;
malformed report objects fail before any coverage verdict is printed.
Visual baseline refresh uses the workflow's 45-minute child budget and restores
moved references on failure. Demo and deployment probes share the passed opener.

Runtime impact propagates consumer bitsets per build, preserving cycles,
input-to-test sets and reachable unknowns without a persistent graph cache.
