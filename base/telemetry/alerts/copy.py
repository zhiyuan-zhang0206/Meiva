# meiva:note
# The user-visible alert templates, one definition and no copies ("one voice for alert
# copy", ruling 2026-08-08; they live in this `base` leaf module because the alert-ingest
# core must not import up into `services`). Alert copy is English only (user ruling
# 2026-11-24, with the UI). Only template/framework copy lives here: alert labels/annotations
# data (severity, alertname, summary, generator_url) passes through as received.
# {severity}/{alertname}/{time}/{url} are filled in at the call site.
# meiva:link gateway/alerts/docs/alerts.md

from __future__ import annotations

ALERT_HEAD = {
    "firing": "⚠️ ALERT [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
    "resolved": "✅ RESOLVED [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
}
ALERT_TRIGGERED_AT = "triggered {time}"
ALERT_JUMP_LINK = "→ {url}/insights/alerts"
# One webhook group (the instances of one rule) is one message: the head carries the count and
# the body lists the first instances' summaries.
ALERT_GROUP_COUNT = " ×{n}"  # noqa: RUF001 — the user-designated count mark
ALERT_GROUP_MORE = "… and {n} more"
