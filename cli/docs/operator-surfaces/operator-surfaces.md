# CLI Operator Surfaces

## Agent and context commands

- `meiva agents ls/timeline/send/cancel/compact/restart/resurrect/terminate/kill/transfer`: `ls` renders the
  authenticated agent summary projection as stable `id / status / machine /
  label` columns. It does not expose runner-local workspace paths. `timeline`
  reads standing context and recent conversation items. `send` delivers a chat
  message (auto-resurrecting a terminated target) and requires explicit
  `--source` provenance (`user`, `external_agent:<subject>[:<instance>]`,
  `shell:N`, `watcher:N`, or `schedule:N`;
  [external caller provenance](../../../base/agents/messages/docs/external_caller.md)).
  `restart`, `resurrect`, `terminate`, `kill` and `transfer --to MACHINE` require
  `--message TEXT | --no-message` (blank `--message` exits 2 at parse); `cancel`
  and `compact` take neither. `transfer` relocates an `idling` or `terminated`
  native agent and its workspace across machines and refuses running agents with
  409 ([agent transfer](../../../gateway/agents/docs/agent-transfer.md)).
- `meiva notices list/resolve/clear`: `resolve` reports success only when the
  gateway accepts the action. A 409 conflict is an error, including an absent
  notice, an action-kind mismatch, or an already-resolved answer/dismissal.
  Every notice requires a response; fresh mark-read operations are rejected.
  `clear --agent` explicitly dismisses each open notice after confirmation
  (`--force` skips confirmation). `clear --stale` targets only terminated agents.
  Resolution is one keyed attempt (`principal-v1`) with a fresh key per invocation.
- `meiva memory init`: explicitly provisions the memory-pool checkout and
  plugin-owned templates. Branch validation runs here, never during converge or
  start.
- `meiva memory refresh`: refreshes the gateway Git checkout; the semantic index
  processes changes on the next indexer patrol. Pool consolidation
  runs on the gateway schedule through `schedules/memory-steward-schedule.py`
  and `meiva_builtins/plugins/meiva_memory/skills/scripts/{steward,arbiter_merge}.py`.
- `meiva memory search QUERY [--limit K] [--json]`: posts an authenticated search
  to the gateway. The human table preserves relative `path` values alongside
  tags and descriptions; JSON emits the gateway response's `results` list
  without converting paths or normalizing null and empty metadata.

## Read-only cluster queries

These verbs are thin clients of the gateway's read routes: they forward the filters you name,
print a table (or `--json`, the gateway's response unchanged) and leave windows and page sizes to
the gateway. A refusal prints the gateway's reason on stderr and exits 1. None of them touches the
database or takes a free-form query; an agent that needs an answer from cluster data uses these
instead of SQL or hand-built HTTP calls.

- `meiva events ls [--category audit|telemetry|log] [--name EVENT] [--agent ID] [--trace ID]
  [--machine M] [--level L] [--tier T] [--hours H | --from ISO --to ISO] [--limit N]
  [--offset N] [--total] [--json]`: newest-first slice of the unified event stream
  (`GET /api/events`). Without a window the gateway reads the last 24 hours.
- `meiva events count --by event_name|machine|level|agent_id|category` plus the same filters:
  the matching events counted per value of one dimension, largest group first
  (`GET /api/events/counts`; the counting runs in SQL under the events statement timeout, and a
  window with more than 1000 distinct values is refused).

## Harness attachment

- `meiva harness exec [--file PATH] [--handle H] [--agent-id N] [--session-id S]`: run one
  Python file (or stdin as `-`) inside a provider-harness attachment, so the script's
  `meiva.*` calls carry the agent identity the bridge registered. The handle defaults to
  `$MEIVA_HARNESS_HANDLE`, which is how the bridge hands it to the child; nothing is
  written to the workspace. The process refuses to attach a second identity, so a script
  that calls `attach()` itself fails instead of nesting
  ([meiva.attach](../../../meiva/self/harness/docs/harness-attach.md)).

## Local and package operations

- [`meiva config get/set/unset`](../config.md)
- `meiva pty freeze/status/resume`: host-wide PTY allocation gate.
- `meiva logs rotate`: top-level copytruncate rotation for service stdout and
  native backend logs at 64 MiB or a UTC-day boundary; zero-byte files are not
  rotated.
- `meiva logs retention`: local, non-recursive managed-log cleanup; legacy global
  14-day fallback or explicit family tiers across service and native archives;
  open handles are excluded.
- `meiva backup walg check|run|drill|restore|status`: the WAL-G physical backup. `check` is the
  pre-flight: it proves the pinned binary, the configuration and key, and a
  put/list/get/delete round trip under the bucket prefix (exit 1 on the first failing
  step). `run` is the daily tick the OS job runs: backup, verify the archived WAL chain,
  apply guarded retention (exit 1 only when a step failed; a skip or a concurrent run
  exits 0). `drill` runs the weekly recovery drill now (exit 0 only if it passed).
  `restore --dir DIR --user U [--backup NAME] [--time T | --lsn L]` recovers a backup into an
  empty directory with a scratch Postgres and never touches the home's data directory.
  `status` prints the configuration, key fingerprint, archiver facts and the
  last tick and drill and never fails
  ([WAL-G](../../../services/backup/walg/docs/walg.md),
  [restore and drill](../../../services/backup/walg/docs/walg-restore.md)).
- `meiva mcp ...`: isolated environments at `$MEIVA_HOME/mcps/`
  ([MCP](../../../meiva/mcps/docs/mcps.md)).
- `meiva plugins ...`
- `meiva skill install/update/upgrade/enable/disable/register/scan/trust`
- `meiva schedules ls/get/create/update/delete/provision/start/stop/restart/logs/runs`:
  provision creates the built-in schedules from `schedules/manifest.json` and
  also runs at every schedule-manager start;
  it creates missing rows and resyncs a built-in row whose script differs from the template.
  `meiva schedules verify` reads each stored script through the gateway (which must be
  reachable; `--rows-file` and `--check-file` read none, and `--rows-file` overlays
  `schedules/manifest.json` templates for built-in rows that `up` resyncs) and checks its imports, the names
  it reads and its calls into repo code in this checkout.
- `meiva trace ship`
- `meiva lgtm on/off/status`: observability-stack toggle on this host, represented
  by its marker and native lifecycle.

## Public client wire proof

[Public client wire contracts](public-client-wire.md)
own the client test layout, actual gateway profile and HTTP listener, lexical
resource lifetime, parser/provenance errors and retained native verification.
