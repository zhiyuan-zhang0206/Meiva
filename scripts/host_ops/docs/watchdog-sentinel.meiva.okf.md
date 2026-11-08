---
type: doc
title: "Alert-pipeline host sentinel"
description: "An observer outside Grafana that checks accepted-ingest age and submits incidents through the installed agent CLI."
---

# Alert-pipeline host sentinel

The sentinel queries Prometheus for the age of the newest accepted alert ingest.
It remains unarmed until a sample exists, records healthy samples locally, and
submits a silent-pipeline incident through the existing `meiva agents send`
path. A successful CLI exit establishes queue acceptance; it does not prove the
human read a message. Failed submission is logged, returns nonzero, and remains
retryable. Successful submissions share the persisted repeat window.

It does not use Grafana's alert engine or any messaging credentials. Delivery
still requires the installed CLI and the cluster services that agent messaging
uses. The CLI posts to the gateway agents message endpoint; gateway outage therefore
leaves the local log and nonzero status. This is not
a separate out-of-cluster route to the human.

Run one tick with `python scripts/host_ops/watchdog_sentinel.py`; use `--dry-run`
to inspect the decision without submitting a message. Existing state and log
paths remain under the supplied home. Runtime job registration is separate from
this repository change.
