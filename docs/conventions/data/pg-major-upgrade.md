# PostgreSQL major-version upgrade (17 → 18)

How to move a cluster's data plane from the pinned Postgres 17 to 18. Two
paths: **A. `pg_upgrade`** — the data directory is upgraded with both majors'
binaries present (no logical rewrite); **B. rebuild through a logical
move** — dump the frozen 17 cluster, load into a
freshly provisioned 18 cluster; the fallback when path A is not viable (missing
extension builds, collation/OS changes, no room for a second copy).

**Status: draft.** The repository pins Postgres 17 in several places; the
upgrade becomes executable only after the approved "expand" change lands. The
places that pin 17 (each needs its 18 counterpart, and the vendored tree is
expanded — a new tree beside the old, never an in-place swap):

- `base/cluster/dataplane/runtime_binaries.py` — `PG_VERSION`, the zonky
  artifact pins, and the pgvector artifact pins;
- `base/cluster/dataplane/pg_runtime.py` — the installed-runtime acceptance
  (`_require_pg17`) must admit 18 while 17 still runs;
- `base/cluster/dataplane/pg_tools.py` — `PG_BIN_LINUX`;
- `base/host/system/backend.py` — the Homebrew keg name
  (`brew_prefix("postgresql@17")`);
- `base/host/tool_pins.py` — the approved pin list; and
  `base/host/system/macos_firewall.py` — the postmaster path allowlist;
- `scripts/provision/database.sh` and `ops/ci_autoscale/cloud-init.yml` — the
  apt/brew package lines;
- `AGENTS.md` — the approved-stable line, after the user approves the bump.

Until that change ships, this document is the target procedure and the
acceptance list; a rehearsal on a scratch home is possible earlier with the
platform packages alone.

## Preconditions (all paths)

- The bump is approved (approved-stable) and the expand change is deployed on
  the target.
- Freeze the home: `meiva stop --yes --close-shell-sessions --stop-browser --stop-extras --keep-no-services --stop-infra --mode smooth --timeout 300 --no-message` (a full local stop; agents drain first).
  PgBouncer and Redis are untouched by a Postgres major; no change needed.
- A fresh recovery point: the newest daily logical dump verified by the restore
  drill (`db-restore.md`). Keep the old major's binaries and the old data
  directory until the bake period ends — they are the rollback.
- Extensions in use are known (`SELECT extname, extversion FROM pg_extension;`)
  and each has files for the new major on this host (pgvector is the one Meiva
  provisions; the expand change re-pins it).
- Disk: path A with `--copy` needs room for a second copy of the data
  (`--link` needs almost none but forfeits a plain rollback); path B needs the
  dump plus a fresh 18 data directory.

## Path A — pg_upgrade

1. `meiva stop --yes --close-shell-sessions --stop-browser --stop-extras --keep-no-services --stop-infra --mode smooth --timeout 300 --no-message`; confirm a clean stop.
2. Install the 18 binaries for this host's resolution mode: the vendored tree
   when the expand change ships it, else the platform packages
   (apt: `postgresql-18` + `postgresql-18-pgvector`; macOS: the
   `postgresql@18` keg — update the pin list). Do not remove the 17 binaries.
3. `initdb` a new data directory beside the live one with the same recipe the
   home's clusters use (same OS user, `UTF8`/`C`; see
   `cli/commands/data_plane/cluster_instance.py`). `pg_upgrade` copies the old
   cluster's configuration files over, and nothing Meiva writes lives in the
   data directory (its settings are launch arguments), so there is nothing
   to hand-merge.
4. Run `pg_upgrade --check` with both binaries and both data directories named;
   resolve everything it reports before touching data.
5. Upgrade: `pg_upgrade --old-bindir <17 bin> --new-bindir <18 bin>
   --old-datadir <$MEIVA_HOME/pg> --new-datadir <$MEIVA_HOME/pg-new>` as the OS
   owner. Decide `--copy` vs `--link` consciously: `--link` is fast but leaves
   the old directory unusable as-is.
6. Swap directories on the same filesystem: keep the old one
   (`mv pg pg-17-old; mv pg-new pg`).
7. `meiva start --all-services` (converge re-validates the toolchain), then wait
   for the next daily dump, or trigger one, so the first recovery point of the
   18 cluster exists.
8. Post-upgrade hygiene: `vacuumdb --all --analyze-in-stages` (optimizer
   statistics are not carried over), then the acceptance list below.
9. After the bake period, retire `pg-17-old` and the 17 binaries.

## Path B — rebuild through a logical move

Use when path A is blocked. The home is frozen in both paths, so a dump taken
after the stop covers every committed transaction.

1. `meiva stop --yes --close-shell-sessions --stop-browser --stop-extras --keep-no-services --stop-infra --mode smooth --timeout 300 --no-message`, then start only the 17
   Postgres for the dump (the writers stay stopped).
2. Dump it through the private owner socket the scheduled dump uses
   (`services.backup.dump_source()`):
   `pg_dump --format=custom --file=<scratch>/meiva_main.dump <owner conninfo>`; confirm it
   with `pg_restore --list`, then stop that Postgres.
3. Move the live directory aside (`mv $MEIVA_HOME/pg $MEIVA_HOME/pg-17-old`) and
   bring up the 18 cluster through Meiva's own provisioning: `meiva start --all-services` (the
   missing data directory is `initdb`ed by the same code path as a fresh
   install; roles, the write generation, the schema and its migrations are the
   repository's, not hand-built). Stop the writers again before loading.
4. Load the dump **data-only**, so the schema stays the migration-owned shape:
   `pg_restore --data-only --dbname=<socket URL> <scratch>/meiva_main.dump`
   (add `--disable-triggers` only if foreign keys force it, as the admin role).
   Sequence values are data; verify them during acceptance and `setval`
   where needed.
5. `meiva start --all-services` fully; wait for the next daily dump, or trigger
   one, so the 18 cluster has its first recovery point.
6. Verify against the acceptance list — including a per-table count comparison
   with the scratch copy (still on disk) for the key tables.

The points that need settling together with the expand change: how the
data-only load interacts with the authority-provisioned roles and grants, and
whether sequence values need explicit `setval` per table (verify both in the
rehearsal).

## Acceptance (both paths)

- `meiva status` clean; no pending migrations; the health probe green.
- Row counts against the source for the key tables (`agents`, `checkpoints`,
  `checkpoint_blobs`, `checkpoint_writes`, `tasks`, `inbound_messages`) and a
  conversation read back through the checkpoint reader — the same check the
  logical restore drill runs.
- The first daily dump of the new major exists and the weekly restore drill
  passes on it before the old binaries and directory are retired.
- Agents boot and read conversations; one conversation read-back is the spot
  check.

## Rollback

Both paths keep the old directory and binaries. Rolling back means: stop, move
directories back, start on the old major. Anything written while the new major ran
exists only in the new cluster, so a rollback started after that point loses it.
A rollback after the new major has accepted writes has not been rehearsed — treat
it as an emergency-only path, and prefer restoring forward.

## Rehearsal requirements before production

- A scratch home (new `$MEIVA_HOME`, no secrets shared) exercised through path A
  and path B, with the acceptance list run against it.
- The measured times and every deviation recorded — this document gains the
  rehearsal record; do not run the fleet upgrade before that exists.
