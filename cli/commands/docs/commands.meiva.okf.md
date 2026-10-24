---
type: doc
title: CLI Command Modules
description: One subpackage per `meiva` command domain, plus the few `_`-prefixed helpers those domains share. Public modules define `cmd_*` handlers, wired by the argparse tree in `cli/main.py`.
tags:
- cli
- tool
---

# CLI Command Modules

## What it is

`cli/commands/` holds the `meiva` command domains. `cli.parsers.build_parser`
composes settings-free domain builders. Agents/notices are owned by
`agents/parsers.py` beside `control.py`, `notices.py` and `timeline.py`.
`agents/impersonation_parsers.py` owns external-session and relay arguments beside
`impersonation.py` and `impersonation_relay.py`; other builders remain in
`cli/parsers/`. Each builder binds its own `_h_*` adapter,
which lazy-imports the runtime `cmd_*` implementation at dispatch. There is
no registry or plugin mechanism; the wiring is the parser.

`cli/commands/__init__.py` is an empty package door: no import work and no
re-exports, so `import cli.commands` loads nothing else, Settings included.
Each command module is its own door; parser adapters lazy-import
`cmd_*` from the module that defines it, and test seams patch there. Eight
subpackages hold the domains, each an independent package door:

- `agents/` — lifecycle control, notices, timelines, external-agent
  impersonation, the pty allocation freeze and the computer-use daemons. The gateway
  verbs (`control.py`, `notices.py`, `notices_audit.py`, `timeline.py`) take the
  `GatewayAccess` their parser handler built from the command's configuration; the
  commands that read the shell's environment take it as a value the handler read once
  (`ImpersonationEnv`) and `pty` and `computer` take the invocation's home. The impersonation
  relay is a shell (`impersonation_relay.py`, `impersonation_adapters.py`) around strict
  decisions in `agents/relay/`.
- `management/` — gateway-managed config, schedules
- `extensions/` — plugins, skills, packages, MCP servers, memory; owns its
  converge steps (`materialize.py`, `skills_sync.py`, `external_skills.py`)
- `observability/` — native LGTM desired state, the OTel collector, trace
  shipping, logs; owns its converge steps (`lgtm_native.py`, `otel_collector.py`)
- `data_plane/` — per-cluster Postgres/Redis/PgBouncer bring-up, their verified
  maintenance stop (`maintenance_stop.py`), `meiva backup walg check|run|drill|restore|status` and the start-time
  WAL-archiving warning (`walg.py`); owns its converge step (`pgbouncer.py`)
- `cluster/` — whole-cluster verbs, the health probe, cron, `destroy` and a remote unit's
  join. Each verb is a shell around a plan: the shells (`control.py`, `health.py`,
  `home.py`, `cron.py`, `unit_join.py`) read the facts and execute, and the strict
  modules in `cluster/plans/` decide the wording, the classification and the order from
  values. A gateway verb takes the `GatewayAccess` its parser handler built from the
  command's own configuration; the health probe takes a `HealthOptions` and reads the
  cluster through `health_readings.py`.
- `converge/` — the orchestrator (`host.py`), the step contract (`spec.py`),
  the warning-only start preflights (health, ports, ownership), the
  rendered-file guard, and host-wiring steps owned by no other domain
  (firewall, Redis bridge, OS jobs including the WAL-G daily tick job, the WAL-G
  install/validate step `walg.py`).
- `lifecycle/` — `meiva start` / `stop` / `restart` / `maintenance` /
  `status`, the pending-migration step, and the single application root they
  drive: [[cli/commands/lifecycle/docs/lifecycle.meiva.okf.md|Host lifecycle]].

`cli.commands.probe` owns service evidence, health-port occupancy checks and
readiness reports, with an explicit `__all__` contract. It probes and prints; the wording of
a service row and of the unready summaries is `probe_report.py` (strict: values in, text
out). Its declaration predicate stays local; tiering and source drift stay with their
`base.deploy` owners.

`stop/` plans a stop (`plan.plan_stop`) and executes the plan; restart calls the same stop
kernel with the data plane and browser kept; shell sessions are closed like in any
stop. The agent host settles running turns itself on SIGTERM, ahead of the other
services in root's order; `stop/shell_sessions` and
`data_plane/maintenance_stop` verify resource exits.
`data_plane/_pooler_stop.OwnedPooler` owns
ordinary pooler stop admission for maintenance and startup recovery: exact native
birth and listener proof precede a durable stop intent and the first SIGINT
(`WAIT_FOR_SERVERS`). Retries and already-closed listeners only wait; PgBouncer
would interpret another shutdown signal as immediate termination. A birth that
finishes its drain and exits while its listeners are being scanned is stopped,
not a foreign listener. An explicit
force request alone permits a kill, with a separate bounded settle wait when
the graceful deadline is spent. The data-plane stop requests [owned PostgreSQL](../../../base/cluster/docs/postgres.meiva.okf.md) fast shutdown (SIGINT), so neither waits on idle client connections a drained state
cannot protect (issue #2307); one still running at its budget's end is ended by an
immediate shutdown and reported. When the data-plane phase fails after the
services phase stopped, `stop` reports the failure and exits non-zero;
it does not restart anything, and the operator decides whether to retry the stop
or run `meiva start`. `stop/extras` uses the same exact-home helper retirement as destroy:
native job, executable, socket and stopped-root custody are checked before
native helper exit and removal of its definition. Start recreates that definition.
Root owns Gate and native LGTM application services. `_exclusive_resources`
serializes whole local start and stop operations under the home lifecycle lock.
See [the coordinated operator procedure](../../../docs/conventions/operations/graceful-maintenance.md).

Gateway data-plane startup (`data_plane/cluster_instance`, `data_plane/bringup`,
`data_plane/pgbouncer`):
[[cli/commands/data_plane/docs/data-plane-startup.meiva.okf.md|Gateway data-plane startup]].

## Notes

The cross-cutting notes live in [[commands-notes|Command package notes]].

## Key Dependencies

- [[cli.meiva.okf.md]] — the CLI domain overview: verbs, cluster identity, idempotent first start
- [[cli/commands/extensions/packages/docs/packages.meiva.okf.md]] — the `meiva plugins` / `meiva skill` / `meiva mcp` package surface
- [[cli/commands/lifecycle/docs/lifecycle.meiva.okf.md]] — start, stop, restart,
  maintenance and the application root
