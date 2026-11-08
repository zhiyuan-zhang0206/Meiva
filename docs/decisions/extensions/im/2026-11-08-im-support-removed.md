# Remove the Instant Messaging support

## Context

Meiva shipped a native IM frontend: a supervised `im-bridge` daemon that spoke to
Weixin (iLink QR login) through our own SDK, an ingress command set, a durable
outbound queue with claim fences and retries, a notice bridge, an alerts-to-IM
leg with its own acceptance/receipt contract, per-channel config fields, an
`im_bridge_*` table family and a Weixin ingress table family.

Telegram and Feishu already retired on 2026-11-06
([history](../../../history/2026-11-06/im-channel-retirement.md)), leaving Weixin
as the only adapter — and it was production-disabled since 2026-08-06
(`MEIVA_IM_DISABLED_ADAPTERS=weixin`), so the daemon ran with nothing to deliver.

On 2026-11-08 the user ruled: *"delete all support for Instant Messaging apps.
IM support is deep and the timing is wrong; remove it wholesale and revisit
later."* The maintenance cost is real and recurring — an SDK-compatibility
surface, a separate replay path, a second alert contract, its own database
family, its own alert rules and its own deployment considerations — for a
channel that delivered nothing.

## Decision

Remove IM support wholesale: the daemon and its roster declaration, both
adapters' code, ingress/commands, the outbound worker and its tables' code, the
notice bridge, the alerts-to-IM leg (acceptance, receipts, the native origin
producer, `notified_at`/`notified` completion), the `im_*` config fields and
their env aliases, the fixed port and health-port entries, the two
`meiva-ops-im-push-*` Grafana rules (replaced by provisioning tombstones so a
live Grafana drops them), the `im_push_failed`/`im_push_health` events, and the
IM narrative checker.

The alert store, list, read state and SSE tail stay: the ingest still freezes the
rendered notification group as the transition record. Nothing delivers it.

**Database tables are not dropped in this change.** The running `im_bridge` of
the previous release still reads `im_bridge_*` and `weixin_ingress_*` until a
wave stops it, and a destructive DDL cannot be undone by a revert. The DROP
TABLE migration is the second step, after that wave
(`future/infra/im-tables-drop.md` records the order and the checks that must pass
first).

## Consequences

The gateway, the roster, the config surface and the frontend lose a product
feature. `notified_at` becomes a column with no writer: new rows keep NULL, so
"notified" reads false for everything, which is the truth. `notify_im` labels in
the Grafana rules are inert. Historical rows and their `native-v1` origin stay
readable; historical decision and incident records stay as evidence.

Any future IM return starts from the recorded requirements rather than from this
code, and needs its own decision.

## Alternatives rejected

- Keep the adapter disabled: the dependency, the SDK patches, the replay path and
  its tests keep costing maintenance for zero delivered messages; the user chose
  removal over dormancy.
- Remove only the daemon and leave the alert-side acceptance: the gateway would
  POST to a route that no longer exists and log a failure for every alert.
- Drop the tables in the same change: a running old daemon reads them, and a
  failed rollout would then have lost its data with no way back.
