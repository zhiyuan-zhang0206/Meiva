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
half. `run` accepts both halves' options, requires one explicit target selection, uses one log and
stops at the first failure; a failed `down` never enters `up`. A failed `up`
does not require repeating `down`. The package imports no application code, so
it runs from a bare operator checkout, whatever generation the cluster runs; its
shape is [[cli/fleet_update/docs/structure.meiva.okf.md]].

## Every choice is an explicit argument

Every lifecycle choice is a required flag, all of them listed together when missing or
conflicting and refused before any host is touched; target selection is verified locally
before any SSH. The flags, their modes and the forwarded stop and start choices are
[[cli/docs/fleet-update-choices.meiva.okf.md]].

## The down half, in order

1. Preflight all listed hosts in parallel. OLD (the one commit every host shares that is not
   NEW) only feeds the `.python-version` refusal.
2. Read the gateway's active impersonation leases and name them: a stop ends
   every takeover in flight. `--lease-confirm-seconds` holds the stops open
   for that long first.
3. Fetch NEW and probe helper facts across every host in parallel, so a host
   that cannot get it is found before anything stops.
4. NEW's offline `meiva schedules verify` over the gateway's stored schedule
   table — a red or an unevaluable check refuses `down` before any stop.
5. Open the window's Grafana silence on the gateway host.
6. `meiva stop --yes` with the operator's flags across all runners in parallel,
   waiting for every in-flight runner stop to finish before stopping the gateway
   (which also gets `--keep-infra` or `--stop-infra`). A stop must exit 0 and leave
   a completed stop in the host's journal; a failure prints the retry step and brings back, with the
   operator's start selection, the hosts this run already stopped
   ([[cli/docs/fleet-update-window/failed-stop-recovery.meiva.okf.md]]).
7. Switch every checkout in parallel: check out NEW detached, repair legacy read-only
   `.venv` directories (early converge passes left them 0555, which `uv sync`
   cannot write through), `uv sync --frozen --compile-bytecode --reinstall-package meiva`, then require a clean tree. The
   check shows a dirty tree and never cleans it: untracked files are the
   operator's to move (not delete), never the update's.

## The up half

Ordered steps (`meiva start` on gateway then runners, roster convergence, runner
smoke, `meiva packages refresh`, drift checks and silence expiry) and completion
gates live in [[cli/docs/fleet-update-window/up-half.meiva.okf.md]].

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
code = ["cli/fleet_update/__init__.py:main", "cli/fleet_update/down.py:down", "cli/fleet_update/plan/release_target.py:resolve_update_target"]
tests = ["cli/tests/fleet_update/test_fleet_update.py::test_runners_stop_first_and_start_last", "cli/tests/fleet_update/test_fleet_update.py::test_run_does_not_start_after_a_failed_down", "cli/tests/fleet_update/test_fleet_update.py::test_run_reports_up_failure_without_repeating_down", "cli/tests/fleet_update/test_fleet_update.py::test_run_dry_run_plans_both_halves_without_mutation", "cli/tests/fleet_update/test_fleet_update.py::test_run_uses_one_log_and_accepts_both_halves_options", "cli/tests/fleet_update/test_fleet_update.py::test_run_requires_a_target_before_contacting_hosts", "cli/tests/fleet_update/test_fleet_update_choices.py::test_the_operators_arguments_are_spliced_verbatim_into_each_hosts_stop_and_start", "cli/tests/fleet_update/test_fleet_update_choices.py::test_missing_choices_are_all_listed_at_once_and_no_host_is_touched", "cli/tests/fleet_update/test_fleet_update_gates.py::test_uncertified_or_tampered_targets_refuse_exit_2_before_any_ssh_command"]
```

The gateway stops after every runner and starts before any runner; NEW is fetched on every host before any stop; the switch follows the stops; the drift checks run last and every one runs, so a red never skips the smoke or the refresh; the first failure ends the half with no rollback (refusals exit 2, failures exit 1).

Parent: [[cli/docs/cli.meiva.okf.md]].
