-- Second step of the Instant Messaging removal (the code went in 20261108, see
-- docs/decisions/extensions/im/2026-11-08-im-support-removed.md). Nothing reads or writes the
-- im_bridge_* and weixin_ingress_* families any more, and the daemon that did is stopped on every
-- machine. Lossy by design: the rows are IM delivery bookkeeping with no remaining consumer.
-- Kept on purpose, as history rather than IM support: alerts.notified_at,
-- alerts.notification_revision, alerts.notified_revision and the native-v1 origin values.
-- Dependants first; IF EXISTS so a fresh database (whose baseline already lacks them) replays this.
DROP TABLE IF EXISTS weixin_ingress_receipts;
DROP TABLE IF EXISTS weixin_ingress_cursors;
DROP TABLE IF EXISTS weixin_ingress_bindings;
DROP TABLE IF EXISTS im_bridge_outbound_replays;
DROP TABLE IF EXISTS im_bridge_outbound_intents;
DROP TABLE IF EXISTS im_bridge_alert_acceptances;
DROP TABLE IF EXISTS im_bridge_notice_acceptances;
DROP TABLE IF EXISTS im_bridge_notice_poll_state;
DROP TABLE IF EXISTS im_bridge_cursors;
