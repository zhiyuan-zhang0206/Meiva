# Persisted Inspector observations

We chose a compact Postgres read model for core Inspector statistics. The browser
must not depend on Loki availability, retention, or raw-log fan-out to render
cost and activity. Collapsing duplicate Loki queries or extending an aggregate
cache would reduce work without removing that dependency, so those alternatives
were rejected. In-flight request sharing remains admission control, not freshness
camouflage.

The model is deliberately observed metrics rather than an audited billing ledger.
Telemetry already has a bounded, lossy queue, and an accounting failure must not
make an agent execution fail. Costs therefore retain the recorded usage-time
price, unknown prices remain explicitly unpriced until cataloged, and successful
persistence or replay does not certify completeness. A read timestamp and newest
observation are different facts; neither is a lossless collection watermark.

### Addendum (2026-11-22): Null-to-priced historical backfill

The immutability rule above protects **already-priced** rows (`cost_usd IS NOT NULL`
/ `unpriced_calls = 0`): a row that captured a usage-time price snapshot is never
repriced against a newer catalog. It does **not** forbid filling previously blank
rows. *Superseded 2026-11-24:* the observation tables are no longer repriced; a blank
usage-ledger row (`llm_usage_ledger.cost_usd IS NULL`) is priced at its own `occurred_at`
by `services/upkeep/events_maintenance/ledger_repricing.py`, and the former
former unpriced-backfill command is gone.

Historical family ledgers remain evidence for full UTC days before the collection
cutover day. They take precedence over replayed observations for that family and
day, preventing double counting while observations fill missing days and partial
edges. An old duration histogram is replaced only when the exact replay has all
of its turn samples. Retained one-second buckets keep their declared precision;
missing duration observations do not become zero throughput. Arithmetic preserves
integer counters and decimal price sums. Exact percentiles still require indexed
work proportional to selected duration observations; no constant-time percentile
claim is made.

The frozen archive and JSONL repair have disjoint timestamp ownership. Archive
owns timestamps through its final row, inclusive. JSONL owns later timestamps;
older metric rows are classified in the persisted cursor's excluded count while
scanning continues. This avoids guessing whether the old PG archive importer
used the same bytes or IDs as a historical mirror. Post-freeze mirrors written
before stored IDs existed use the documented canonical line/timestamp surrogate
introduced by commit 1a25ddb90. Unknown rows fail the repair batch rather than
silently advancing its cursor. Source scans describe completed traversal only.

Actual metadata status transitions define nonterminated intervals after cutover.
For an older agent, a recent window can have known activity even while its whole
lifetime denominator remains unknown. Historical absent evidence stays partial
or unavailable. Since-compact statistics stay unavailable because there is no
existing authoritative stamp after a completed durable compact; the early
success event cannot stand in for one. Adding that transaction boundary is a
separate execution contract decision.

Only typed numeric facts and identity/time fields are retained, never checkpoint
copies, message bodies, or log bodies. Storage grows linearly with observed metric
events, plus per-agent daily sums and lifecycle intervals. This change introduces
no expiry or deletion of retained facts: discarding exact durations would change
future exact-window and percentile semantics. Recovering all history is not
promised, and unavailable archive or absent mirrors remain visible limitations.

Downgrade refuses before any DDL when persisted metric evidence or lifecycle
intervals exist. Empty-schema reversal is supported; a populated downgrade
requires a separately reviewed preservation protocol because expired telemetry
cannot reconstruct the sole durable facts.

The older `agent_archive_stats` exact whole-archive distribution remains retained.
It has no event timestamps and cannot safely replace day-selected distributions
across the archive/live partial-day seam. Metadata names that retained but
unapplied evidence; this change does not claim it was lost or silently use it
as exact window evidence. A precision enhancement requires a proven disjoint
interval replacement or timestamp-addressable replay.
