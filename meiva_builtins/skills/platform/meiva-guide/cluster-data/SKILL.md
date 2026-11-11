---
name: cluster-data
description: "Reads Meiva cluster data such as audit events, telemetry, inboxes, tasks, schedules and costs. Use when an agent needs facts about the cluster that no meiva.* call returns."
---

# Reading cluster data

The cluster keeps its records in Postgres. Agents usually do not need SQL: most of
what they want has a supported entry that returns a typed value, applies filters and
limits, and keeps working when the schema changes. Use an entry first and fall back
to a read-only query only when none fits.

## 1. Use a supported entry first

| You want | Use |
|---|---|
| Your own identity | `meiva.self.AGENT_ID` |
| Which agents exist, their status and machine | `meiva.agents` (`meiva.help(meiva.agents)`), or `meiva agents ls [--scope live\|terminated\|all] [--query Q] [--limit N]` |
| One agent's status, machine, harness, last activity | `meiva agents get <agent_id> [--json]` |
| Whether a message reached an agent and whether it was picked up (pending, claimed, done; chat, system notes, heartbeats) | `meiva agents inbox <agent_id> [--status pending\|claimed\|done] [--kind K] [--source S] [--hours H] [--limit N] [--full]` (newest first; content is a 200-character preview unless `--full`) |
| What an agent has been doing | `meiva agents timeline <agent_id> [--limit N] [--before ITEM_ID]` |
| Tasks: read, create, update, log progress | `meiva.tasks` (`get`, `list`, `create`, `update`, `log`; `meiva.help(meiva.tasks)`) |
| Tasks from a shell: which tasks an agent owns, which are open or stale, one task in full with its reminder state | `meiva tasks ls [--owner ID] [--status in_progress\|done\|cancelled] [--window 24h\|7d\|30d\|all]` (at least one filter), `meiva tasks get <id>`; `--json` for scripts |
| Audit, telemetry and log events: what happened, when, on which machine | `meiva events ls [--agent ID] [--name EVENT] [--category audit\|telemetry\|log] [--level L] [--machine M] [--hours H] [--limit N]` (newest first; default window: the last 24 hours; `--json` for scripts) |
| How many events of each kind, machine, level or agent | `meiva events count --by event_name\|machine\|level\|agent_id\|category` with the same filters |
| What each agent costs (calls, tokens, cost, turn outcomes) | `meiva metrics agents [--days N] [--json]` (costliest first) |
| Fleet cost and tokens over a window, warning and error totals, balances and quotas that plugins report | `meiva stats [--hours 0\|1\|6\|24\|72\|168\|720] [--json]` (the plugin cards, such as provider balances, are listed under the totals) |
| Schedules and their runs | `meiva schedules ls`, `meiva schedules get <name>`, `meiva schedules runs <name> [--limit N]`, `meiva schedules logs <name> [--lines N]` |
| Open questions waiting for the user | `meiva questions list [--agent ID]` |
| Files of an agent, delivering something to the user | `meiva.files`, `meiva.ui` |
| Service health | `meiva status` |

`meiva <command> --help` owns the exact flags. When the table has a row for your
question, stop there.

## 2. Query the database read-only

Searching message text across agents, cost split per model and day, and the scheduler's bookkeeping have no entry. Your process environment carries
`MEIVA_DB_URL`, the cluster's database address, so a query is one call away:

```python
import os
import psycopg

with psycopg.connect(os.environ["MEIVA_DB_URL"], autocommit=True) as conn:
    conn.read_only = True
    rows = conn.execute(
        """
        SELECT ts, event_name, agent_id, level
        FROM audit_events
        WHERE ts > now() - interval '1 hour' AND level IN ('error', 'critical')
        ORDER BY ts DESC
        LIMIT 50
        """
    ).fetchall()
```

From a shell, `psql "$MEIVA_DB_URL" -c "SELECT ..."` does the same. The connection is
the cluster's runner login, so it sees every agent's rows, not only yours.

How to use it:

- **Read only.** The SDK and the services own every write and the invariants behind
  it (receipts, status transitions, append-only audit). A direct `INSERT`,
  `UPDATE` or `DELETE` skips those invariants, so change state through the SDK or the
  CLI. `conn.read_only = True` makes the server refuse a write.
- **Keep the address private.** Do not print `MEIVA_DB_URL`, a password or a token
  into output, a message or a file.
- **Bound every query.** Give it a time window and a `LIMIT`; the event tables hold
  the cluster's whole history.
- **Do not browse `information_schema` to find tables.** The next section lists the
  ones worth knowing; the full definitions are in `db/schema.sql` of the source
  checkout (`$MEIVA_HOME/source`).

## 3. Tables worth knowing

| Table | Holds | Useful columns |
|---|---|---|
| `audit_events` | Append-only audit trail | `ts`, `event_name`, `agent_id`, `target_agent_id`, `level`, `machine`, `trace_id`, `attributes` (JSONB) |
| `telemetry_events` | Telemetry and log events, partitioned by month on `ts` | same shape plus `category` (`telemetry` or `log`) and `cluster`; always filter on `ts` so only the months you need are read |
| `inbound_messages` | Every agent's inbox: chat, system notes, lifecycle commands | `agent_id`, `kind`, `source`, `status` (`pending`, `claimed`, `done`), `created_at`, `claimed_at` |
| `agents`, `agents_meta` | Agent identity and live status | `agents.label`; `agents_meta.status` (`running`, `idling`, `terminated`), `machine`, `harness`, `last_active_at` |
| `agent_tasks` | The task board | `parent_id`, `title`, `status`, `priority`, `owner`, `created_at`, `updated_at` |
| `schedules`, `schedule_runs` | Persistent scheduled work and its run history | `schedules.name`, `enabled`, `status`, `last_error`, `machine`; `schedule_runs.schedule_id`, `ran_at`, `ok`, `note` |
| `agent_model_tokens_daily` | Per-agent, per-model token and cost totals by day | `agent_id`, `day`, `model`, `llm_calls`, `tokens_in`, `tokens_out`, `cost_usd` |
| `agent_questions`, `machines`, `machine_status_snapshot` | Open questions for the user; which machines are in the cluster and how they look | see `db/schema.sql` |

`event_name` values come from the emitting code. To find the one you want, list the
names that occurred in a short window with `meiva events count --by event_name --hours 24`,
then filter on it with `meiva events ls --name <event_name>`.

### Example queries

Schedules that are failing:

```sql
SELECT name, status, last_error, machine, updated_at
FROM schedules
WHERE enabled AND status = 'error'
ORDER BY updated_at DESC
LIMIT 50;
```

Spend per agent and model over the last week (`meiva metrics agents --days 7` gives the per-agent totals without the model split):

```sql
SELECT agent_id, model, sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out,
       sum(cost_usd) AS cost_usd
FROM agent_model_tokens_daily
WHERE day > current_date - 7
GROUP BY agent_id, model
ORDER BY cost_usd DESC
LIMIT 50;
```
