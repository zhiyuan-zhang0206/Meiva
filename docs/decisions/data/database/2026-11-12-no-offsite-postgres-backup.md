# Postgres is not backed up off the machine

## Context

Two mechanisms sent Postgres data to an object store. WAL-G shipped every WAL
segment and a daily base backup to an Aliyun OSS prefix and drove a weekly recovery
drill ([WAL-G](../backup/2026-10-02-walg-physical-backup.md)). The daily logical dump
also had a best-effort leg that published each encrypted artifact to OSS under
`meiva-logical/`. Together they carried a configuration domain with credentials, a
pinned downloaded binary, a daily OS job, postmaster launch arguments, a health probe
and a restore and drill command surface. The off-site copy of the dump was a planned
item (`future/infra/data/pg-backup.md`) before it was built.

## Decision

Postgres is not backed up off the machine. The daily encrypted local `pg_dump`
(`$MEIVA_HOME/backups/db/`, the newest `services.backup_keep` kept, the pre-update
snapshots and the weekly isolated restore drill) is the only backup. Everything that
sent database content elsewhere is deleted: WAL-G and its configuration key,
binary, job, probe, restore and drill commands, and the dump's OSS publish leg with its
three configuration keys and the `oss2` dependency.

A deployed host is converged by code, not by hand: Postgres is launched without
archive arguments from now on, and converge removes the daily job, its files and
the installed binary (the binary waits while a still-running postmaster archives,
because that postmaster keeps its launch arguments until it is launched again).

## Alternatives rejected

- **Keep WAL-G, drop only the dump's off-site leg**, or the reverse. The ruling was that all
  off-site Postgres logic goes, so neither half was kept: each carries its own credentials and
  failure surface, and the point-in-time recovery WAL-G gave is the capability that is
  given up.
- **Leave dead configuration keys tolerated for a while.** The registry ignores a key no
  field declares, so a stale `.env` line is inert; keeping fields alive to avoid that would
  keep the code the decision removes.

## Consequences

- Losing the host's disk loses every backup that lives on it. Keeping a copy elsewhere
  is now an operator's own act and outside Meiva.
- There is no point-in-time recovery: the recovery point is the last daily dump, up to
  a day old, and a bad migration is fixed forward.
- Objects already in the bucket are not touched by any code; deleting them is a manual
  decision.
- The WAL-G records in `docs/decisions/data/backup/` stay as history and point here.
