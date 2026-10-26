---
type: doc
title: Gateway Entry Points
description: Where the gateway process starts — FastAPI app definition, the __main__ entry, and the canonical launch script.
tags: []
---

# Gateway Entry Points

## Entry points

- `gateway/app.py` — FastAPI application factories (`create_app`, `create_app_from_environment`), cold `openapi_schema()`, lifespan, middleware, route mounting
- `gateway/http/config.py` — frozen `GatewayConfig` (`gateway_config_from_boot`, the one place the root reads settings for handlers; the root also hands it the account home, the memory directory under it and the `rng` every request id, attempt id, upload batch id and session key is drawn from) shared across app assembly, auth, CORS, memory reads, and lifespan; its groups (`display`, `models`, `sse`, `alerts`, `pages`, `memory_search`, ...) carry the tunables a route reads, and `gateway_config(request)` hands it to a handler
- `gateway/__main__.py` — `.venv/bin/python -m gateway` → uvicorn
- `gateway/cluster/server.py` — the process root `main(config=..., build_app=...)`: it reads three facts from the booted config and executes the plan `gateway/cluster/serve_plan.py` returns
- `services/wake/schedule_manager/engine.py` — the schedule execution engine; the ScheduleManager launches `services/wake/schedule_manager/runner.py` to compose the SDK in an independent process
- `scripts/entrypoints/gateway.py` — canonical launch script (per `gateway/app.py` docstring)

`gateway.app.main` boots the `ConfigBoot` and hands it to `server.main`. `serve_plan` is a
strict module over three values (does this machine serve the gateway role, does it declare a
reachable address, is hot reload on) and returns what `main` then does in order: require the
cluster secret, bind the loopback or every interface, require the transport-encryption
declaration off the loopback, raise the cluster's minimum code version, and either build the
app or leave it to a reload worker's factory. The effects (schema assertion, pidfile, fd limit,
logging, `faulthandler`, uvicorn) are the shell's; they are process globals, so the tests run the
real `main` in a child process against the throwaway Postgres
(`gateway/tests/bootstrap/test_server.py`) and assert the pidfile, the served port, the
`deployment_state.min_code_version` row and the exit on SIGTERM.

`cluster/process_boot.py` owns one immutable first-module-load image shared by
the Python entry and the ASGI factory entry (`gateway.app:create_app_from_environment`).
Each root creates its own `GatewayProcess`: its `CodeVersion` and `ProcessDbGate`
are shared by logging and all database handles, never stored as module state.
The Python entry lends its root to `create_app(..., process_ownership="borrowed")`;
the ASGI factory entry passes `process_ownership="owned"` so the lifespan closes its root.
Health, local cluster status and MCP status report that same image, including
unknown SHA, without reading a later checkout HEAD. Uvicorn reload children
capture their own generation in their new process. Startup failures still
close constructed clients; cleanup errors preserve the original primary.
The lifespan registers each start-up resource for release as it acquires it, with
`gateway.app.startup_releases`, and unwinds them in reverse order: telemetry flushers, the
MCP manager, upload recovery, the runtime monitor, HTTP clients and the inspector cache finish
before the pools close, and one failing release never skips the rest. A start-up error is the
error that propagates; a release that also fails is a note on it. The lifespan calls the real
client builders, the cron registration and the flushers directly; the flushers and the
notification outbox run as named tasks (`gateway-latency-flusher`, `gateway-auth-flusher`,
`gateway-notification-outbox`) that the lifespan tests find in `asyncio.all_tasks()`. The
release policy is asserted with plain steps in `gateway/tests/bootstrap/lifespan/test_startup_releases.py`;
the ordering against the real pools and tasks in `test_gateway_lifespan.py` and
`test_inspect_lifespan.py`.

Routes call the real configuration RPCs, command catalog, receipt writers and wake publishers
directly; transactional helpers require the collaborators as arguments and retain transaction
ownership. A test controls them with real resources (a runner served on a listener, the
throwaway Postgres and Redis), never through a handle on the application.

Bootstrap credential validation reads the app's captured `GatewayConfig`, just like
request authentication. Page URL construction reads its serving process config;
neither path falls back to the ambient process settings.

The snapshot's required `memory_root` supplies search, graph, note, pool and refresh
routes. The composition boundary resolves the existing `base.paths.gateway_memory_dir`
from the current process home and the startup config's machine roles: a combined
gateway/runner uses `$MEIVA_HOME/gateway/memory`, while a gateway-only unit uses
`$MEIVA_HOME/memory`. `ConfigBoot` owns the role values, not the process home.
These routes keep the captured path instead of resolving the ambient home
again per request. Explicit app builders can supply different roots without
patching the path resolver; each app also owns its graph cache. Cold OpenAPI
assembly supplies a non-existent path without resolving the runtime home.

Refresh passes that checkout to `memory_repo.pull_main`, with keep-local and
track-branch read from the existing process ConfigBoot for each request. The
indexer and memory checkout initialization retain their existing composition.
See [[gateway/routers/docs/memory.meiva.okf.md|memory route contracts]].

## One data root per process

`GatewayProcess` takes `home`: the data root its entry read once from `$MEIVA_HOME`.
Every owner the process builds reads that value — the event writer and the bus
(`event_bus_config_from_config`), the config authority's `<home>/.env`, the insights
socket, the skills load dir, a deleted schedule's work dir, the health identity, the
generation ledger and session key behind `cluster_credential` / `session_mints`. Request paths read
`app.state.gateway_process.home`; none resolves the process default again, so a second
app in one process reads none of the first root's files. `GatewayProcess.home` keeps the
entry's unresolved string, so `/api/health` still reports the exact `$MEIVA_HOME` that
`probe_unit_home` compares against; owners that take a filesystem path resolve it
themselves.

Parent node: [[gateway.meiva.okf.md|Gateway]].

The route-contract test recursively follows the current FastAPI include
wrappers' `effective_candidates` to leaf routes and reads methods from
`original_route`. Inspecting only the wrapper can omit routes from the contract
inventory; this describes the integrated route representation, not a promise
about every future framework version.
