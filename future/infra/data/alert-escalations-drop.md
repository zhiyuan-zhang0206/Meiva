# Drop the retired shadow escalation ledger

The Alertmanager agent receiver change retires the only production code that
reads and writes `alert_escalations`. Keep the table through the supported
rollback window because the previous events-maintenance daemon still uses it.

After rollout, verify that all running events-maintenance units include the
retirement and confirm the supported rollback target also excludes the loop.
Then add a timestamped migration to drop the table, synchronize the baseline
and remove the test fixture entry. The original creation migration remains
immutable. Verify both upgrades and fresh schema provisioning.

[Decision](../../../docs/decisions/observability/alerts/2026-11-24-alertmanager-agent-receiver.md)
