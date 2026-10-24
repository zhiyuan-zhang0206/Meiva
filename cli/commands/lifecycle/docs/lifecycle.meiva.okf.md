---
type: doc
title: Host lifecycle
description: '`meiva start` / `stop` / `restart` / `status` and the single application root they drive: one package, because every verb goes through the same root driver and stop boundary.'
tags:
- cli
- lifecycle
---

# Host lifecycle

## What it is

`cli/commands/lifecycle/` holds the local host lifecycle verbs and the machinery
they share. The verbs are `start/` (`meiva start`), `stop/` (`meiva stop`,
`meiva restart`) and `status/` (`meiva status`). Every one of them reaches the single application
root through `root_driver.py`, and every stopping verb goes through the same
native stop boundary (`stop/`) — which is why they
form one package rather than one per verb.

`root_driver.py`, `stop/shell_sessions.py`, `root_control/generation.py`, `maintenance.py` and
`start/migrations.py` are public: `data_plane/maintenance_stop` and the cutover
scripts reach them. The `_`-prefixed modules are steps only this package calls.

## Stand-down

A stop leaves `run/meiva-root/stand-down.json` so the OS boot job cannot undo it;
only an explicit start ends it. [[stand-down|Stand-down]] owns the record's shape,
who writes it and how the boot job reads it (task #5493).

## Stop and restart

`stop/` plans a stop from the operator's choices and the host's facts, then a shell
executes the plan. [[stop-and-restart|Stop and restart]]
owns the plan, the shell, the restart legs and the failure report.

## Start

`cli/commands/lifecycle/start/migrations.py:cmd_migrations_apply` is deliberately
not a user-facing verb — it runs as a step of `meiva start`, so any restart
crossing a schema change catches the DB up on its own.

Every host runs that step on a cold start, so its two branches split by who can
mutate the schema. A plane this home owns migrates over the postmaster's own
owner socket — direct by construction, and it survives a transaction pooler
because the applier holds a session advisory lock for its whole loop. A plane this
home does not own is dialed ordinarily: an agent-runner only ever finds nothing
pending, because the mutation authority is checked exactly when something is. A
provider-managed plane's URL is that provider's only address for us — nothing here
can tell whether it is a pooler. The applier therefore accepts one residual risk: a
provider-side transaction pooler would drop the session advisory lock silently. No
such plane runs in this fleet today; if one is added, point the URL at the
provider's direct (session-mode) endpoint, or run migrations from a host that can
prove directness. The checkpoint schema check that follows both branches is a
read-only verification, so it dials the ordinary access URL as well.

`start/__init__.py` is a shell around plans: the stand-down gate, the refusals, the cold
preparation and its order (`start/cold_start_plan.py`), the schema checks, the gateway probe and
the readiness verdict are `strict` modules over values the shell read, and the shell executes
them in order. Their tests assert the returned plans; the tests of the shell run `cmd_start_body`
against a real home, a scripted root-control socket and the suite's Postgres
(`tests/startup/start_harness.py`). The cold preparation is `start/cold_start.py`: it runs the
plan's steps in order against the values the start hands it (`ColdStart`, converge step table
included) and its shell tests (`tests/startup/test_cold_start.py`) run a runner's preparation over
a real home and the suite's Postgres. `meiva restart` is tested the same way
(`tests/startup/test_restart_legs.py`): the scripted root's own record of the verbs it served is
the evidence of what each leg did.

`root_control/generation.record_running_sha` records the running source
after a successful start. What start treats as already-up, what it waits for,
and when an unready service becomes exit code 4:
[[start-readiness.meiva.okf.md]].

After a ready gateway, start prints the gateway hint and, through
`cli/commands/lifecycle/start/login_link.py:print_login_link`, a one-time UI login link
(`<ui>/#ticket=<ticket>`). The ticket is minted with the home's human secret and
only when output is a terminal and a UI service was launched; a failed mint is
reported on stderr and never fails the start. Gateway side:
[[gateway/http/auth/docs/web-sessions.meiva.okf.md]].

## Status

`status/` is `meiva status`: the local view of services, data plane, host reading and
release, plus the gateway's own `GET /api/cluster/status` snapshot. The local probes
are the bootstrap-exemption supplement (they work with the gateway down, mid-restart)
and the gateway can only see itself, so both always print, and a gateway that cannot be
reached or addressed prints inline instead of failing the verb.

The command is a shell around a plan. `cmd_status` takes the `CommandRuntime` (its
home, machine, config and host are values, never read from the environment), collects
the facts that decide the layout (`StatusFacts`: repo, capability set, roster with gate
reasons, the last start's launched set, the designated observability host) and executes
the steps `plan.plan_status` returns: static lines, or a named probe whose output the
owning renderer words. `roster` decides the note each service row carries and the table
layout (a gate reason wins over the launch record; before any start nothing counts as
launched), `render` the banner, host reading, source-drift and release lines, and
`gateway` the gateway view, which prints the gateway's snapshot or the reason there is none: unreachable
or not answering with JSON, no gateway URL, or no credential to present (neither a machine API token nor
the cluster secret), each as one inline line while the other sections still print. The slow or fallible sections (data plane, gateway) are
steps, not facts, so the operator sees the earlier sections while they run. The planning
and rendering modules are `strict`, so their tests assert the returned lines; the
tests of the shell run it against a scripted root-control socket, a real manifest and
git checkout, and the suite's throwaway Redis.

## Key Dependencies

- [[cli/commands/docs/commands.meiva.okf.md]] — the command-package overview
- [[start-readiness.meiva.okf.md]] — the launch guard, the root-owned readiness
  wait and the failure exit code
