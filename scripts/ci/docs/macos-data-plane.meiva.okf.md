---
type: doc
title: macOS data plane
description: The macOS data-plane job that rides the nightly CI verdict, why no pull request runs it, and how its red becomes a card that a Linux green cannot retire.
tags:
- scripts
- ci
---

# macOS data plane

`.github/workflows/ci.yml` job `data plane (macOS)` runs `cli/commands/data_plane/tests`
and `cli/commands/lifecycle/tests/start_storage` on `macos-15`. It rides the nightly
verdict: the job's `if:` admits every event but a pull request, so it runs on
`ci.yml`'s daily cron (02:00 +08) and on any `workflow_dispatch` of the same
workflow. The two directories are the ones that start a real Postgres, Redis and
PgBouncer and read the launch receipts back, which is where a platform reaches a
comparison: 435 tests, about two and a half minutes on an M-series Mac, most of it
server startup.

## Why the job exists

`ci.yml` did not collect these tests on macOS. Its two macOS jobs are `host-marks`
(which selects `-m requires_host`, by marker rather than by directory) and
`helper-signing-smoke` (build, sign, launchd smoke). So a test that writes its
receipt's platform down instead of reading it stays green on every lane while
failing on the Mac that runs it — twice on 2026-11-08 (#8659, #8660), and both
times nobody was watching the Mac.

The static half of that defence is `scripts/lint/host_platform_marks.py` rule 3
([[scripts/lint/docs/host-platform-marks/written-platform.meiva.okf.md|the platform
written down]]), which refuses the written-down value at the commit. This job is
the dynamic half: a platform that arrives through a variable, which no lint can
see.

## Why it is a job here and not a workflow of its own

A macOS runner costs ten times an Ubuntu one and this job takes ten minutes of it,
so no pull request runs it: the defect class it catches is refused at the commit by
the lint, and a per-PR run would pay that price on every change to buy the cases the
lint cannot see. The trade is stated rather than hidden — a PR that introduces a
dynamic platform substitution is caught on the next night, not on the PR.

It is a job inside `ci.yml` rather than a `macos-data-plane.yml` of its own because
`.github/workflows` is at its twenty-direct-entry cap
(`docs/conventions/python-conventions.md`, enforced by `lint-code-structure`), GitHub
requires workflow files to sit directly in that directory, and the cap allows no
per-directory exemption. A twenty-first file is not available; the nightly verdict is
the trigger this job wanted anyway.

## How a red becomes a card

`main-red-watch` reads `ci.yml` already
(`schedules/jobs/watch/main-red-watch-schedule.py`), so this job's red is a card for
the P0 lead like any other failure in the run. One thing differs, and it is the
reason this job needed a mechanism:

- **A Linux green never retires its card.** `ci.yml` is a full-suite workflow, so its
  green closes the reds it is evidence about — but a Linux run never collected these
  tests. The failure carries that fact (`Failure.covers_full_suite`, set from
  `_PARTIAL_JOBS` in the schedule entry, keyed on this job's name), and the entry it
  opens keeps it, so only a later run that collects the tests retires the card.
  Renaming the job moves that entry with it.

A red still needs two sightings, and the pair is two nightly verdicts — a day apart.
`gh workflow run ci.yml --ref main` is how a reader asks for the second run early;
its full suite publishes nothing.

## Provisioning

`bash scripts/provision/database.sh` — the macOS branch of the one provisioning
script, so the versions this job tests (`postgresql@17`, `redis@8.2`, `pgbouncer`,
`pgvector` from brew) are the ones a developer's Mac and a Mac host carry, not a
second list kept in YAML. The script proves each binary afterwards.

Parent: [[scripts/ci/docs/ci.meiva.okf.md|CI]].
