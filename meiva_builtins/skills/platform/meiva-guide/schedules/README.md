# meiva-guide.schedules — Declarative Scheduled Work

The [skill](SKILL.md) translates timing requirements into single-round Python
payloads or agent wake messages using `meiva.schedules` and `meiva schedules`.
The manager owns firing and recovery; scripts perform one bounded round and exit.

Choose `at` for one instant, `cron` for a wall-clock cadence, and `every` for
periodic condition checks. Owner-local workspaces retain payloads, latest logs
and ordinary local state files between fires. Agent-owned work cancels on owner
termination by default; `on_owner_terminate="keep"` explicitly decouples it.

The SDK and CLI own their signatures. The skill owns trigger selection,
resumable round design, lifecycle choice, evidence and stopping conditions.
For identity and placement details, see the repository's
`services/wake/schedule_manager/docs/schedule-identity.md`.
