# Remove the Instant Messaging support

The user asked on 2026-11-08 to delete every Instant Messaging app integration,
having concluded that IM support is deep and the timing wrong. The repository had
already retired Telegram and Feishu on 2026-11-06; Weixin was the last adapter and
had been production-disabled since 2026-08-06, so the supervised daemon ran
without anything to deliver.

The removal covers the `im-bridge` service and its roster entry, the adapters,
ingress commands, the durable outbound queue and the notice bridge, the
alerts-to-IM acceptance path and its completion stamps, the `im_*` config fields
with their env aliases, the fixed and health ports, the two IM push rules (as
Grafana tombstones), the two IM events, and the IM narrative check.

Alerts keep their store, list, read state and SSE tail. The ingest still freezes
the rendered notification group; nothing sends it, so `notified_at` has no writer
and stays NULL. The IM tables stay in place: the previous release's daemon still
reads them until a wave stops it, and the DROP TABLE migration follows that wave.

Retired serialized records remain readable, and historical decision and incident
records remain evidence of the capability at that time. No production home is
changed by this change itself; the deployment that stops the old daemon is a
separate, operator-run wave.
