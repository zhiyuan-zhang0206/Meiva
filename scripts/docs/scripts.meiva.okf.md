---
type: doc
title: "Scripts"
description: "Engineering tools for setup, audits, linting, CI, code generation and deployment."
tags:
  - tool
  - ops
  - ci
---

# Scripts

## What is it

`scripts/` contains Python and shell engineering tools invoked by developers, git hooks, CI, and the CLI for installation, validation, and release. Purpose subdirectories and their own children each obey the 20-direct-entry structure budget; every nested directory counts as one direct entry in its parent.

## Script Categories

### Lint (`lint_*.py` + `check_*.py`)
Split by kind: code/AST/Python-convention guards in [[scripts/lint/docs/lint.meiva.okf.md]]; document/OKF/skill/migration-format guards in [[scripts/content_lint/docs/content_lint.meiva.okf.md]].

### `audit/` — read-only drift audits
GitHub protection, reference and move inventories, runtime impact, SDK usage and
live schema drift: [[scripts/audit/docs/audit.meiva.okf.md]].

### `codegen/` — derived-artifact generation and OKF tooling
Tool and artifact owners: [[scripts/codegen/docs/codegen.meiva.okf.md]].

### `ci/` — CI job / test infrastructure
CI tool owners, explicit evidence contexts and test feedback contracts:
[[scripts/ci/docs/ci.meiva.okf.md]].

### `verify/` — the verification boundaries (Linux container, Tart macOS VM)
Host side, stdlib only: `boundary.py` (what both recipes share: the start profile, the `meiva init` and `meiva start` argv, the refusal to let host state in, the commit resolution, the evidence directory and bounded logged steps), `container.py` (builds the image on input change, starts one container per commit, clones the commit into `~/.meiva/source`, runs `meiva init` and the first `meiva start`, copies the evidence out, removes the container) over `Dockerfile` (the image: the repository's own provisioning scripts, an ordinary user, no source or secret), and `tart_run.py` (clones the golden Tart image into a throwaway VM, fetches the commit from a read-only export, provisions the toolchain, runs `meiva init`, retires the golden image's loaded helper with guest-local `meiva stop` before the first `meiva start`, observes, copies the evidence out, deletes the VM) over `tart_vm.py` (the Tart driver: the two-VM ceiling, the one read-only share, deletion limited to run VMs) and `tart_golden.py` (builds the golden image once, with a person for the two desktop grants). `observe.py` runs inside either boundary: toolchain, service identity probes, frontend, CORS and a scripted agent executing `print(1 + 2)`, plus on macOS the signed helper chain (launchd, helper, root, every unit) and its desktop grants. Design and claims: [verification boundaries](../../future/infra/engineering/verification-boundaries.md).

Golden-build unit tests use a nonexistent Tart binary to check dry-run step
rendering and absence of an evidence directory, then synthetic guest records to
check grant verdicts. They do not build a VM or establish real desktop grants.
Dry-run still resolves the commit through a read-only Git query.

### Startup / Deployment / Multi-host
- `entrypoints/agent.py` (spawns a root agent through gateway `POST /api/keyed/v1/agents`; the gateway must already be up), `entrypoints/gateway.py` (directly starts the gateway FastAPI body, ≈ `.venv/bin/python -m gateway`) — **the latter does not spawn an agent**
- `data_plane_ops/rotate_cluster_secret.py` — the human-bearer `MEIVA_CLUSTER_SECRET` rotation on the gateway (`advance`): journaled as fingerprints, it verifies the pinned logical-backup passphrase (pinning `sha256(secret)` only on a home without a pin) before writing the new secret; default dry run (`--execute` performs it); it does not change the data plane
- `data_plane_ops/rotate_data_plane_secrets.py` — gateway-host rotation of the Redis credentials (`--scope admin` for the `requirepass` password, `runner` for the runtime ACL password, `both` by default); default dry run (`--execute` performs it), resumable from a 0600 state file; PostgreSQL has no rotatable password here

## Key Dependencies

- [[../../cli/docs/cli.meiva.okf.md]] — `meiva start` owns cluster identity and service readiness; `lifecycle/converge_host.py` applies host wiring and plugin scaffolds.
- [[../../tests/docs/tests.meiva.okf.md]] — many lint scripts have corresponding `tests/test_lint_*.py`

## Notes

- `hooks/prepush-base.sh` owns the contribution range for branch lint, pyright, input gating, artifact freshness and `provision/prepush_frontend.py`; missing base fails. Frontend selection includes known filesystem consumers and reports global/deletion closure as CI-only.
- Hooks run lints, codegen, types and frontend checks. Full pytest and migration smoke stay in CI; local tests are targeted.
