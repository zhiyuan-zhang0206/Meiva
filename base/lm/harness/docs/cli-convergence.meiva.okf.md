---
type: doc
title: External harness CLI convergence and delivery
description: 'External harness CLI path discovery, version catalog, host convergence, and scheduled updates.'
tags:
- base
- library
- llm-inference
- external-agents
---

# External harness CLI convergence and delivery

Part of [[base/lm/harness/docs/harness.meiva.okf.md|Provider harness]].

## Where the CLI comes from

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
system dirs. The same list feeds the probe in `base/agents/birth/harness.py` and the
`launchd` job PATH in `base/host/system/cron/mechanics.py`, so all three agree on where the
CLIs live.

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

## CLI version convergence and updates

External harness CLIs installed on host machines are kept in sync with the repository's
pinned versions:

1. **Catalog owner:** `base/host/env/harness/catalog.py` defines the canonical specifications (`CATALOG`),
   pinned versions, installation commands, and pure version decision helpers (`needs_cli_update`).
2. **Host convergence step:** `cli/commands/lifecycle/converge_host.py` registers the "external harness CLIs"
   step for machines with the `agent-runner` role (`ensure_external_harness_clis`).
3. **Local convergence engine:** `base/host/env/harness/convergence.py` coordinates non-disruptive updates under
   a file lock (`$MEIVA_HOME/run/harness-cli-update.lock`). For `agy`, it verifies the binary in a staging directory
   before moving it atomically into place. For npm-managed CLIs (`claude`, `codex`, `dsh`), it uses a user-level
   prefix and rolls back to a preserved backup if verification fails. Install logs are appended to
   `$MEIVA_HOME/onboarding/install-log.jsonl` with sensitive tokens redacted.
4. **Daily runner schedule:** `harness-cli-update` (`schedules/harness-cli-update-schedule.py`) runs at 04:30 cluster
   time on every machine with `agent-runner` placement, ensuring long-running clusters track pinned versions
   without waking agents or opening operator tasks.
