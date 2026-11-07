# Database migrations

The model itself — baseline vs post-baseline deltas, the applied **set** keyed by
migration name, the immutability of merged migrations, expand-contract for lossy
operations, and the bidirectional `assert_schema_current` check — is in
[migration model](../../base/deploy/schema/docs/migrations.meiva.okf.md). What follows is how you operate it.

## Applying migrations

There is no standalone migrate command. Pending migrations are applied as a step of
`meiva start` (early in boot, after Postgres is up and before the schema-current assertion), so
any restart that crosses a schema change catches the DB up automatically.

Production upgrades run `meiva update run` (attended, with
user approval; see the runbook's "Updating a networked cluster in source mode"):
the gateway's cold `meiva start` in the `up` half applies the pending migrations.
Separate `down` and `up` commands allow intermediate manual release work.
For a manual catch-up, run `meiva start` directly, which applies pending
migrations on the way up.

## Checkpoint dependency upgrades

Fresh install alone invokes `PostgresSaver.setup()`; later starts only read and
verify the complete checkpoint migration set after applying Meiva migrations.
`base/cluster/provision.py` freezes the adopted upstream baseline at version 9.
An upstream dependency that adds version N must carry that DDL in an Meiva
timestamp migration and declare it in `CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS`.
Do not advance the frozen baseline instead. The migration must remain idempotent
when fresh install already created both the effects and the upstream migration
row, while recording its Meiva migration name. Real-Postgres verification covers
existing N-1 upgrade and fresh N birth followed by first-start registration;
the drift gate refuses unmirrored dependency changes before DB mutation.

## Adding a new migration

1. Write `migrations/YYYY/MM/DD/HH/YYYYMMDDTHHMMSS_<kebab-name>.sql` under the repository
   root. `db/migrations/` is not discovered at startup and is rejected by migration lint.
   The prefix is a
   second-precision UTC timestamp (`date -u +%Y%m%dT%H%M%S`). The `HH` directory
   is its UTC hour. The loader discovers SQL recursively
   and identifies migrations by basename, so additional directory levels preserve identity.
   Use further UTC minute/second groups if an hour exceeds the directory budget. Write
   pure SQL; don't INSERT schema_migrations (the runner does it)
2. There is no down migration. A mistake in a merged migration is fixed forward by a new
   migration; its SQL bytes and migration name never change (lint check 4). Directory-only
   moves retain the same applied-set identity. Lossy
   operations go expand-contract: ship the code that stops using the object first, drop it in
   a later migration
3. Sync the corresponding schema change into `db/schema.sql` (the baseline stays current).
   That includes `COMMENT ON` statements: the smoke compares `pg_dump --schema-only`
   output, so a comment that lives only in the migration makes the two diverge.
   When the change is **non-idempotent** (strict — no `IF NOT EXISTS` / `OR REPLACE`),
   also stamp this migration's name in the seed section — `INSERT INTO schema_migrations
   (name) VALUES ('<name>')` — because a fresh DB replays every unseeded migration and
   would fail on `already exists` (lint check 7 enforces the mechanically detectable cases)
4. PR review focus: running `db/schema.sql` on a fresh DB and running the baseline + all
   post-baseline migrations on a dev DB must converge to the same schema
5. An object the migration touches may belong to an upstream dependency rather than to
   `db/schema.sql` — the LangGraph checkpoint tables the saver creates are the current
   example. The table is present when a real cluster migrates (a birth runs
   `PostgresSaver.setup()` before this step), but not in a schema-only database, so such
   a migration guards on `to_regclass` and says so with `RAISE NOTICE` when it skips.
   Keep the guard narrow: it covers the fixture, never a cluster that should have the
   table

## Pre-commit lint

`.venv/bin/python scripts/content_lint/lint_migrations.py` checks the migration
contracts below; CI and the pre-commit hook run the same checker.

1. **Filename format** — every migration matches
   `YYYYMMDDTHHMMSS_<kebab-name>.sql` (the regex is reused from
   `base.deploy.schema.migrations`, so lint and the runtime loader can never
   disagree on the format). The timestamp part must parse as a real UTC datetime
   (catches fat-fingered `20261301T...`). There are no down migrations: a
   `.down.sql` file is rejected, and a mistake is fixed forward.
2. **Unique names** — no two files share a name (the applied-set primary key
   would reject a duplicate at apply; catch it here).
3. **Unique timestamp prefixes** — no two migrations share the same
   `YYYYMMDDTHHMMSS` prefix. Names stay second-precision by ruling (2026-08-07),
   so a shared prefix means two distinct migrations are timestamp-ambiguous;
   operator chatter keys off these names, and a pair that differs only in the
   kebab tail is one typo away from colliding.
4. **merged migrations are immutable** — against the base revision (the
   merge-base with `origin/main`, or `--base` in CI) no existing
   migration name or SQL bytes may change; directory-only moves preserve identity.
   A DB that already applied a migration will never re-run it, so editing the
   file forks what a fresh DB builds from what the applied ones hold, and
   deleting it makes the applied set disagree with the code's required set
   (the develop/prod fork after migration #27 was deleted). Skipped, with a
   note, when no base revision can be found.
5. **schema.sql baseline seed** — `db/schema.sql` must stamp the baseline
   sentinel row and must NOT still carry the pre-cutover `generate_series(...)`
   seed. It must also stamp every migration name whose non-idempotent change is
   already folded into the current schema — the stamp is what keeps a fresh DB
   from replaying that change. Check 8 enforces the mechanically detectable
   subset of this rule, where a fresh-DB replay would definitely fail; the
   replay-safe shapes (idempotent DDL, guarded DO blocks, in-place rebuilds)
   stay exempt by design.
6. **rollback-snapshot retirement** — every table following the shared
   `*_backfill_*` rollback-snapshot convention that is created by an up
   migration must have a later up migration that drops it. The archive CLI
   accepts the same convention; the table is a finite recovery buffer, never
   durable application state.
7. **folded strict migrations must be seeded** — an unseeded up migration must
   not carry a strict (non-idempotent) DDL statement whose object already
   exists in `db/schema.sql`: a fresh DB replays every unseeded migration over
   the baseline (the migration smoke builds exactly that DB), and the statement
   would die on "already exists". Covers `ADD COLUMN`, `CREATE TABLE`,
   `CREATE INDEX`, `ADD CONSTRAINT`, and `CREATE TRIGGER`, matched
   table-qualified so a same-named object on another table never trips it.
   Exempt by design: statements the same or an earlier unseeded migration drops
   first (a replay rebuild), statements inside an anonymous `DO` block (the
   block owns its guard), and objects absent from the baseline (they cannot hit
   already-exists — whether the baseline is missing the change at all is the
   smoke convergence gate's question). Heuristic by design — a tripwire for the
   convention, not a SQL parser.
8. **no role switching** — neither `db/schema.sql` nor any migration may
   `SET ROLE`, `RESET ROLE` or change the session authorization. Both run as
   the OS-user administrator acting as the schema owner (`base.cluster.dataplane.pg_admin`);
   switching away would create admin-owned objects or run with superuser
   rights the owner never had. Comments and quoted text are ignored.

Deliberately **no** continuity / next-number / cross-branch-collision checks:
timestamp names are collision-free by construction, which is the whole point of
the 2026-07-19 re-baseline (the 0060 / 0062 / 0080 numbering collisions).

Timestamp migrations under `db/` are rejected because startup discovers only
repository-root `migrations/`.

## Baseline-schema smoke test

`scripts/ci/test_migrations_apply.sh` (matching ci.yml step) builds a fresh empty DB, applies the
baseline `db/schema.sql`, INSERTs a fixture agent + page + UPDATEs
`agents_meta SET status='terminated'`, and verifies the cascade trigger
actually fires (DO block asserts `agent_pages.closed_at` was
propagated). **Adding any migration that changes a trigger / function
body must add an exercise line to `test_migrations_apply.sh`** —
`CREATE OR REPLACE FUNCTION` at apply time does NOT validate PL/pgSQL
column references; body bugs surface only when the trigger actually
fires (a wrong `NEW.agent_id` reference
in #369's rename slipped past lint + pytest + apply-time validation,
caught only by real cross-machine force-terminate in prod).

## Schema drift

`scripts/audit/schema_drift.py` compares a live database's public columns with
`db/schema.sql` and reports every extra or missing table and column with a class
— a designed runtime object (checkpoint tables, `telemetry_events` partitions)
or unexplained residue. Run it before writing a migration for an object you
cannot find in the baseline, and after an upgrade:
[schema drift audit](../../scripts/audit/docs/schema-drift.meiva.okf.md).
