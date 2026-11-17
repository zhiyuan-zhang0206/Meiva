# Fleet update over SSH

`meiva update run` updates a networked source-mode cluster over key-based SSH
in one invocation: `down` stops every host and switches its checkout to NEW,
then `up` starts the cluster and proves it. The separate `down` and `up`
commands remain for intermediate manual work and recovery. The operator-facing procedure is
the runbook
([updating a networked cluster in source mode](../../docs/conventions/runbook.md#updating-a-networked-cluster-in-source-mode));
this node owns the code-side order, and the window's guards are
[Fleet update window guarantees](fleet-update-window/fleet-update-window.md).
The halves are idempotent: after a failure, fix the cause and rerun the failed
half. `run` accepts both halves' options, resolves the target channel (`latest` by default, or `stable` / `--track-branch`), uses one log and
stops at the first failure; a failed `down` never enters `up`. A failed `up`
does not require repeating `down`. Its
shape is [Fleet update structure](../fleet_update/docs/structure.md).

## Every choice is an explicit argument

Every lifecycle choice is a required flag, all of them listed together when missing or
conflicting and refused before any host is touched; target selection is verified locally
before any SSH. The flags, their modes and the forwarded stop and start choices are
[Fleet update choices](fleet-update-choices.md).

## The down half, in order

1. Preflight all listed hosts in parallel. OLD (the one commit every host shares that is not
   NEW) only feeds the `.python-version` refusal.
2. Fetch NEW (updating `refs/fleet-update/target` to NEW) and probe helper facts on the gateway
   first, then across all runners in parallel, so runners whose `origin` is the gateway's
   checkout can fetch NEW from it and any host that cannot get NEW is found before anything stops.
3. NEW's offline `meiva schedules verify` over the gateway's stored schedule
   table — a red or an unevaluable check refuses `down` before any stop.
4. Pre-stage NEW across every host in parallel while the cluster is still serving:
   warm changed Python wheels (`uv.lock` or `.python-version`, seeded from `$S/.venv`
   via APFS CoW clone on macOS, with `UV_HTTP_TIMEOUT` defaulted to 120s) and `pnpm`
   store entries (lockfile, workspace configuration or patch changes) in a temporary
   directory from NEW’s Git objects, including optional workspace configuration and
   patches when NEW contains them. Archive/extraction errors abort preparation;
   pnpm fetch uses an isolated linker only in this temporary cache directory
   (the application retains its configured linker), succeeds silently and prints
   captured stdout/stderr on failure — failing fast if
   either cache warm exits non-zero — and converge external agent harness CLIs
   (`claude`, `codex`, `agy`, `dsh`) on `agent-runner` hosts so neither `switch` nor
   `meiva start` spends the outage downloading packages or harness binaries.
5. Open the window's Grafana silence on the gateway host.
6. `meiva stop --yes` with the operator's flags across all runners in parallel,
   waiting for every in-flight runner stop to finish before stopping the gateway
   (which also gets `--keep-infra` or `--stop-infra`). A stop must exit 0 and leave
   a completed stop in the host's journal; a failure prints the retry step and brings back, with the
   operator's start selection, the hosts this run already stopped
   ([The failed-stop recovery](fleet-update-window/failed-stop-recovery.md)).
7. Switch every checkout in parallel: check out NEW detached, repair legacy read-only
   `.venv` directories (early converge passes left them 0555, which `uv sync`
   cannot write through), `uv sync --frozen --compile-bytecode --reinstall-package meiva`, then require a clean tree. The
   check shows a dirty tree and never cleans it: untracked files are the
   operator's to move (not delete), never the update's.

## The up half

Ordered steps (`meiva start` on gateway then runners, roster convergence,
`meiva packages refresh`, drift checks and silence expiry) and completion
gates live in [Fleet update up half and completion gates](fleet-update-window/up-half.md).

A failure ends the half: nothing rolls back, the message names the step to
fix, and the whole half is rerun. Refusals exit 2 (nothing touched); failures
exit 1. `--dry-run` really runs only the read-only steps — each host's probe
and the stored-schedule verify — and prints every other effect without
performing it. With `run`, it previews both halves without stopping, switching
or starting hosts.

## The operation identity

A window mints one identity and stamps it on every effect command it sends
(`plan/operation.py`); each host's own `meiva` reads it back into the caller block of its
lifecycle audit row (`base/native_process/root_control/client.py`, written by
`cli/database/lifecycle_audit.py`). Three facts come out of it, and a reader gets them from
`meiva cluster ops`:

- `MEIVA_OPERATION_ID` — one per window, minted at the entry, never derived from a time or a
  host, so a five-host restart is one operation rather than five unrelated stops.
- `MEIVA_OPERATION_INITIATOR` — `user:<name>@<host>` for an immediate window, or the
  explicit `user` / `agent:<id>` source for an OS-scheduled window; the name of the machine that started it survives in the row's
  `spawn.ssh_connection` even when this is empty.
- `MEIVA_OPERATION_GATE` / `MEIVA_OPERATION_GATE_REASON` — `satisfied` when the target was
  resolved through the certified channel (`stable` or `latest`); `overridden` when the operator
  asked for `--track-branch`, and the reason names the branch. This is the field that makes a
  bypass of the release gate a queryable fact instead of an assumption.

Only effect commands carry it: a read is a question, not an operation.

## Contracts

```meiva-contract
id = "complete-update"
kind = "invariant"
statement = "run requires a target and every lifecycle choice as an explicit argument (a missing or conflicting one exits 2, all listed at once, before any host is touched), forwards the stop and start choices verbatim to each host (--keep-infra/--stop-infra to the gateway's stop only), and executes down then up in one log; a failure stops at its phase, names that half for recovery and never rolls back or retries; a failed down never starts a host, and --dry-run previews both halves without their effects."
code = ["cli/fleet_update/__init__.py:main", "cli/fleet_update/down.py:down", "cli/fleet_update/plan/release_target.py:resolve_update_target"]
tests = ["cli/tests/fleet_update/test_fleet_update.py::test_runners_stop_first_and_start_last", "cli/tests/fleet_update/test_fleet_update.py::test_run_does_not_start_after_a_failed_down", "cli/tests/fleet_update/test_fleet_update.py::test_run_reports_up_failure_without_repeating_down", "cli/tests/fleet_update/test_fleet_update.py::test_run_dry_run_plans_both_halves_without_mutation", "cli/tests/fleet_update/test_fleet_update.py::test_run_uses_one_log_and_accepts_both_halves_options", "cli/tests/fleet_update/test_fleet_update.py::test_run_requires_a_target_before_contacting_hosts", "cli/tests/fleet_update/test_fleet_update_choices.py::test_the_operators_arguments_are_spliced_verbatim_into_each_hosts_stop_and_start", "cli/tests/fleet_update/test_fleet_update_choices.py::test_missing_choices_are_all_listed_at_once_and_no_host_is_touched", "cli/tests/fleet_update/test_fleet_update_gates.py::test_uncertified_or_tampered_targets_refuse_exit_2_before_any_ssh_command"]
```

The gateway stops after every runner and starts before any runner; NEW is fetched on every host before any stop; the switch follows the stops; the drift checks run last and every one runs, so a red never skips the refresh; the first failure ends the half with no rollback (refusals exit 2, failures exit 1).

Parent: [CLI](cli.md).

## Scheduled host-owned update

`meiva update run --at +5m --source agent:123 ...` accepts the same explicit
stop/start choices as `run`, including `--message TEXT` or `--no-message`.
Use `--source user` for a human initiator. `--at` accepts positive relative
seconds/minutes/hours/days (`+60s`, `+5m`) or a future ISO timestamp with timezone.
It requires `--execute` and schedules the complete `run` window.

Submission is safe from an agent shell: it resolves and pins the target SHA,
performs the ordinary host preflight, fetch, root/schedule checks and prestage,
then warns every currently running agent except the initiator. Sleeping agents
receive no submission message. Preparation or timer registration failures exit
2 without stopping any service. The submitting agent must finish its turn before
the deadline, as it is included in the final running-agent check.

The local operator host owns a systemd user timer on Linux or a launchd one-shot
on macOS (the latter owns its deadline wait outside Meiva). The private record
under `<log-dir>/scheduled/<operation_id>.json` holds `run_at`, `initiator`, the
pinned target and outcome. The command prints its path. Inspect or cancel it with
`meiva update status RECORD` / `meiva update cancel RECORD`. Cancellation refuses
an executing job. Retain the operator checkout, interpreter and records until the
operation finishes; they must live outside the serving checkouts being switched.
Submission refuses the local serving `~/.meiva/source` checkout/interpreter; invoke
the command with the independent operator checkout's own `.venv/bin/meiva`.
The host user manager / macOS GUI login must remain available for execution.

At the deadline the job rechecks preparation and queries every directory page
immediately before stop. If any agent is running, it stops no service and sends
the busy list to the recorded agent initiator and those running agents. Otherwise
it performs the ordinary down/switch/up chain and sends the recorded agent
initiator the result after up and its verification. A `user` initiator reads the
record and logs. Failed message delivery leaves the durable record and log for
recovery. This is one strict attempt: agent activity can change after the check,
and there is no inbox lock or automatic retry. Direct synchronous run/down keep
the existing agent-shell refusal.
