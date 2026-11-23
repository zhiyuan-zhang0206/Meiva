# Harness CLI and environment delivery

Part of [Provider harness](harness.md).

A harness session starts from the daemon child environment, which is a positive
allowlist and therefore carries no `PATH`: a bare `codex`/`claude`/`agy` would
resolve against the OS default (`/usr/bin:/bin:/usr/sbin:/sbin`), so a real spawn
failed while `which codex` in a shell succeeded. Two rules close that gap.

The environment a session starts from is a required value: the host root builds it once
(`child_env(process_environment())`) and hands it down as
`HostPolicy.harness_environ`; `bridge_for`, `open_session` and `session_environ` never fall
back to the process environment.

`base/host/env/harness_binaries.py::with_harness_path` rebuilds `PATH` at the
session delivery point (`wiring.session_environ`): the host's own entries first,
then the directories harness CLIs install into (`~/.local/bin`, `~/.npm-global/bin`,
`~/.bun/bin`, `~/.claude/local`, `/usr/local/bin`, `/opt/homebrew/bin`), then the
system dirs. `wiring.session_environ` sets `MEIVA_PYTHON` to the absolute path
of `meiva_python.sh` and pins the host's `sys.executable` in
`MEIVA_PYTHON_EXECUTABLE`. The Claude session allowlist preserves both
values. Managed SDK shell sessions pin their own interpreter the same way,
including sessions created by `meiva.shell.run_background`.

The launcher runs the pinned interpreter with `-P` for `-c`, `-m`, and script
invocations. Python skips its implicit working-directory or script-directory
import entry, so running the SDK from a different checkout uses the package
installed in the pinned interpreter's environment. The launcher leaves
`PYTHONSAFEPATH` unset in a clean environment, so a child Python process uses
its usual import behavior. Scripts that need sibling modules must make those
imports available explicitly.

The same binary directory list feeds the probe in `base/agents/birth/harness.py`
and the `launchd` job PATH in `base/host/system/cron/mechanics.py`, so all three
agree on where the CLIs live.

Every unit this repository installs composes its PATH through
`harness_binaries.harness_unit_path`, which appends the table and then refuses a
result that omits any of it (`HarnessPathIncompleteError`, naming the unit and the
directories): a hand-written narrow PATH fails where the unit is written, not at the
first spawn.

Each adapter then spawns the path it resolved, not the name: `codex` and `agy` cache
the resolution, and the Claude Code session passes the SDK a `cli_path` because the
SDK resolves a bare name with `shutil.which` against its own process environment. A
failure names the PATH and every directory that was searched instead of a bare
`No such file or directory`. A successful search returns an absolute path before
the child changes to its workspace, including when PATH contains relative entries.
The path keeps symlinks intact; it does not resolve their targets. An explicitly
configured path containing a separator is validated and returned unchanged.

## Native OpenTelemetry

Hosted Codex sessions export metrics and traces. Hosted Claude Code sessions
export metrics, events and beta traces. The host uses a dedicated loopback
receiver at `/meiva-harness/v1/*`. Its port is derived from the unit's local
OTLP port by shifting within the unprivileged range, keeping adjacent unit
ports distinct. Converge checks for conflicts before replacing the collector
configuration and recognizes an existing listener owned by the same unit.
If the derived port is occupied, converge warns and renders only the ordinary
receivers. When enabling launchers, each existing proxy bypass variable keeps
its own entries, and the collector's loopback address is appended without
changing which spelling a child prefers. Before starting a CLI, the host sends an empty OTLP request
and verifies the live receiver's redaction marker within a half-second total
budget. The probe accepts at most 16 response bytes. It logs a warning and starts
the CLI with export disabled if the probe fails. During collector replacement,
a CLI that already has the dedicated endpoint can only send to the redacted
receiver. It enables these exporters
when `MEIVA_TELEMETRY_OTLP_ENABLED` is on and enables their spans when
`MEIVA_TRACE_ENABLED` is also on. OTLP resources identify the agent through
`meiva.agent_id`, `harness` and `service.instance.id`. The per-agent instance
keeps cumulative Prometheus counters from different agents in distinct series.
Harness-reported token and cost observations are for diagnostics. Billing and
product usage continue to come from the existing Meiva usage path.

Codex passes W3C Trace Context in each app-server JSON-RPC request. Its native
OTLP log exporter is disabled because tool arguments can appear there even
when prompt and tool-output recording are disabled. Claude Code has per-process
TRACEPARENT support. The bridge clears it at SDK launch so a persistent process
can emit independent interaction traces without adding later turns to the first
Meiva turn. Follow [Claude Agent SDK per-message context support](https://github.com/anthropics/claude-agent-sdk-python/issues/1126)
for the remaining integration point.

The harnesses' content controls are disabled at launch, and the collector
applies a diagnostic attribute allowlist to their native signals before local
storage or relay. The collector also replaces log bodies with an event label
and clears free-text span status messages.

The privacy filter also normalizes diagnostic names and removes span links,
trace state and metric exemplars before export.
