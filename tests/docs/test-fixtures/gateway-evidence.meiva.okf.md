---
type: doc
title: Gateway fixture evidence
description: Authentication and application lifespan boundaries in gateway tests.
---

# Gateway fixture evidence

`gateway/tests/agents/gateway_auth.py` injects credentials only into an assembled
app whose state contains `GatewayConfig`. Explicit Authorization, including an
empty value, wins. Requests still pass through production authentication
middleware; authentication-specific tests opt out of automatic credentials.

Lifespan-owned concurrency gates are rebuilt for each test app so a semaphore
bound to an earlier event loop cannot leak into the next test. Upload pool state
must be assembled in the real lifespan order. Use the `memory_search_app` and
`gateway_test_client` owners: a bare client without startup does not establish
that application-state contract.

A test whose requests reach the gateway's own machine, or that reads its `machines` row,
requests `gateway_local_runner` (a parameter, or `pytest.mark.usefixtures` for a file); nothing
registers the machine implicitly. The fixture registers the machine in `machines` as a runner
whose ops daemon serves in this process: the daemon's real admission state (`active_ops`,
`requests`, `workers`, the `ops_concurrency` semaphore), its pool and executor, `ops_route` as
the `POST /ops` handler and `base.daemon.health.start_health_server` as the HTTP layer, bound to
port 0 on its own event loop for the life of the test. A spawn, lifecycle or config request the
gateway forwards to itself therefore runs the real op code over the real HTTP round trip, and
the fixture drains the daemon at teardown the way SIGTERM does, failing the test if work is left
running. Only the host effects behind the ops are `fake_host()`. What the daemon did is read from
its ledger: `executed_ops(db_conn, "spawn-launch")` returns the `api_idempotency` rows it wrote
for the ops that carry a key.

A test that needs another machine, or scripted answers and faults for the gateway's own, serves
them with `gateway/tests/support/runner_ops` (`runner_ops`, `completed` / `failed` / `refused`).
Such a runner stands in for a machine whose ops code the test cannot run. Prefer asserting the
response or the rows. `expecting(payload, reply)` states what the gateway must send as the
runner's own validation, so a wrong payload fails the request with the difference in its detail.
Read `runner.calls` only where the request itself is the behavior: a dial that must not happen
or must happen a given number of times, or a forwarded body whose fields are the subject. The
network daemon's process and signals are still not exercised.
