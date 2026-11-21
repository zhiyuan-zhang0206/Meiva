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
have other owners ([Repository Automation](repository-automation.md)).

Sharded fan-out behind stable check names, every job capped by
`timeout-minutes` so a hang cannot run to GitHub's global limit. A `classify`
job maps the PR diff through `base/deploy/git/repo_change.py` (the shared CI path
classifier) and gates the suites: docs-only skips both suites, frontend-only
skips backend, backend-only skips the frontend unit suite; e2e runs whenever
either side changed. Draft PRs skip CI until ready_for_review.
The classifier recognizes component `docs/` layers containing `.meiva.okf.md`
files, excluding test data. The job decides both sides from the diff alone and
only passes the head ref through; a non-PR run sets both sides. Independent documentation,
language, content-manifest and security gates still run for documentation PRs.

[CI test selection](test-selection.md) covers enforced backend selection; see
[docs/conventions/engineering/test-selection.md](../docs/conventions/engineering/test-selection.md).

The required Gitleaks job scans only a change's added commits (PRs and
dispatched topic branches against `origin/main`, or a bounded 50-commit window
on `main`); the `secret-scan-history` job of `scheduled-audits.yml` re-scans all history nightly.
`.gitleaks.toml` is also the local pre-commit policy (one owner for fixture
exceptions). A separate dependency-audit job runs `uv audit --frozen` and `pnpm
audit` as informational checks while the remediation policy matures; its
failure does not block the workflow yet.

There is no separate merged-tree preview job: the suite runs on the merge ref
itself (`refs/pull/N/merge`), so it already sees main's contribution. See
[merged-tree lint](../docs/conventions/engineering/merged-tree-lint.md) for what
that does and does not cover.

| CI check name | Composition |
|---|---|
| backend (pytest + pyright) | aggregator over `backend-static` (ruff, migration + clock-lattice lints, native Grafana/migration smoke and pyright), parallel `backend-static-tests` (8-shard full static pytest contracts with JUnit, counts and coverage — narrowed to 1 shard over selected static files on `SELECTED` pull requests — inside the runner container without native toolchain setup) + 8 checked whole-file shards (`-n 4` each, pytest-split LPT with fresh runner-local full plans and the cached `.test_durations`); combines per-shard coverage and applies the 85% line gate |
| backend structure (pre-commit lint + codegen freshness) | structure and source-note checks cover every eligible change class; Node/pnpm supplies the parser; four freshness hooks run on matching paths, selector errors and manual dispatch |
| frontend (eslint + tsc + vitest) | one job: `pnpm install --frozen-lockfile`, route typegen, `tsc --noEmit`, eslint with a warning baseline, `vitest run --coverage`, serial flaky lane |
| e2e (Playwright happy path) | aggregator over 4 shards, Playwright chromium `-n 2` each (browser dir cached) |

The backend test gate is fail-closed: [CI test gate and executed-test counts](test-gate.md).

Full Pyright uses `--threads 2`; `time` records wall/CPU cost. Threads reject
`--stats`; use `uv run pyright --stats` for manual phase profiling.
`pyproject.toml` owns scope and diagnostics.

Parent: [.github](README.md).

## Frontend install hangs

All frontend dependency installs in this workflow use
[`pnpm-install`](actions/pnpm-install/action.yml): the same frozen lockfile is
attempted twice, with a process-group deadline per attempt and a step-level
backstop. No cache deletion, dependency re-resolution, or package-manager upgrade
is part of recovery. The existing [failed-job rerun](workflows/ci-rerun.yml)
also covers a runner/container transport hang that prevents the shell retry from
running at all.

On 2026-11-19, [run 44536029204, attempt 1, e2e 4/4](https://github.com/zhiyuan-zhang0206/Meiva/actions/runs/44536029204/attempts/1)
printed pnpm 11.9.0's `Done in 12.6s` at 13:43:18Z, then remained in the install
step until cancellation at 14:22:21Z. (The initial incident description named
3/4; the job API and logs identify 4/4.) On 2026-11-21,
[run 44886023185, attempt 1, Backend static contracts 6/8](https://github.com/zhiyuan-zhang0206/Meiva/actions/runs/44886023185/attempts/1)
printed `Done in 12.2s` at 08:42:31Z and was cancelled at 09:06:32Z. The successful
e2e 4/4 retry took 8.1s; static 2/8 in the second incident's original attempt
took 8.8s and advanced immediately. Both failures used the same runner image,
plain `pnpm install --frozen-lockfile`, and pnpm version as healthy jobs.

The frontend has no root lifecycle/postinstall script. Its workspace uses the
hoisted linker, disables the global virtual store and automatic pre-run installs,
and denies the two listed native fallback builds. No lifecycle script or store
server appears in either failed log. There is no process snapshot from the
cancelled runners: `Done` alone does not prove pnpm exited, and the logs cannot
distinguish a live Node handle, a descendant retaining output pipes, or a Docker
exec/runner transport stall. Therefore this is bounded recovery, not a claimed
root-cause fix. Failed attempts now print a process snapshot for diagnosis; the
step backstop limits even a transport stall instead of consuming the job cap.
