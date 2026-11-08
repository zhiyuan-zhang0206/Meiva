# Drop the IM tables (second step of #6537)

The code that reads them is gone (2026-11-08, `docs/decisions/extensions/im/2026-11-08-im-support-removed.md`).
The tables below still exist because the previous release's `im-bridge` daemon
reads them until a wave stops it.

Order:

1. Land and deploy the removal; confirm no `im-bridge` process is running on any
   machine (`meiva services status`, roster rows) and that the wave's health
   probes are green.
2. Add one migration dropping, in this order (dependants first):
   `weixin_ingress_receipts`, `weixin_ingress_cursors`, `weixin_ingress_bindings`,
   `im_bridge_outbound_replays`, `im_bridge_outbound_intents`,
   `im_bridge_alert_acceptances`, `im_bridge_notice_acceptances`,
   `im_bridge_notice_poll_state`, `im_bridge_cursors`.
3. Regenerate `db/schema.sql` from the migrations and update the fixture table
   list in `tests/fixtures/provisioning.py`.
4. Leave `alerts.notified_at`, `alerts.notification_revision`,
   `alert_notification_groups.origin = 'native-v1'` values and the message-source
   enum as they are: they are history, not IM support.
