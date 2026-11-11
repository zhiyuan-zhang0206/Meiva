# CLI Operator Surfaces

## Agent and context commands

- `meiva agents ls/get/timeline/inbox/send/interrupt/compact/restart/resurrect/terminate/kill/transfer`: `ls` renders the
  authenticated agent summary projection as stable `id / status / machine /
  label` columns. It does not expose runner-local workspace paths. `get ID [--json]`
  prints one agent's status, machine, harness and activity clocks (`GET /api/agents/{id}`).
  `inbox ID [--status pending|claimed|done] [--kind K] [--source S] [--hours H] [--limit N]
  [--full] [--json]` reads the agent's inbound queue, newest first (`GET
  /api/agents/{id}/inbound`): every status and kind, not only what is still queued; content is a
  200-character preview unless `--full`; one agent's rows only, never a content search across
  agents. `timeline` reads standing context and recent conversation items. `send` delivers a chat
  message (auto-resurrecting a terminated target) and requires explicit
  `--source` provenance (`user`, `external_agent:<subject>[:<instance>]`,
  `shell:N`, `watcher:N`, or `schedule:N`;
  [external caller provenance](../../../base/agents/messages/docs/external_caller.md)).
  `restart`, `resurrect`, `terminate`, `kill` and `transfer --to MACHINE` require
  `--message TEXT | --no-message` (blank `--message` exits 2 at parse); `interrupt` requires
  `--message TEXT` (and `--source`) and `compact` takes neither. `transfer` relocates an `idling` or `terminated`
  native agent and its workspace across machines and refuses running agents with
  409 ([agent transfer](../../../gateway/agents/docs/agent-transfer.md)).
- `meiva questions list/answer/skip`: the gateway settles all of an agent's open questions in one
  submission, so `answer --agent N` takes one `--answer ID=TEXT` (a numbered reply) or `--skip ID`
  for every open question of that agent and posts them with the open set it just read; a partial
  cover exits 2 without posting, and a changed set (409) prints the latest ids and exits 1 with
  nothing settled. `skip --agent N` skips every open question of that agent after confirmation
  (`--force` skips confirmation); `skip --stale` targets only terminated agents. A submission is one
  keyed attempt (`principal-v1`) with a fresh key per invocation.
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
- `meiva metrics agents [--days N] [--since-compact] [--json]`: one row per agent over the last
  N days (cost, calls, tokens, cache hit, turn and exec outcomes), costliest first
  (`GET /api/metrics/agents`; the aggregation runs in the gateway's SQL).
- `meiva stats [--hours 0|1|6|24|72|168|720] [--json]`: the fleet's tokens, cost (API and
  subscription), warning and error totals over a window, then the plugin status cards such as
  provider balances (`GET /api/stats/dashboard`; `0` is the last five minutes).
- `meiva tasks ls [--owner ID] [--status in_progress|done|cancelled] [--window 24h|7d|30d|all]
  [--json]`: the metadata of the matching tasks, newest created first (`GET /api/tasks?fields=
  summary`; `owner` and `status` narrow in SQL, and a trailing `*` on an id marks an
  out-of-window ancestor delivered for tree shape). The registry holds every task ever created,
  so `ls` exits 2 unless at least one filter is named; `--window all` asks for everything.
  `meiva tasks get ID [--json]` prints one task in full: owner, priority, reminder state
  (interval, count, last, paused until), description and results (`GET /api/tasks/{id}`).

## Harness attachment

- `meiva harness exec [--file PATH] [--handle H] [--agent-id N] [--session-id S]`: run one
  Python file (or stdin as `-`) inside a provider-harness attachment, so the script's
  `meiva.*` calls carry the agent identity the bridge registered. The handle defaults to
  `$MEIVA_HARNESS_HANDLE`, which is how the bridge hands it to the child; nothing is
  written to the workspace. The process holds one identity, so the script's first identity read
  reuses this attachment instead of nesting a second
  ([harness attach](../../../meiva/self/harness/docs/harness-attach.md)).

## Local and package operations

- [`meiva config get/set/unset`](../config.md)
- `meiva pty freeze/status/resume`: host-wide PTY allocation gate.
- `meiva logs rotate`: top-level copytruncate rotation for service stdout and
  native backend logs at 64 MiB or a UTC-day boundary; zero-byte files are not
  rotated.
- `meiva logs retention`: local, non-recursive managed-log cleanup; legacy global
  14-day fallback or explicit family tiers across service and native archives;
  open handles are excluded.
- `meiva mcp ...`: isolated environments at `$MEIVA_HOME/mcps/`
  ([MCP](../../../meiva/mcps/docs/mcps.md)).
- `meiva plugins ...`
- `meiva skill install/update/upgrade/enable/disable/register/scan`
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
