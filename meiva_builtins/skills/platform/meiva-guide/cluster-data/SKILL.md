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
| What an agent has been doing | `meiva agents timeline <agent_id> [--limit N] [--before ITEM_ID]` |
| Tasks: read, create, update, log progress | `meiva.tasks` (`get`, `list`, `create`, `update`, `log`; `meiva.help(meiva.tasks)`) |
| Schedules and their runs | `meiva schedules ls`, `meiva schedules get <name>`, `meiva schedules runs <name> [--limit N]`, `meiva schedules logs <name> [--lines N]` |
| Open questions waiting for the user | `meiva notices list [--agent ID]` |
| Files of an agent, delivering something to the user | `meiva.files`, `meiva.ui` |
| Service health | `meiva status` |

`meiva <command> --help` owns the exact flags. When the table has a row for your
question, stop there.

## 2. Query the database read-only

Audit events, telemetry events, the cross-agent inbox, cost totals and the
scheduler's bookkeeping have no entry. Your process environment carries
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
| `agent_notices`, `machines`, `machine_status_snapshot` | Open questions for the user; which machines are in the cluster and how they look | see `db/schema.sql` |

`event_name` values come from the emitting code. List the distinct names in a short
window first, then filter on the one you found:

```sql
SELECT event_name, count(*)
FROM audit_events
WHERE ts > now() - interval '1 day'
GROUP BY 1 ORDER BY 2 DESC
LIMIT 50;
```

### Example queries

Recent errors one agent's telemetry recorded:

```sql
SELECT ts, event_name, source, attributes
FROM telemetry_events
WHERE agent_id = 123
  AND ts > now() - interval '6 hours'
  AND level IN ('error', 'critical')
ORDER BY ts DESC
LIMIT 100;
```

Messages an agent has not consumed yet:

```sql
SELECT id, kind, source, created_at
FROM inbound_messages
WHERE agent_id = 123 AND status = 'pending'
ORDER BY id
LIMIT 50;
```

Open tasks of one owner:

```sql
SELECT id, priority, title, updated_at
FROM agent_tasks
WHERE owner = 123 AND status = 'in_progress'
ORDER BY priority, updated_at DESC
LIMIT 50;
```

Schedules that are failing:

```sql
SELECT name, status, last_error, machine, updated_at
FROM schedules
WHERE enabled AND status = 'error'
ORDER BY updated_at DESC
LIMIT 50;
```

Spend per agent and model over the last week:

```sql
SELECT agent_id, model, sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out,
       sum(cost_usd) AS cost_usd
FROM agent_model_tokens_daily
WHERE day > current_date - 7
GROUP BY agent_id, model
ORDER BY cost_usd DESC
LIMIT 50;
```
