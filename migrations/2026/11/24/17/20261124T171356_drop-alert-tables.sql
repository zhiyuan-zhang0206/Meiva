-- The gateway keeps no copy of alert state (docs/decisions/observability/alerts/
-- 2026-11-24-alerts-have-one-owner-grafana.md): Grafana owns the alert list and its history, and
-- the escalation loop, the notification-group ledger and every reader of these tables are gone.
-- Lossy by design; operators archive the four tables with pg_dump before this runs.
-- Dependants first: alert_notification_members references alert_notification_groups.
DROP TABLE IF EXISTS alert_notification_members RESTRICT;
DROP TABLE IF EXISTS alert_notification_groups RESTRICT;
DROP TABLE IF EXISTS alert_escalations RESTRICT;
DROP TABLE IF EXISTS alerts RESTRICT;
