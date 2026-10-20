# Lifecycle CLIs take every choice explicitly: no defaults, no inference

## Context

`meiva stop`, `meiva start`, `meiva restart` and `python -m cli.fleet_update` each made
choices the operator never wrote down. Shell sessions closed unless `pty-sessions` sat in
`--keep-service`. The browser and the extras were stopped by `stop` and kept by
`restart`. The stop budget and the restart mode had defaults. A bare `meiva start`
reused the last persisted selection. The fleet update derived its keep set from the
window's diff
([2026-10-17-component-scoped-keep-on-update](2026-10-17-component-scoped-keep-on-update.md)).
An operator, human or agent, could not see from the command line what a lifecycle
command would keep.

## Decision

User ruling, 2026-10-20 (direct): no automatic judgement and no default parameter in
any lifecycle CLI; every choice is specified explicitly. Terms follow the SDK:
"shell sessions".

- Every keep-or-stop choice, mode, budget and service selection is a required argument.
  A missing one is a usage error (exit 2), and every missing choice is named at once.
- Machine facts only refuse a choice that does not apply. For example, a host without
  the gateway capability refuses `--keep-infra/--stop-infra`, and unreadable
  capabilities refuse the stop. Machine facts may also pick an implementation, such as
  the macOS LaunchAgent start. They never answer a choice.
- Nothing is derived from an earlier run:
  - The service selection is not persisted.
  - `meiva lgtm on|off` and standalone `meiva converge` take the selection on their
    command line.
  - The fleet keep decision is deleted.
- Callers forward the operator's choices verbatim. The fleet update checks names and
  budgets before it touches any host.

The current rule is kept in
[CLI argument discipline](../../../../conventions/engineering/cli-argument-discipline.md).

## Fixed by the verb, not defaults

Each of these follows from what the verb is, so none is an option:

- **`meiva restart` keeps a gateway's data plane.** Its start leg needs the database;
  a restart that stopped it would not be a restart. To restart the data plane, run
  `meiva stop --stop-infra …`, then `meiva start …`.
- **The fleet update always passes `--yes` to each host's `meiva stop`.** It runs over
  non-interactive SSH, where a prompt could not be answered.
- **`meiva cluster destroy` adds `--stop-infra` on a gateway.** A detach leaves nothing
  of the cluster running, and `--drop-db` may only remove the data directories of a
  stopped instance. Shell sessions, the browser and the extras are stopped for the
  same reason. The operator still names the stop mode and the budget.
- **`--drop-db` keeping data by default is left for its own change.** It is not a
  stop/start/restart/fleet choice. It is the next candidate under the same ruling.

## Consequences

- The first `down` after this change must run from an operator checkout of the previous
  code. Each host's old `meiva stop` does not know the new flags.
- An existing OS boot job (launchd plist or systemd unit) runs a bare
  `meiva start --boot` and is refused until a cold start runs converge, which rewrites
  it with the selection.
- The operator now owns the risk that the fleet keep decision used to guard. Keeping
  shell sessions or the browser across a window that changes their code leaves them on
  the old code until a later explicit `--close-shell-sessions` or `--stop-browser`.
