# meiva-being-a-long-running-agent skill — Long-Running Process Discipline

## What is it
A set of **behavioral disciplines** that keep you effective beyond the first few turns (`$MEIVA_HOME/skills/meiva-being-a-long-running-agent/`). Load when you're responsible for a long task or an ongoing domain (watching services, monitoring queues, coordinating peers, driving multi-step pipelines); don't use it for one-off work. It exists because long-running agents have a class of recurring failure modes (treating "replying" as "done", getting stuck silently, using tight loops to wait for events, losing state on compaction); this skill nails those countermeasures as disciplines.

## Ownership and procedures

The core system prompt's **Efficient long-running operation** section carries
lifecycle and cost principles even without fleet or this skill being loaded.
This skill owns the operational procedures: record awaited events and monitor
references, select event delivery or background mechanical checks, set response
requirements and heartbeat pauses, persist progress, and restore missing monitors
without duplicating survivors. Ordinary metric changes do not require model
wakes; useful wakes carry a trigger, evidence, and a durable record pointer.

The fleet plugin owns peer communication. This skill refers to that contract for
milestones, commitments, blockers, and handoffs rather than prescribing a second
cadence. The human interruption section remains separate.

The entrypoint retains lifecycle, scope, and recovery decisions. Waiting
procedures load from `references/waiting.md`; state destinations and the task-file
example load from `references/durable-state.md` when persistence or recovery
requires them. A finite task still terminates when complete.

## Key Dependencies
- [Skills index](../../README.md) — full skills catalog
- [Watcher SDK](../../../../meiva/watcher/docs/watcher.md) — the "use watchers" primitive
- [meiva.self](../../../../meiva/self/README.md) — `pause_heartbeat` / `compact` / identity (AGENT_ID)

## Usage reminders

Usage budget reminders compare recorded USD cost with `--usd-limit`. Select
explicit agent IDs, a window or lifetime, and immutable spawn/fork lineage.
Token counts are usage observations, never budget thresholds. Reminders notify
named peers once and leave graceful disposition to the receiving agents.
`scripts/usage_budget.py` decides over values (the reminder a report earns and what a
polling round does next); `scripts/agent_usage.py` reads the database and sends it.
