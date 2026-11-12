# External harness CLI convergence and delivery

Part of [Provider harness](harness.md).

## Where the CLI comes from

The session environment, `PATH` rebuild and resolved-path spawning are owned by
[Harness CLI and environment delivery](cli-delivery.md).

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
5. **Claude login keepalive:** `claude-auth-keepalive` (`schedules/jobs/harness/claude-auth-keepalive-schedule.py`)
   runs one minimal `claude -p` per machine every six hours so the CLI refreshes its own login; it never calls the
   OAuth refresh endpoint or touches credentials (see `schedules/README.md`).
