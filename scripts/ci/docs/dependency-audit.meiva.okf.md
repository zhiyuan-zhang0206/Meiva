---
type: doc
title: "Weekly dependency audit"
description: "The weekly report over locked dependencies, pnpm audit and pinned binaries: its three sections, the single tracked issue, and the dev-only exemption that keeps unfixable tooling findings out of the actionable set."
tags:
  - ci
  - security
  - dependencies
---

# Weekly dependency audit

`scripts/ci/dependency_audit.py` runs weekly (and on demand) from
`.github/workflows/dependency-audit.yml`. One markdown report carries three
sections into one tracked GitHub issue, found by its marker and updated in
place, then closed when a run comes back clean:

- Python: `uv audit` over `uv.lock`; the JSON carries no severity, so each
  advisory id is graded through OSV (GitHub's reviewed severity) and an
  advisory OSV cannot grade is listed as `unknown`.
- Frontend: `pnpm audit` over `ui/web/pnpm-lock.yaml`.
- Pinned binaries: the version constants Meiva downloads or installs outside the
  lockfiles (zonky Postgres, pgvector, wal-g, otelcol-contrib, uv, pnpm, Grafana)
  against their upstream's newest release. pgvector publishes tags, not GitHub
  releases, so its row reads tags; the zonky row compares only within the
  pinned Postgres major.

The report never changes a pin: moving one is a dependency upgrade that needs
approval. `uvx` runs the audit with the exact `UV_VERSION` pinned in
`base/host/brew_pin.py`, so the report and the installed toolchain agree.

## The dev-only exemption

A finding whose advisory findings are all dev-only (`"dev": true` and every
`.><direct_dep>` path rooted strictly in `importers['.'].devDependencies` of
`ui/web/pnpm-lock.yaml`) and whose patched range offers no forward fix
(`patched_versions` is `<0.0.0` or not newer than the installed version) has
no upgrade that fixes it. It stays listed in the report (a run must remain
auditable) but does not make the issue actionable. Without this exemption an
unfixable dev-tooling advisory would keep the issue open forever. A dev-only
finding with a real upgrade available, and every production finding, stay
actionable.

## Contracts

A finding is dev-only when every advisory finding carries `dev: true` and reaches only root `devDependencies`, and has no forward fix when `pnpm audit` reports `<0.0.0` or a bound not newer than the installed package; only both together exempt it.

The direct `python3 scripts/ci/dependency_audit.py` entry remains standard-library
only. It supplies command and HTTP transports to the real audit, binary discovery,
report and issue owners; nested transports are required. Type-only port imports
do not load application dependencies. Audit commands keep their 600-second bound,
GitHub commands 60 seconds and HTTP requests 30 seconds. A nonzero GitHub command
raises with its captured output; audit commands retain their distinct complete
findings policy. HTTP adaptation passes the timeout by keyword to `urlopen`.

Each pinned-binary row reads its version constant from its owning file by an exact regex; a renamed, moved or ambiguous constant raises instead of silently dropping the row.

Parent: [[scripts/docs/scripts.meiva.okf.md]].
