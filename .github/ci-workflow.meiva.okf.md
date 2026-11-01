---
type: doc
title: The ci.yml workflow
description: "Job composition of the repository's single CI workflow: the classify gate, sharded suites behind stable required-check names, the fail-closed backend test gate, and artifact rules."
tags:
  - ci
  - ops
---

# The ci.yml workflow

Which events start it is `ci.yml`'s own `on:` block: this node does not restate the
trigger list. The copy is what drifts, and here the drift had teeth — this section once
claimed a `pull_request` trigger the file no longer declared, and that stale sentence was
cited when the temporary PR-body receipt gate was retired. A pull request runs this
workflow again as of the user's 2026-11-01 ruling (`#8091`), so its required checks are
what judge a change before it lands on `main`. A dispatch carries no pull-request
context, so `classify` defaults to a full run and the structure guard compares against the
merge-base with `origin/main`; jobs that exist only for a pull request stay skipped by
their own `event_name` guards. The post-merge Release Train (`workflows/release-gate.yml`) certifies
full-suite builds every 6 hours. Shard timings, the test-count baseline and warm caches
have other owners ([[repository-automation.meiva.okf.md]]).

Sharded fan-out behind stable check names, every job capped by
`timeout-minutes` so a hang cannot run to GitHub's global limit. A `classify`
job maps the PR diff through `base/deploy/git/repo_change.py` (the shared CI path
classifier) and gates the suites: docs-only skips both suites, frontend-only
skips backend, backend-only skips the frontend unit suite; e2e runs whenever
either side changed. Draft PRs skip CI until ready_for_review — except
Trunk test branches (`trunk-merge/*`, `trunk-temp/*`), which must verify.
The classifier recognizes component `docs/` layers containing `.meiva.okf.md`
files, excluding test data. Trunk always sets the backend side so its full
suite remains reachable; non-PR runs set both sides. Independent documentation,
language, content-manifest and security gates still run for documentation PRs.

[[test-selection.meiva.okf.md]] covers enforced backend selection; see
[docs/conventions/engineering/test-selection.md](../docs/conventions/engineering/test-selection.md).

The required Gitleaks job scans only a change's added commits (PRs and
dispatched topic branches against `origin/main`, or a bounded 50-commit window
on `main`); `secret-scan-history.yml` re-scans all history nightly.
`.gitleaks.toml` is also the local pre-commit policy (one owner for fixture
exceptions). A separate dependency-audit job runs `uv audit --frozen` and `pnpm
audit` as informational checks while the remediation policy matures; its
failure does not block the workflow yet.

The informational merged-tree structure preview checks main's contribution to
the PR head; see [merged-tree lint](../docs/conventions/engineering/merged-tree-lint.md).

| CI check name | Composition |
|---|---|
| backend (pytest + pyright) | aggregator over `backend-static` (ruff, migration + clock-lattice lints, native Grafana/migration smoke and pyright), parallel `backend-static-tests` (4-shard full static pytest contracts with JUnit, counts and coverage — narrowed to 1 shard over selected static files on `SELECTED` pull requests — inside the runner container without native toolchain setup) + 8 checked whole-file shards (`-n 4` each, pytest-split LPT with fresh runner-local full plans and the cached `.test_durations`); combines per-shard coverage and applies the 85% line gate |
| backend structure (pre-commit lint + codegen freshness) | structure and source-note checks cover every eligible change class; Node/pnpm supplies the parser; four freshness hooks run on matching paths, selector errors and manual dispatch |
| frontend (eslint + tsc + vitest) | one job: `pnpm install --frozen-lockfile`, route typegen, `tsc --noEmit`, eslint with a warning baseline, `vitest run --coverage`, serial flaky lane |
| e2e (Playwright happy path) | aggregator over 4 shards, Playwright chromium `-n 2` each (browser dir cached) |

The backend test gate is fail-closed: [[test-gate.meiva.okf.md]].

Full Pyright uses `--threads 2`; `time` records wall/CPU cost. Threads reject
`--stats`; use `uv run pyright --stats` for manual phase profiling.
`pyproject.toml` owns scope and diagnostics.

Parent: [[.github/.github.meiva.okf.md]].
