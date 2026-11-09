# Deployment primitives

`base/deploy/` owns home lifecycle, schema, release, provenance and deployment state.
Its component nodes describe the current contracts and implementation.

## Documented components

- [Home Lifecycle Mutex](../lifecycle/docs/home_lifecycle_locks.md) — Home Lifecycle Mutex.
- [Explicit service selection](../lifecycle/docs/service-selection.md) — Explicit per-start service selection.
- [Collector artifact download](../release/docs/collector-artifact.md) — Collector artifact download.
- [Schema Migrations (baseline + deltas)](../schema/docs/migrations.md) — Schema Migrations (baseline + deltas).
- [Schema reset generations](../schema/docs/reset-generations.md) — Schema reset generations.
- [Recovery Snapshot Convention](../schema/docs/rollback_snapshot.md) — Recovery Snapshot Convention.
- [Start phase timings](start-trace.md) — Start phase timings (on-demand `meiva start` attribution).

`timing.clock_values()` captures the canonical registered clock sources once at
agent-host startup. The startup root supplies that complete snapshot to
`assert_clock_lattice`; validation rejects missing or unknown names and checks
the same declared ordering and derivations without reading settings again. Three
of those clocks — the wedged age, the exec node timeout and the held path's
settle budget — are operator settings, so the root passes them in and the lattice
judges the values the host will live by rather than the defaults they were seeded
from.
