# Case evidence after a single main red

Task #6488 uses opportunities rather than age: zero later completed runs of
the same workflow is a wait, while a later run supplies a discovery worth
review. A workflow verdict cannot distinguish a case that passed from one
that was skipped or never collected. On 2026-11-12, failed ci.yml run
43126259332 nevertheless contained the prior suspect
`test_runner_hard_exit_child_ownership[complete]` passing in backend shard 3
(2.329 seconds). It had previously failed in a different shard.

Rechecks therefore read published JUnit across shards and preserve missing or
unreadable evidence separately from absence in successfully parsed bundles.
Only explicitly matching attempt filenames are used when publishers name
attempts. A later attempt of the same run can update the evidence without
charging a second run opportunity.

The first opportunity opens a review card, even if the case is green in that
run: the card records the original single red and its observed disposition.
A normal second red uses the same card; a subsequent exact case green closes
it. This preserves the requested discovery without declaring a missing or
skipped case fixed. Historical workflow-wide green inference remains for
legacy confirmed failures outside this review path.

Existing phase-one entries with charged opportunities but no case evidence
are backfilled from bounded main-branch run metadata and JUnit, without
replaying failed logs or charging the same historical runs again. Runs newer
than the stream watermark remain for the normal next round. An unavailable
historical listing still exposes one review card with unreadable evidence and
the retained opportunity count. Each round shares one lazy 120-second network
budget across its on-demand JUnit and history reads; exhaustion remains
explicitly unreadable rather than a case green.

The Grafana table reads the latest round before removing empty slots. Doing
the filtering inside LogQL would keep an older nonempty suspect visible after
it disappeared. Runtime provisioning and a real round remain separate
acceptance steps; this change does not trigger CI runs or manufacture
production cards.

## CI follow-up: registry ownership

The initial panel edit omitted its registration and two dashboard contracts;
full CI exposed those omissions. The corrected panel is supplied by the core
metric registry and regenerated through the existing renderer, preserving its
geometry and fidelity locks. A positive Grafana log-record limit explicitly
selects bounded backward range reads for raw-log tables; ordinary metric
tables keep their existing instant-query behavior. This avoids a second
fixture-only panel path or weakening the registration worklist assertion.
