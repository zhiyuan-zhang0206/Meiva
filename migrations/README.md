# migrations/

Post-baseline schema deltas, grouped by UTC date, one forward file each:

```
YYYY/MM/DD/YYYYMMDDTHHMMSS_<kebab-name>.sql
```

- **Timestamp prefix** = second-precision UTC (`date -u +%Y%m%dT%H%M%S`). It
  makes names collision-free without a coordinating counter, so parallel
  branches never fight over the "next number". Names sort ≈ chronologically;
  `apply_pending_migrations` applies every file whose name is not yet in the
  DB's applied set, in name order.
- **No down migrations.** A mistake is fixed forward by a new migration, and
  lossy operations go expand-contract (the drop is its own later migration,
  after the code that stopped using the object has shipped).
- **A merged migration is immutable.** SQL bytes and names already on main never change. Directory-only moves
  preserve the name and bytes, and therefore the applied-set identity
  (`scripts/content_lint/lint_migrations.py`, against the merge-base with
  `origin/main`). A DB that applied it never re-runs it, so changing the file
  forks what a fresh DB builds from what applied DBs hold.
- The current full schema lives in **`db/schema.sql`** (the squashed baseline a
  fresh DB bootstraps from); a new migration must also reflect its change there.

## Current baseline (2026-11-01)

The 8 deltas through `20261101T091332_retire-display-and-timeline-settings` are folded into
`db/schema.sql` (`20261017T203552_checkpoints-compact-boundary-index` remains as a
post-baseline file because `public.checkpoints` is created by LangGraph's
`PostgresSaver.setup()` after `db/schema.sql`). Fresh databases stamp the baseline
sentinel and `20261101T170000_schema-baseline`; the retired history's seed rows are
absent. Subsequent deltas must also be reflected in the current schema.

`base/deploy/schema/migration_history.py` keeps the exact frozen inventories for this reset
(8 names), the 2026-10-26 reset (93 names), the 2026-09-23 reset (101 names), and the
2026-08-14 reset (59 names). The earlier inventories remain necessary because older backup/PITR
restores have not been ruled out. Before deleting any tracking rows, the runner refuses partial
generations. A database without the current anchor must carry the entire 8-name predecessor inventory;
an old baseline-only restore cannot masquerade as a fresh database.

Upgrade older restores through the release immediately before the applicable
reset. Integer-keyed history is unsupported; restore it under its matching
release before advancing. Current live databases already use the applied set.

Replaying an older release across the current anchor cannot safely restore the
folded strict DDL. Recover across the boundary with a matching pre-reset database
backup, or fix forward. Merge does not authorize a
runtime rollout or a restore.
