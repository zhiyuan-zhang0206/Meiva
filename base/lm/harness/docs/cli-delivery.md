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
