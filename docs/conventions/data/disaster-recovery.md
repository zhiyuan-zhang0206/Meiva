# Disaster recovery

This matrix states the current recovery promise. A backup is not recovery
proof: the named exercise must complete successfully before its path is treated
as usable. Never restore an artifact into a live database.

| Asset | Backup or recovery media | RPO / RTO declaration | Exercise frequency |
| --- | --- | --- | --- |
| Data: Postgres, including checkpoints and conversation history | Encrypted daily local `pg_dump` artifacts in `$MEIVA_HOME/backups/db/`. They are the only Postgres backup: nothing is copied off the machine, so losing the host's disk loses them too ([decision](../../decisions/data/database/2026-11-12-no-offsite-postgres-backup.md)). | RPO: at most one daily backup window. RTO: one maintenance window; the production-sized isolated restore measured 136.6 seconds on 2026-08-27, not a guaranteed duration. | Weekly isolated local restore after the Sunday 03:00 cluster-time dump. |
| Configuration: cluster `.env`, identity, and host wiring | Surviving gateway or runner `.env` secret escrow plus source-controlled installation inputs | No automated configuration-backup RPO/RTO is declared. Rebuilds require a surviving secret and an operator-led install/enrolment procedure. | Verify secret escrow during every recovery exercise and cluster-change review. |
| Code | The merged Git history and the checkout installed for the cluster | RPO: merged commits. RTO: checkout, dependency sync, and the normal cluster-update path; no wall-clock SLO is declared. | Every cluster update proves the deployed revision can start. |
| Agent state | Durable conversations and checkpoint state are in the Postgres artifacts above; live processes and in-flight turns are ephemeral | Durable state inherits the database RPO/RTO. Process state has no backup and resumes only from a completed checkpoint after restart. | The weekly logical restore verifies a checkpoint-reader sample. |

## Automated proofs and alerts

The gateway-owned `pg-backup` scheduler runs the weekly logical drill only after
a successful daily dump. It writes an owner-only success marker, so a previous
success does not suppress a later week's proof.

A failed drill emits the typed `recovery_drill_failed` telemetry event with the
drill name. Grafana alerts immediately on that event's one-hour window. A failed
daily logical dump emits `backup_dump_failed` and is not attempted again before
the next backup hour; its Grafana rule keeps firing for 24 hours. Every
scheduled backup worker uses private staging and bounded stop, without durable
custody or blocked-kind retirement. Failures remain failures in scheduler
health/logs and never advance backup/drill success markers. If cleanup fails,
inspect the reported private scratch path; no process-family census claims all
orphans disappeared (`docs/conventions/runbook.md`).

## Restore procedure

For the isolated logical restore commands and acceptance checks, follow
[`db-restore.md`](../../../meiva_builtins/skills/platform/meiva-guide/operations/references/db-restore.md).
There is no point-in-time recovery and no migration rollback (no down migrations): a bad
migration is fixed forward with a new one, or recovered from the newest daily dump; the
retired mutable-checkout updater drill is not a supported recovery entrypoint.
