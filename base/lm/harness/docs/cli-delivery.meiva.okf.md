---
type: doc
title: Harness CLI and environment delivery
description: How `wiring.session_environ`, `with_harness_path`, `MEIVA_PYTHON`, and `resolve_harness_binary` deliver executables and the Meiva interpreter to external harness sessions.
tags:
- base
- library
- llm-inference
- external-agents
---

# Harness CLI and environment delivery

Part of [[base/lm/harness/docs/harness.meiva.okf.md|Provider harness]].

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
system dirs. `wiring.session_environ` also stamps `MEIVA_PYTHON` (`sys.executable`)
and `_SESSION_DELIVERY_KEYS` preserves it across `ClaudeSession`'s allowlist so
`"$MEIVA_PYTHON" -c ...` always runs Meiva's interpreter even when a CLI's shell
tool spawns a login shell (`/bin/zsh -lc`) that reorders `PATH` or works inside a
repository with a foreign `.venv`. The same binary directory list feeds the probe in
`base/agents/birth/harness.py` and the `launchd` job PATH in
`base/host/system/cron/mechanics.py`, so all three agree on where the CLIs live.

The pinned interpreter fixes which `python` runs, not which code it imports:
`python -c` puts the working directory first on `sys.path`, so that command run
inside a clone of this repository imports that clone's `base`/`meiva` and the
home's ledger refuses the process it then is (`this process runs <cwd>, not the
home's source checkout <source>`). Run the command outside any checkout
(`cd /tmp`), or pin `PYTHONSAFEPATH=1` on that one command; never export it, since
every child inherits it and a checkout's `pytest` imports its own tree from the
working directory.

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
