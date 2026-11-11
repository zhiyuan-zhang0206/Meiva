# PG-Backup — Daily Local Postgres Backup

## What is it
A gateway-owned `pg-backup` scheduler daemon, declared by `services/supervision/roster/spec.py` as a
first-class `ServiceSpec`. It calls the dump logic after `services.backup_hour` (default 3 AM
**cluster** time — `MEIVA_TIMEZONE`, cluster-pinned) when the current cluster day
has no dump; a host down at 03:00 catches up at the next scheduler wake. Its
health reports last-success age, so the watchdog only supervises the scheduler
and is never delayed by a dump. The day boundary is the cluster's, not the
host's: reading a host timezone can make a current dump appear to be future.

## Core Responsibilities
- **Daily dump**: `pg_dump --format=custom --compress=zstd:3` full database (the custom archive compresses in-dump, including the LangGraph checkpoint tables that hold conversation history), then encrypts it to `$MEIVA_HOME/backups/db/<dbname>-YYYYMMDDTHHMMSSZ.dump.enc` — under path-only cluster identity, `$MEIVA_HOME` itself already uniquely locates the cluster, so dump directories no longer need a cluster token.
- **UTC-stamped names**: the stamp is UTC with an explicit `Z`, so prune's oldest-first ordering is a total order over real instants. The grammar is `services/backup/artifact/names.py` (`<db>-<stamp>.dump.enc`, with a bare `.dump` also inside it); a name outside it, such as a pre-cutover wall-clock stamp, is never counted or pruned.
- **One passphrase resolution** (`services/backup/artifact/passphrase.py`): writers and every restore path use the pinned `$MEIVA_HOME/backups/logical-backup.passphrase`, minted at birth and independent of the cluster secret; it is backup-critical — see [Logical-backup passphrase](passphrase.md) for its lifecycle and escrow.
- **Atomic write, no plaintext at rest**: writes private plaintext and encrypted `.partial` files first, then publishes the encrypted artifact only after both commands succeed — half-finished dumps won't be mistaken for complete backups by due/prune logic or manual recovery. A failed or interrupted run removes its partials once its tool is killed and reaped. Every run, the scheduled worker's staged one included, first sweeps `backups/db/` under the backup lock: a dead run's partials, key files and cross-filesystem publish copies that no process holds open are removed (one still held by an orphaned writer is reported and waits), so a killed run's plaintext never outlives the next nightly dump.
- **Scheduled worker**: the dump uses a fixed worker and unique private staging
  (`services/backup/scheduler/worker.py`, see [Shutdown](shutdown.md)).
  The worker encrypts the dump; its controller validates zero exit,
  artifact name and SHA-256 before exclusive publication (a verified private
  copy across filesystems). Pruning respects the existing backup lock.
  Failure/cancellation sanitizes staging and reports cleanup errors with the
  private path. No durable custody, quarantine or blocked-kind retirement is
  required; see [Scheduled workers](../../scheduler/docs/scheduled-workers.md).
- **Keep a week**: only manages dumps matching the managed-name grammar (`services/backup/artifact/names.py`); manually placed dumps in the same directory are never touched; deletes those beyond the newest `services.backup_keep` (default 7).
- **Timeout guard**: `_DUMP_TIMEOUT_S=60min` bounds pg_dump inside the scheduler; a stalled dump cannot freeze watchdog supervision.
- **Dump authority** (`dump_source`): a locally owned plane dumps as the administrator acting as the schema owner over the home's owner-only socket (`base.cluster.dataplane.pg_admin`), custody-checked first and password-free, so no write-generation login is needed and a rollout revoking one cannot kill a dump; a remote-managed plane dumps through its provider URL (password in the child environment only). The restore drill recreates the capability groups `meiva_gateway`/`meiva_runner` the dump's grants name.

- **Configuration ownership**: the roots (scheduler daemon, worker, restore drill) read configuration once and pass values down: `run_backup(now, tree=, db_url=, keep=)` takes the `BackupTree`, the dial and the retention. Due-ness, names, argv and retention are pure (`schedule.py`, `dump_plan.py`, `dump_lines.py`).

## Key Dependencies
- [Meiva Root — one application service owner](../../../supervision/meiva_root/docs/meiva_root.md) — probes and restarts the scheduler without executing the dump
- [Database Layer](../../../../agent/db/docs/db.md) — the dump target is the cluster's Postgres database

## Entry Points
- `services/backup/scheduler/daemon.py` — scheduled due-check, retry, and health state
- `services/backup/dump.py` — `run_backup()` dumps, encrypts and prunes in-process; `run_backup(staging=...)` writes the scheduler worker's artifact into its private controls
- `services/backup/restore_drill.py` (`python -m services.backup.restore_drill [ARTIFACT]`) — decrypts a managed database backup, restores it into throwaway Postgres, and verifies schema, table counts, and a readable checkpoint conversation without touching the live database; scratch files and the throwaway cluster are removed afterwards
- `services/backup/scheduler/worker.py` — the dump and logical restore-drill operation kinds, their sanitizers, and `commit_scheduled_backup()`
- `services/backup/artifact/intermediates.py` — `sweep_closed_partials()`, the closed-intermediate sweep every backup run applies to the backup directory
- `services/backup/artifact/names.py` — the managed-dump name grammar
- The daily dump is this package's recovery point and the only Postgres backup: it stays on this machine, and there is no off-site copy or point-in-time recovery (see `docs/decisions/data/database/2026-11-12-no-offsite-postgres-backup.md`).

## Notes
- Gateway capability only; `meiva start --disable-service pg-backup` prevents its scheduler session from starting and watchdog revival respects the same marker.
- Protects against bad migrations / accidental deletion / DB corruption on this machine only: the dump is never copied elsewhere by Meiva. Keeping a copy off the host is an operator's own decision.
- Restore: follow `.agents/skills/meiva-guide/operations/references/db-restore.md` to decrypt before `pg_restore --clean --if-exists`.
[Shutdown ownership](shutdown.md)
[Static recovery-contract test boundary](recovery-contract.md)
