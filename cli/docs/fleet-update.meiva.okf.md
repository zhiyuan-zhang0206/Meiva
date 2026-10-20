---
type: doc
title: Fleet update over SSH
description: Complete and split cli.fleet_update commands — stop, switch and start a networked source-mode cluster — and their ordering.
tags: [cli, updates, operations]
---

# Fleet update over SSH

`cli.fleet_update run` updates a networked source-mode cluster over key-based SSH
in one invocation: `down` stops every host and switches its checkout to NEW,
then `up` starts the cluster and proves it. The separate `down` and `up`
commands remain for intermediate manual work and recovery. The operator-facing procedure is
the runbook
([updating a networked cluster in source mode](../../docs/conventions/runbook.md#updating-a-networked-cluster-in-source-mode));
this node owns the code-side order, and the window's guards are
[[cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md]].
The halves are idempotent: after a failure, fix the cause and rerun the failed
half. `run` accepts both halves' options, requires `--new`, uses one log and
stops at the first failure; a failed `down` never enters `up`. Its failure
message names the half to rerun. A failed `up` does not require repeating `down`.
The module imports no application code — it must run from a bare
operator checkout, whatever generation the cluster runs.

## Every choice is an explicit argument

Each choice below is a required flag; a missing or conflicting one exits 2, all
listed together, before any host is touched or log written. The operator's stop
and start choices are forwarded unchanged, never checked against the diff.

- Both halves: `--runner ALIAS` (repeatable) or `--no-runners`; `--dry-run` or
  `--execute`.
- `down` / `run`, forwarded to every host's `meiva stop --yes`:
  `--keep-shell-sessions|--close-shell-sessions`, `--keep-browser|--stop-browser`,
  `--keep-extras|--stop-extras`, `--keep-service NAME` (repeatable) or
  `--keep-no-services`, `--mode smooth|force`, `--stop-timeout` (as `--timeout`).
  `--keep-infra|--stop-infra` goes to the gateway's stop only. `--yes` is always
  sent (non-interactive SSH).
- `down` / `run`, for the fleet itself: `--allow-python-change|--refuse-python-change`,
  `--allow-red-schedules|--refuse-red-schedules`, `--silence-hours`,
  `--lease-confirm-seconds` (0 reports the leases and never waits).
- Every half, forwarded to every host's `meiva start` (over SSH, or in the macOS
  one-shot LaunchAgent) by `up` and by a failed `down`'s recovery: `--all-services`,
  `--only-service NAME` or `--disable-service NAME` (repeatable), and `--start-timeout`.
- `up` / `run`: `--smoke-timeout`, `--roster-timeout`.

One start selection applies to every host.

All commands refuse an agent's inherited `MEIVA_AGENT_ID`. An authorized agent
can submit an OS-owned job and return before drain; the submitting shell is not
the updater. The [agent update guide](../../meiva_builtins/skills/platform/meiva-guide/ops/references/agent-update.md)
owns the macOS launchd submission recipe. The operator checkout and interpreter
remain unchanged throughout the update.

## The down half, in order

1. Preflight each listed host. OLD (the one commit every host shares that is not
   NEW) only feeds the `.python-version` refusal.
2. Read the gateway's active impersonation leases and name them: a stop ends
   every takeover in flight. `--lease-confirm-seconds` holds the stops open
   for that long first.
3. Fetch NEW on every host, so a host that cannot get it is found before
   anything stops.
4. NEW's offline `meiva schedules verify` over the gateway's stored schedule
   table — a red or an unevaluable check refuses `down` before any stop.
5. Open the window's Grafana silence on the gateway host.
6. `meiva stop --yes` with the operator's flags on each runner, then the gateway
   (which also gets `--keep-infra` or `--stop-infra`). A stop must leave the host
   paused/stopped; a failure prints the retry step and brings back, with the
   operator's start selection, the hosts this run already stopped
   ([[cli/docs/fleet-update-window/failed-stop-recovery.meiva.okf.md]]).
7. Switch every checkout: check out NEW detached, repair legacy read-only
   `.venv` directories (early converge passes left them 0555, which `uv sync`
   cannot write through), `uv sync --frozen`, then require a clean tree. The
   check shows a dirty tree and never cleans it: untracked files are the
   operator's to move (not delete), never the update's.

## The up half, in order

1. `meiva start` with the operator's service selection on the gateway, then each runner; on macOS through a run-once
   `gui/<uid>` LaunchAgent, because the helper's launchd job lives in `gui/<uid>`. Each start must release its maintenance hold.
2. The roster gate: every listed machine online with checkout and running code
   on one commit; unlisted machines are reported and never block.
3. The gateway smoke-tests each listed agent-runner with a real agent.
4. `meiva packages refresh` on every host; it never forces.
5. The drift checks, last: `meiva schedules verify --no-notify` on the gateway
   and `meiva plugins verify` on every host. Every check runs to the end and a
   red fails `up` with all the details — after the smoke and the refresh, so a
   red never skips them.
6. Expire the silence.

A failure ends the half: nothing rolls back, the message names the step to
fix, and the whole half is rerun. Refusals exit 2 (nothing touched); failures
exit 1. `--dry-run` really runs only the read-only steps — each host's probe
and the stored-schedule verify — and prints every other effect without
performing it. With `run`, it previews both halves without stopping, switching
or starting hosts.

## Contracts

```meiva-contract
id = "complete-update"
kind = "invariant"
statement = "run requires a target and every lifecycle choice as an explicit argument (a missing or conflicting one exits 2, all listed at once, before any host is touched), forwards the stop and start choices verbatim to each host (--keep-infra/--stop-infra to the gateway's stop only), and executes down then up in one log; a failure stops at its phase, names that half for recovery and never rolls back or retries; a failed down never starts a host, and --dry-run previews both halves without their effects."
code = ["cli/fleet_update.py:main", "cli/fleet_update.py:down"]
tests = ["cli/tests/fleet_update/test_fleet_update.py::test_runners_stop_first_and_start_last", "cli/tests/fleet_update/test_fleet_update.py::test_run_does_not_start_after_a_failed_down", "cli/tests/fleet_update/test_fleet_update.py::test_run_reports_up_failure_without_repeating_down", "cli/tests/fleet_update/test_fleet_update.py::test_run_dry_run_plans_both_halves_without_mutation", "cli/tests/fleet_update/test_fleet_update.py::test_run_uses_one_log_and_accepts_both_halves_options", "cli/tests/fleet_update/test_fleet_update.py::test_run_requires_a_target_before_contacting_hosts", "cli/tests/fleet_update/test_fleet_update.py::test_the_operators_arguments_are_spliced_verbatim_into_each_hosts_stop_and_start", "cli/tests/fleet_update/test_fleet_update.py::test_missing_choices_are_all_listed_at_once_and_no_host_is_touched"]
```

Runners stop first and start last — the gateway stops after every runner and starts before every runner; NEW is fetched on every host before any stop; the switch follows the stops; the drift checks run last and every one runs, so a red never skips the smoke or the refresh; the first failure ends the half with no rollback (refusals exit 2, failures exit 1).

Before up completes: every listed machine is online on the roster with checkout and running code on one commit — re-read on the poll interval until --roster-timeout; a listed host is matched by the machine name it reports, a name with no row fails, unlisted machines are reported and never block; the gateway has smoke-tested each listed agent-runner; and every host ran meiva packages refresh, which replaces a differing local copy and never forces.

Parent: [[cli/docs/cli.meiva.okf.md]].
