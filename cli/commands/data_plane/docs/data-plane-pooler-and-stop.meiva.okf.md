---
type: doc
title: Data-plane pooler and maintenance stop
description: 'How the pgbouncer pooler is brought up and owned, and how a maintenance stop ends a whole local data plane.'
tags:
- cli
- data-plane
- pooler
---

# Data-plane pooler and maintenance stop

## Pooler

PgBouncer starts only after schema and group grants exist, always with SCRAM
against the generation's verifier userlist; changed userlist or ini bytes
restart it (a reload never revokes a user). A live pooler with closed listeners
retains custody: normal start cannot repeat its shutdown signal or escalate to
force. A graceful stop timeout fails without killing the survivor.

`pgbouncer.py` is the shell. It observes (this home's pooler custody, the ini and
userlist bytes on disk, the socket table), asks `pooler/convergence.py` what to do and
executes the returned steps, stopping at the first that fails. The plan is a function of
three facts: a running pooler whose files are unchanged and whose public listener verifies
is reloaded and never waited on; changed files restart it; a pooler missing its reachable
listener is restarted after the reachable address is assigned (a reload does not retry a
bind that failed at startup); and one that is not running is started after the same wait.
`pooler/ini.py` renders the configuration, `pooler/text.py` the operator lines,
`pooler/stop_intent.py` the durable stop intent and `pooler/identity.py` attributes a running
pgbouncer to a home from its command line. The composition root (`completion.ensure_pooler`)
hands the shell one `PoolerSpec`: the ini's shape, the userlist and admin credential, the
resolved binary, the daemon environment (`daemon_process_env`) and the waits, so a test starts
a real pooler under its own home with small waits instead of replacing the code that looks
for it.

Where the pooler's files live is the one thing here that still follows the process rather than
the `home` argument: `ini_path()`, `pidfile_path()` and `pooler_dir()` in
`base/cluster/dataplane/pooler.py` resolve `meiva_home()`. `completion.ensure_pooler` renders the
userlist for the `home` it is given, but `pgbouncer.py` writes it (and the ini, the pidfile and
the log) under the process home, and `stop_pgbouncer` and the maintenance stop look for the
pooler there. The two agree whenever the process is the home's own, which is every production
call; they move together when base's `pooler.*` functions take the home.

A pooler stop signals a recorded birth once (`_pooler_stop.OwnedPooler`): a birth that is already
gone, or whose pid another process now holds, is reported stopped and never signalled, and an
intent file that is not exactly the recorded shape (not JSON, not an object, wrong keys or types)
is one `RuntimeError` that retains custody.

Native maintenance-stop tests wait for both listener readiness and the daemon's
PID publication: an open port does not prove the PID file is complete. Their
bounded fixture wait accepts missing or empty publication while startup proceeds;
nonempty corrupt content fails immediately. The real exit and idle-client stop
contracts exercise a pending PID read and retain verified native cleanup custody.

Redis and the pooler are spawned with `base.native_process.child_env.daemon_process_env`
(the pooler's is the one its spec carries),
and the Postgres postmaster with `base.cluster.dataplane.pg_tools.pg_start_env` (the same set
plus the macOS locale fallback): the operator's PATH, home, user, temp dir,
timezone and locale only. The gateway login, write generation and API token
the boot pass delivered to `meiva start`, and the human secret and Redis admin
password `.env` gives it, never reach a data-plane daemon's environment.
Nothing the postmaster runs needs more.

Startup ordering is asserted as the plan `storage_plan.plan_completion` returns
(`../tests/instance/test_storage_plan.py`); the steps against a real database, the remote probes
and the status lines are in `../tests/bringup/`. The lifecycle tests retain the `meiva start`
caller dispatch contracts.

Pooler drain tests combine low candidate ports, actual listener custody and
retries to reduce reuse of a just-closed port. A free-port preflight is only an
observation at that moment; it cannot grant later exclusive ownership.

## Maintenance stop

`maintenance_stop.py` ends a unit's pooler, Postgres and Redis once its services have stopped.
It identifies every captured service first, then executes what `maintenance/plan.py` returns:
pooler, then Postgres, then Redis, with Redis saving unless the caller proved a final snapshot.
A missing pidfile or a refused port is not evidence of no local process, so
`maintenance/process_scan.py` also judges the process table from facts the shell read. A
process of this home that nothing recorded refuses the stop; another home's process is none of
its business (a sibling home's path may have this one's as a prefix); and a same-user
candidate whose working directory (for a pooler, its configuration file) cannot be read is
reported as unreadable, which refuses the stop with its pid instead of raising out of the
scan or passing, because proving that nothing unrecorded runs is what the scan is for.
`maintenance/report_lines.py` renders the clients still on the pooler and a Postgres
escalation for the operator and the stop journal.

The forced stop of a cluster's own instance (`bringup.stop_cluster_instance`, the `--mode force`
path) ends Redis with a bounded `redis-cli shutdown nosave` as its admin user (30 seconds; a
timeout reads as return code -1) and counts it stopped only when its port no longer accepts
connections, since neither the exit status nor an authenticated PING proves that. The home's
`redis-server` processes (name and working directory) are identified before the shutdown; a
listener that outlives it while one is alive is reported as that Redis still answering, and one
with none alive as a process other than this home's Redis holding the port. The stop fails either
way.

The shell tests start the real processes (`../tests/native_processes.py`: a pgbouncer under a
test home, a Redis on its data directory, a Postgres through `start_pg`); the decisions are
asserted as values in `../tests/pooler/` and `../tests/maintenance/`.
