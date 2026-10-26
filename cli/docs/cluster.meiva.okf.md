---
type: doc
title: meiva cluster Subcommands
description: '`meiva cluster ...` — verbs that act on the cluster rather than on this host''s services: roster, destroy, health-probe, and the OS-job registration verbs. The home is `MEIVA_HOME` (else `~/.meiva`), never a path argument.'
tags:
- cli
- cluster-lifecycle
---

# `meiva cluster` Subcommands

## What it is

The verb group that acts on **the cluster** rather than on this host's local
services. The host runs one cluster, in the home `MEIVA_HOME` names (else `~/.meiva`),
so no verb takes a path or a name. Handlers live in
`cli/commands/cluster/control.py`, with `destroy` in
`cli/commands/cluster/home.py`. The host keeps no list of clusters: each home
describes only itself (`base/cluster/record.py`).

## Verbs

| Command | Function |
|---------|----------|
| `status` | this cluster's multi-machine roster |
| `db-authority issue-unit` | gateway only: seal one remote unit's capability bundle (its join, emergencies) ([[base/cluster/authority/docs/wiring.meiva.okf.md]]) |
| `db-authority install-unit BUNDLE` | an initialized agent-runner unit only: install a bundle sealed for it, the same join `meiva init --db-capability` makes (a fresh expiry or a rotated telemetry token; stop the unit first); the key is taken out of `MEIVA_DB_CAPABILITY_KEY` by the parser handler and the bundle file is deleted once installed. Refused on a gateway home and on an uninitialized home (`cli/commands/cluster/unit_join.py`) |
| `destroy` | decommission this host's cluster: stop it (`--stop-mode smooth|force` and `--stop-timeout SECONDS` are required and feed the stop), retire its OS jobs and the permissions helper, mark the home detached; `--drop-db` deletes pg/redis data too (e.g. `meiva cluster destroy --stop-mode smooth --stop-timeout 300`). Asks for the home path typed at a terminal: stdin and stdout must both be terminals, and no flag skips the prompt |
| `health-probe` | Observation-only OS job (exit 0/1; wrong-checkout refusal is 2). Every check runs on every round and each failed check emits a `health_probe_failing` event (`check`, `failure_class` of `code` / `environment` / `maintenance` / `alert-only`, `message`); a healthy run emits nothing, and the events repeat on every run while their conditions hold. Grafana alert rules read them (`for:` grades WARNING then ERROR per check; a deploy window is a Grafana silence). The probe keeps no state and notifies no one. The probe neither rolls back releases nor publishes known-good state. Provider balance and halted-agent checks remain part of health observation. |
| `health-probe-register` / `health-probe-unregister` | Register/remove the observation-only OS job at its default interval; no rollback threshold |

## Shape: a plan, executed by a shell

Each verb reads the facts it needs once and executes a plan decided from values; nothing
below `cli/commands/cluster/` reads `settings` or the process environment.

- `plans/` holds the decisions as strict modules: `roster_table` (the `status` table and its
  cells), `gateway_replies` (what each machine verb prints for each gateway answer),
  `health_verdicts` (what every health check says, its failure class and its order),
  `service_probe_plan` and `provider_verdicts` (which services the probe may certify, the
  provider guard's verdicts), `destroy_plan` (the prompt, the stop argv and the ordered steps
  of a destroy), `unit_join_checks` (what a join refuses), `unit_join_steps` (which credential
  a join presents, what the install is handed and what is said once it landed) and
  `db_authority_replies` (which homes `issue-unit` and `install-unit` refuse and the wording of
  their reports).
- The gateway verbs (`control.py`) take a `GatewayAccess` that the parser handler builds with
  `gateway_access_from_boot(args.runtime.config)`; `status` also takes the gateway's probe
  budget from the same configuration.
- `db-authority issue-unit` takes the invocation's `CommandRuntime`: the gateway home is
  `command.home`, the cluster secret and the plane's locality come from `command.config`, and
  the endpoint it seals is the one `ConfigAuthority.served_db_endpoint` serves for that home.
  `install-unit` and the join take the transport key as a value that the composition root took
  out of the environment, so neither reads `os.environ`.
- `unit_join.join_gateway` is the one join: `unit_join_steps.plan_credential` chooses the
  carried bundle or the installed capability, the shell opens it, fetches `/api/bootstrap` with
  its machine API token, `unit_join_checks` refuses an unroutable or incomplete answer, and
  only then does `install_bundle` run (the cluster's login probe is the install's own) and the
  bundle file is deleted. The served keys are dropped from the identity values, never stored.
- `health.py` runs one round: `health_readings.read_health` observes the cluster (the gateway
  with its retry budget, the agent table, the schema, the roster's services, the disk, WAL
  archiving, the prod checkout, the provider account) into values, `health_verdicts.judge_health`
  returns the checks in report order, and the shell prints them and emits one
  `health_probe_failing` per failed check plus the heartbeat. A round is configured by a
  `HealthOptions` that `health_options(command)` reads from the command's configuration and
  runtime; the WAL-G configuration file is read from the options' `data_plane` only.
- `home.py` publishes the terminal intent, runs `meiva stop` from the checkout the request
  names, retires the OS jobs and (on macOS) the helper, marks the home detached and then
  drops the data directories, in the order `destroy_plan.plan_destroy` returns. The terminal
  is the three streams of the command's runtime (`CommandRuntime.terminal`), so a confirmation is
  answered by whatever sits behind them. Its stop mode, host system and marker state are enums.

## Notes

- Stopping without decommissioning is `meiva stop`; `destroy` additionally marks the
  home detached so it never starts again (deleting `destroy-intent.json` by hand is
  the only way back). There is no slot to free: a stopped home's ports are simply
  unbound. Only `destroy` can be told to drop the data, and its prompt then lists
  the directories it will delete. It acts on the default home too: the typed path
  is the guard.
- A home that is not the default home neither registers nor removes OS jobs
  (`base.host.system.cron.mechanics.owns_os_job_home`): their labels and crontab markers name the
  job, not a home.
- A destroyed home keeps its files, `.env` included — that `.env` is the only
  copy of the cluster's secret, of any explicitly configured provider credentials, and
  of the URLs the data-plane identity is read from, so detaching never
  discards credentials. (It would not strand the preserved pg data either way:
  `ensure_cluster_role` re-sets the role password to the current secret on every
  bring-up, so a rotation self-heals — the cost of losing `.env` is credentials
  and config, not data.) The leftover home stays *un-bootable* instead: its
  `destroy-intent.json` is `detached`, which `meiva init` and `meiva start` both refuse
  (`cli/start_identity.py`).
- `health-probe` is a cron payload that exits 0/1, not a human-readable view —
  the roster is `status`.
- `status`'s `code` column is a live per-host probe reading: the commit the
  answering process froze at, marked when it differs from the host's checkout
  HEAD. There is no cluster pin or known-good column.
- There is no `cluster` verb for a stopped host: read it with `meiva status` (it reports
  `paused` with reason `startup`) and bring it back with `meiva start`
  ([graceful maintenance](../../docs/conventions/operations/graceful-maintenance.md)).

## Key dependencies

- [[cli.meiva.okf.md]] — the CLI overview: why identity is path-only, and the top-level verbs
- [[cli/commands/docs/commands.meiva.okf.md]] — the module split these handlers live in
