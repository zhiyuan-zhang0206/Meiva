# Retire native Feishu and Telegram channels

The user requested removal of this repository's Feishu support, then added Telegram to the same retirement on 2026-11-06,
while continuing the recorded-debt repayment work. Maintaining the Feishu
adapter, its SDK compatibility patches and its separate replay path no longer
serves the chosen product scope. The chosen action removes both capabilities;
keeping a dormant adapter would preserve the dependency and maintenance work.

This decision retires the native IM integration, configuration domain and
SDK dependency. Weixin remains the only shipped native adapter. Installed
third-party skills and the user's external accounts are outside this change.
Retired serialized adapter kinds remain readable for historical alert receipts.
Existing database cursor and outbox records remain retained; the worker only
selects streams for loaded adapters, so the retired channel is not dispatched.
There is no database cleanup or runtime rollout in this change.

The Feishu websocket compatibility patch disappears with the feature. Record
that decrease as feature retirement, separately from code repairs. Historical
incident and decision records remain evidence of the capability at that time.
Old Feishu secret variables remain scrubbed from child/test environments so an
older installation cannot leak its retired credentials after the config field
is removed. The Grafana provisioning tombstone removes the retired alert on a
future authorized provisioning pass.

Telegram retirement also removes automatic IM notice forwarding and Grafana's
direct Bot API receiver. Manual IM notice lookup returns text to its requesting
conversation; the shared UI queue and response handling stay intact. The host
sentinel keeps Prometheus sampling, its persisted repeat window and existing
agent-CLI submission. CLI acceptance is not a separate external receipt to the
human. Submission failure stays in its local log, returns nonzero and is retried.

With all retained adapters disabled or unconfigured, the IM daemon keeps its
health server and owned background tasks alive until shutdown. Retired adapter
names in the disabled list are ignored, and push-health channels are restricted
to the adapter names the release ships. No production home or environment is
changed by this patch.
