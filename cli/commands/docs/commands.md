# CLI Command Modules

## What it is

`cli/commands/` holds the `meiva` command domains. `cli.parsers.build_parser`
composes settings-free domain builders. Agents/questions are owned by
`agents/parsers.py` beside `control.py`, `questions.py` and `timeline.py`.
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

- `agents/` — lifecycle control, questions, timelines, external-agent
  impersonation, the pty allocation freeze and the computer-use daemons. The gateway
  verbs (`control.py`, `questions.py`, `questions_audit.py`, `timeline.py`) take the
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
  maintenance stop (`maintenance_stop.py`); owns its converge step (`pgbouncer.py`)
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
  (firewall, Redis bridge, OS jobs, the retirement of what WAL-G archiving left on a
  deployed host `walg_retirement.py`).
- `lifecycle/` — `meiva start` / `stop` / `restart` / `maintenance` /
  `status`, the pending-migration step, and the single application root they
  drive: [Host lifecycle](../lifecycle/docs/lifecycle.md).

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
the graceful deadline is spent. The data-plane stop requests [owned PostgreSQL](../../../base/cluster/docs/postgres.md) fast shutdown (SIGINT), so neither waits on idle client connections a drained state
cannot protect (issue #2307); one still running at its budget's end is ended by an
immediate shutdown and reported. When the data-plane phase fails after the
services phase stopped, `stop` reports the failure and exits non-zero;
it does not restart anything, and the operator decides whether to retry the stop
or run `meiva start`. `stop/extras` uses the same exact-home helper retirement as destroy:
native job, executable, socket and stopped-root custody are checked before
native helper exit and removal of its definition. Start recreates that definition.
Root owns Gate and native LGTM application services. `do_stop` and `cmd_start_body`
hold the home lifecycle lock for a whole start or stop.
See [the coordinated operator procedure](../../../docs/conventions/operations/graceful-maintenance.md).

Gateway data-plane startup (`data_plane/cluster_instance`, `data_plane/bringup`,
`data_plane/pgbouncer`):
[Gateway data-plane startup](../data_plane/docs/data-plane-startup/data-plane-startup.md).

## Notes

The cross-cutting notes live in [Command package notes](commands-notes.md).

## Key Dependencies

- [CLI](../../docs/cli.md) — the CLI domain overview: verbs, cluster identity, idempotent first start
- [Package Commands (plugins / skill / mcp / packages)](../extensions/packages/docs/packages.md) — the `meiva plugins` / `meiva skill` / `meiva mcp` package surface
- [Host lifecycle](../lifecycle/docs/lifecycle.md) — start, stop, restart,
  maintenance and the application root
