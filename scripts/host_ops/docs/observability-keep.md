# Observability keep advice

`scripts/host_ops/observability_keep.py OLD NEW` is the read-only helper an operator runs before a
fleet update window to decide whether the observability stack (`loki`, `prometheus`, `tempo`,
`grafana`, `otel-collector`) can stay up through the window.

## Why a helper and not a fleet-update decision

The fleet update never judges a window: every stop choice is an explicit flag and is forwarded
unchanged ([2026-10-20 explicit lifecycle choices](../../../docs/decisions/runtime/updates/release/2026-10-20-explicit-lifecycle-choices.md)).
The helper therefore only prints the flags; the operator reads the verdict and types them.

Keeping the stack saves the window's longest single stop: Tempo exits gracefully in ~30 s while
every other unit on the gateway is already stopped (measured 2.1 s -> 33.5 s on `ubuntu` when the
native Tempo backend shipped), and it keeps traces, logs and metrics flowing through the window.

## The verdict

- `keep: --keep-service loki --keep-service prometheus --keep-service tempo --keep-service
  grafana --keep-service otel-collector` (exit 0): no changed path can reach the stack. Pass these
  flags in place of `--keep-no-services`; `meiva stop` leaves the units running and `meiva start`
  finds them already running (`up` is idempotent per unit) and still checks their readiness.
- `restart: N changed path(s) ...` (exit 1): leave the five out of `--keep-service` so the window
  restarts them and the new commit's configuration, rules, dashboards and binaries take effect.
- `restart: no verdict ...` (exit 2): an unresolvable commit or a missing git; treat it as restart.

## What the verdict does not cover

- **OLD is the commit the stack was started on**, normally each host's current checkout. The helper
  takes one OLD; when hosts run different commits, run it once per distinct commit and keep only
  if every run says `keep`.
- **`--keep-service` needs `--keep-extras`.** `--stop-extras` is refused at argument parsing
  (`extras_keep_refusal`), before any host is touched.
- **Keeping `otel-collector` keeps `meiva-root` on every host** (a root with a kept unit is not
  stopped), so the window's kept-root generation check applies to all hosts: a NEW whose launch
  manifest differs from the live root refuses before the stop. That refusal is safe, and the
  helper's `keep` does not predict it.
- **A runner that has no such unit ignores the name** (the five names are roster entries on every
  host); only `otel-collector` exists on a runner.
- **Dashboards also come from installed plugins** (database rows, not git paths), which the helper
  cannot see.
- **Listener and port code** (`base/cluster/listener_plan.py`, `port_preflight.py`) is a restart
  reason although it names no backend: the units' bind addresses come from it.

## The path list is one-sided on purpose

A wrong entry may cost a window a restart it could have skipped; it must never keep a stack the
commit changed. The list (`RESTART_PREFIXES`) therefore names whole directories, and
`scripts/host_ops/tests/test_observability_keep.py` scans every tracked file for a backend or
collector name and fails when the file is neither listed nor reviewed as a consumer of the stack
(`_CONSUMERS`: clients that dial it, commands that show it, tooling). Adding a file that names a
backend forces that decision in the same change.

## Boundary

The stack stays on the code and configuration it was started with until a window restarts it. A
window that keeps it across a commit the helper called unchanged is safe by construction of the
list; a window that keeps it against a `restart` verdict leaves the stack stale and is the
operator's explicit choice, as with `--keep-shell-sessions`.
