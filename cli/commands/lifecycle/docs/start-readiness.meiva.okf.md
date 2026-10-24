---
type: doc
title: Start readiness
description: Selected core protocol readiness gates start; optional capabilities remain diagnostic.
tags:
- cli
- lifecycle
- readiness
---

# Start readiness

`meiva start` first validates the persisted home (initialized by `meiva init`) and validates the service selection named on the command line (nothing is read back from an earlier start).
Before converge, `cli/commands/data_plane/host_dependencies/` runs the host
dependency preflight: it resolves every external executable and OS service the
selected roster executes, installs a missing group through `scripts/provision/*`
(Homebrew on macOS, apt on Debian/Ubuntu), re-probes, and refuses the start
before any service launch when something is still missing. A runner-only unit
and a remote-managed plane skip the local data-plane group; a headless gateway
skips Node; `MEIVA_OS_JOBS_ENABLED=0` (or a non-default `$MEIVA_HOME`) skips the
scheduler group. The
pre-bind gate in `cli/commands/lifecycle/start/__init__.py:refuse_occupied_health_ports` refuses
ports held by another unit before launching application processes. It reads the
selected core roster; optional port conflicts cannot block the core. The refusal's
words (`start/occupied_ports.py`) are a function of the occupants it is handed.

`cli/commands/lifecycle/root_driver.py` owns admission, launch and observation of the
single application root. An idempotent start observes the live root generation;
a cold start launches one through the platform's ordinary process owner.
macOS places the permission helper above the root. Linux starts the root under
the caller or systemd without a helper.

The start entry (`cli/commands/lifecycle/start/`) is a shell too. Each decision it makes is a
`strict` module of that package over values the shell has read: `stand_down_gate` (what to do
about the home's stand-down record), `admission` (a destroyed home or a disposable prod
checkout, capabilities that differ from the ones `meiva init` recorded, whether a pure
runner probes its gateway), `cold_start_plan` (the ordered preparation of a start with no live
root, executed by `cold_start`, the schema checks of every admitted start, and who may migrate), `readiness_plan` (what
is reported and the exit code once readiness was waited for), `occupied_ports` and
`gateway_hint` (the words), `login_link_plan` (whether to mint a one-time login link) and
`start_config` (the configuration values a start reads, read once). The shell dials the root,
executes each plan in order and stops at the first step that fails.

The driver is a shell: it reads the clock, the control socket and the processes, prints,
and executes plans. What a status snapshot means is decided by values in
`cli/commands/lifecycle/root_control/` — the typed `snapshot`, the `reconcile_plan` (which
units to resume, which generation to refuse, what a stop takes down), the `poll_plan` (the next
step of every wait on the root, its keeper or a service) and the `launch_plan` (the daemon
command line and the keeper's seed). The home and the settings a launch reads
(`RootLaunchInputs`) are handed in by the start entry; the driver reads no settings. It still
reads the process environment through `managed_service_env` when it builds the launched root's
environment, and the start and stop shells reach the process-environment home through the base
owners that take no home (`start_serving`, `launch_failures.record`, `status_journal`).

The CLI's existing caller-owned child list retains a directly launched root
through readiness, any later command work and the CLI telemetry drain, just as
it retains directly launched PostgreSQL children. A failed readiness attempt
does not release that handle. An embedded caller keeps the same explicit list
for as long as it needs the native PID retained; root launch neither reaps nor
terminates a child when a start phase returns. The list is a required argument of each call
that launches a child (`cmd_start_body`, `prepare_cold_start`, the LGTM toggle), never a field of
the frozen values the start passes down; only the parser handlers, which may be built without
one, refuse a launching verb that has none.

`cmd_start_body` runs under the home's lifecycle lock (`exclusive_resources`) and returns an
exit code: the same body serves `meiva start` and the start leg of `meiva restart`.

After launch, `cli/commands/lifecycle/root_readiness.py` observes the roster through
the root driver. Selected core services must be running and answer their protocol.
Optional services are reported without blocking serving or adding a startup wait.
Protocol availability is an observation, not a proof that an endpoint belongs to
a particular native process generation. Process ownership for actual signals
remains with the launch/stop owner. The rollout launch-failure record contains
only core launch failures.

## Host dependencies

The preflight resolves every external executable and OS service the selected
roster executes, installs a provisionable group through `scripts/provision/*`
(Homebrew on macOS, apt on Debian/Ubuntu), re-probes, and refuses the start
before any service launch when something is still missing — naming the tool and
the command that fixes the host. `crontab` and `systemctl` are part of that
table, so a host without `cron` no longer dies at the WAL-G job step in the
middle of converge. [[cli/commands/lifecycle/docs/start-host-dependencies.meiva.okf.md|The dependency table]]
lists every row, its owner and the behavior when it is absent.

## Related contracts

- [[cli/commands/lifecycle/docs/readiness-verdict.meiva.okf.md]] — exit codes,
  core deadlines, optional availability and alerts.
- [[cli/docs/start_identity.meiva.okf.md]] — init identity and start admission.
- [[base/cluster/machine/docs/machine.meiva.okf.md]] — capabilities and the selected service roster.
