# Drop the gateway's alert tables

The gateway keeps no copy of alert state any more
([decision](../../../docs/decisions/observability/alerts/2026-11-24-alerts-have-one-owner-grafana.md)).
No production code reads or writes `alerts`, `alert_notification_groups`,
`alert_notification_members` or `alert_escalations`. The shadow escalation ledger
([decision](../../../docs/decisions/observability/alerts/2026-11-24-alertmanager-agent-receiver.md))
was retired earlier. The tables stay through the supported rollback window because the previous
release still writes and reads them.

After rollout, verify that the gateway and every events-maintenance unit run a release without the
alert store, and that the supported rollback target is no older than that release. Then add one
timestamped migration that drops the four tables (members, groups, `alerts`, `alert_escalations`),
synchronize `db/schema.sql`, remove the entries from `tests/fixtures/provisioning.py`, and delete
`assert_alert_writes_denied` with its grant-matrix call. The creation migrations remain immutable.
A `pg_dump -t alerts` before the drop keeps the rows that Grafana's own history does not (the
seven-day volume is a few hundred rows). Verify both upgrades and fresh schema provisioning.
