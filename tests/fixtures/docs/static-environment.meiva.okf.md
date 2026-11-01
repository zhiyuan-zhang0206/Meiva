---
type: doc
title: "Static Test Environment"
description: "Process-scoped execution for meizel-derived static unit contracts: automatic fixture/import lane derivation, refused data-plane effects, retained isolation guards and required CI artifacts."
tags:
- evaluation
- quality-assurance
---

# Static Test Environment

`pytest --test-environment=static` runs every non-e2e backend test file that
`meizel` derives into the `static` lane (`python -m meizel tests --lane static`,
backed by `meizel.checkout.lanes_at` and cached in
`<git-common-dir>/meizel-scan-cache-v1.sqlite3`). There is no manual
`STATIC_TEST_PATHS` or `NATIVE_TEST_PATHS` list to maintain.

`meizel.test_graph.lanes` classifies each collectable test file deterministically
from its AST, fixture-resolution scope (`conftest.py` ancestors, `pytest_plugins`,
`path_scopes.toml` declarations, and re-exported fixtures), and transitive module
dependency closure:

- Any test that requests (directly, via `autouse`, via `@pytest.mark.usefixtures`,
  or via `request.getfixturevalue`) a native data-plane fixture (`db_url`,
  `database`, `redis_url`, `event_bus`, `postgres_access`, `redis_access`,
  `gateway_data_plane`, etc.), carries a host/serial marker (`requires_host`,
  `flaky`, `native_permissions_helper`), performs an unresolvable dynamic fixture
  or module load, or transitively imports native Postgres/Redis/PgBouncer
  provisioning or host-binary tooling (`base/deploy/tests/containers.py`,
  `base/deploy/tests/pgbouncer.py`, `base/cluster/dataplane/pg_tools.py`,
  `base/cluster/dataplane/pg_foreground.py`, or the CI runtime smoke runners) is
  classified `native`.
- Every remaining collectable backend test file is classified `static` —
  including all `meizel` compiler contracts, pure unit suites across `agent`,
  `base`, `cli`, `gateway`, `meiva`, `meiva_builtins`, `ops`, `schedules`,
  `scripts`, `services`, and `tests`, and the static environment boundary and
  selection canaries (`test_static_environment.py`,
  `test_static_environment_selection.py`).

Mixed `tests/` directories partition cleanly at the file level: in default whole-lane
runs, `--test-environment=static` collects only derived static files while skipping
native siblings and native-only subtrees, and `--omit-static-tests` collects only
derived native files. Explicit CLI paths passed to `--test-environment=static` must
resolve inside the checkout without symlink traversal to either a derived static test
file or a directory containing static tests and zero native tests; mixed parents,
native siblings, and escaping symlinks fail closed before execution.

This process keeps unreachable data-plane sentinels, refuses Postgres/Redis
provisioners and sync/async driver entry points, and never creates a native
instance or its watchdog. Native processes (`test_native_environment.py`) also
refuse undeclared driver access; ordinary fixture dependencies admit only their
requested resources. Static processes never admit requests. Private-home,
identity, plugin, leak and host-effect guards remain active for the entire
process.

Required CI runs this lane inside `backend-static-tests` (4 shards on full runs,
or 1 shard filtered to selected static files on `SELECTED` pull requests) with four xdist
workers, using `--dist=loadfile` to keep whole test files on a single worker and
avoid duplicate module fixture calculations across workers, each retaining the
process-lifetime native refusal and isolation guards. JUnit validation, executed counts
and combined coverage include all workers. Unhandled test-thread exceptions are errors
in the actual CI command; a refused background driver call cannot leave only a warning
and green JUnit.
The job installs Python and Node dependencies (with frontend pnpm dependencies for
TypeScript compiler contracts) without Postgres/Redis binaries, Grafana, or browser
setup. It runs alongside `backend-static`, which retains ruff,
types, migration and native Grafana smoke checks, and the existing structure job.
The required backend aggregator checks both jobs. Native full, selected and flaky commands use
`--omit-static-tests` from the same `meizel` owner; combined counts and coverage include
the static artifacts. Local pytest remains native unless explicitly selected.
Nightly duration refresh uses the native environment with `--omit-static-tests`;
it does not measure this lane's static population or its execution cost.

Isolation plugins: [[tests/docs/test-fixtures/test-fixtures.meiva.okf.md]].
