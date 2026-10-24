---
type: doc
title: Gateway data-plane startup
description: How `meiva start` brings up a home's own PostgreSQL, PgBouncer and Redis with URL-derived identities, always-authenticated pg_hba, the write generation and native custody checks.
tags:
- cli
- postgres
---

# Gateway data-plane startup

`meiva start` on a gateway-capable unit brings up the home's own PostgreSQL,
PgBouncer and Redis before any application service.

## Configuration

The commands read no `settings`, no environment and no home of their own.
`cli/commands/data_plane/config.py:DataPlaneConfig` is a frozen value of the `data_plane`,
`services` and `walg` fields they use (the URLs and both passwords hide from the repr;
`is_remote` is derived from the two URLs, so a copy with a swapped URL answers for it, and
`walg_config_file` is the archive configuration path or `None`).
The composition roots build it once with `data_plane_config_from_boot(command.config)`: the
`meiva start`, `meiva stop` and `meiva status` steps in `cli/commands/lifecycle`, and the pooler
converge step (`ensure_pgbouncer_step`). Every function below them takes it as the first
parameter and the home it works in as a keyword (`home=command.home`); `ensure_cluster_storage`,
`start_pg`, `start_redis` and the three gateway entries derive every path from that home. A test
builds the same value with `data_plane_config_from_boot` and changes a field with
`dataclasses.replace`.

What a service launch is delivered is `cli/commands/data_plane/instance/launch_delivery.py:LaunchDelivery`, read once at
the start entry (`bringup.read_launch_delivery`): the home, whether the plane is remote-managed,
the launch environment and the home's `.env` values. `root_driver.service_extra_env` takes it
and `bringup.db_delivery`/`api_delivery` decide the delivery kind from it.
Identity and port derivation consume the URLs the caller supplies.

## Plans and shells

The decisions of bring-up are values in `instance/`, declared `strict` in its `meizel.toml`:
`pg_plan.py` (the pg_hba and pg_ident bodies, the postmaster command line, the ordered steps of
a start), `redis_plan.py` (the conf, the command line, the credential refusals, the steps of a
start), `redis_binaries.py` (override directory, Homebrew formula or PATH), `bounded_wait.py`
(retry or give up), `storage_plan.py` (admission of a gateway start, the ordered steps of the
completion), `status_lines.py`, `stop_plan.py` and `bringup_lines.py` (what status, stop and
the remote probes print and do). `cluster_instance.py`, `bringup.py` (the entry, the probes,
status, stop, the launch deliveries) and `completion.py` (the final grants, the write generation,
the pooler and the consumer checks) are their shells: they read the OS user, the platform, the
machine's addresses and the binaries (`pg_tool`, `redis_*_bin`), execute each step and stop at
the first one that fails. The bounds a test must
shorten (the reachable-address wait, the postmaster start, one readiness probe) are the fields
of `StartTuning`, each defaulting to its production constant. The shell tests run the real
`postgres` and `redis-server` in the test's own home (`../tests/native_cluster.py`); the plans
are asserted as values in `../tests/instance/`.

## Identity

`cluster_instance` takes separate URL identities: the Postgres database/owner
comes from `db_identity(db_url)` (the URL's database), the Redis ACL user from
`redis_identity(redis_url)`. First-start identity persists the Redis credentials before
effects, and the identity owner writes each URL before storage comes up; no
runtime identity backfill exists. Startup refuses missing Redis credentials,
and a home without a database authority ledger that is not mid-birth, before
any native effect (no conversion exists); it never substitutes the bearer. Explicit remote-managed URLs retain provider authority.

## Authentication and the write generation

`pg_plan.pg_hba_body` always authenticates (the OS user by `peer` on the owner-only
socket, also as the password-less monitoring role `meiva_monitor` through the
`pg_ident` map `pg_plan.pg_ident_body` writes; every other role SCRAM) and
`require_authenticated_hba` proves the running postmaster demands passwords
after every rewrite (`pg_plan.hba_dial_verdict` judges each password-less dial). `bringup` wires the write generation
([[base/cluster/authority/docs/wiring.meiva.okf.md|delivery and wiring]]): birth runs
groups -> monitor -> retire legacy logins -> ledger -> mint generation 0 ->
pooler serving that pair -> pooled proof of both logins -> activate; an
ordinary start re-grants the groups after migrations, converges the monitor
and holds on any catalog/ledger mismatch
before the pooler. `db_delivery` gives
each launched service its class login.

## WAL archiving launch arguments

When `MEIVA_WALG_CONFIG_FILE` is set (`DataPlaneConfig.walg_config_file`), `start_pg` appends `archive_mode`,
`archive_timeout` and `archive_command` (`services.backup.walg.archive.archive_pg_args`)
to the postmaster's `-c` list; unset adds nothing. The settings live only in the
launch arguments, and a retained postmaster is reloaded, not relaunched, so they
take effect at the next new launch. After Postgres is ready, `warn_archive_inactive`
prints a warning when the running Postgres reports other archive settings
([[services/backup/walg/docs/walg.meiva.okf.md|WAL-G]]).

## Native custody

`base.cluster.ownership` is the common startup/maintenance observer: home
paths, native process birth and all listener PIDs must agree before config,
reload or ACL effects. Redis ACL and maintenance shutdown each retain one
observed connection; a reconnect loses authority and fails. PostgreSQL admin
and pooler dials use only the home's canonical Unix socket directory. Owned
provisioning, checkpoint, grant and migration dials verify their native
backend against the home's postmaster before DDL, which acts as the schema
owner (`base.cluster.dataplane.pg_admin`).

The layout and bind facts the cli and the root diagnostics must agree on live in
`base.cluster`, not in the cli: the Redis port (`ownership.redis_port_of`), the data plane's
bind posture (`port_preflight.bind_addrs`: loopback alone on a single box, loopback plus the
machine's declared reachable address otherwise) and the pooler's binary, files and listener
probes (`dataplane.pooler`). The data directories are derived from the home the caller hands in
(`cluster_instance.pg_data_dir(home)`, `redis_data_dir(home)`, `pg_socket_dir(home)`). The cli
owns bring-up and stop; the diagnostics only look.

## Pooler and maintenance stop

[[data-plane-pooler-and-stop|Data-plane pooler and maintenance stop]] owns the pgbouncer bring-up
(plan and shell) and the maintenance stop of the whole local plane.
