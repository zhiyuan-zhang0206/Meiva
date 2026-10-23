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

Creation tests combine real database state with `gateway_local_runner`: the
gateway's own machine is registered in `machines` as a runner whose `/ops` route
(`services.agent_runner.agent_ops.daemon.ops_route`) is served in this process on a
listener at port 0, so a spawn, lifecycle or config request the gateway forwards to
itself runs the real op code over the real HTTP round trip. A test that needs another
machine, or scripted answers and faults for this one, serves them with
`gateway/tests/support/runner_ops` (`runner_ops`, `completed` / `failed` / `refused`) and
asserts on the HTTP response and on the envelopes the runner received. The network
daemon's process, signals and sockets are still not exercised.
