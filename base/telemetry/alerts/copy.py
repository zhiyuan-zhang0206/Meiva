# meiva:note
# The user-visible alert templates, one definition and no copies ("one voice for alert
# copy", ruling 2026-08-08; they live in this `base` leaf module because the alert-ingest
# core must not import up into `services`). Language follows the UI language --
# user_settings ``display.language`` ("zh" | "en"; missing/unknown falls back to
# ALERT_LANGUAGE_DEFAULT, user ruling 2026-08-13). Only template/framework copy is
# translated: alert labels/annotations data (severity, alertname, summary, generator_url)
# passes through untranslated. The English set keeps the pre-ruling production strings;
# {severity}/{alertname}/{time}/{url} are filled in at the call site.
# meiva:link gateway/alerts/docs/alerts.meiva.okf.md

from __future__ import annotations

ALERT_LANGUAGES = ("zh", "en")
ALERT_LANGUAGE_DEFAULT = "zh"

ALERT_HEAD = {
    "zh": {
        "firing": "⚠️ 告警 [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
        "resolved": "✅ 已恢复 [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
    },
    "en": {
        "firing": "⚠️ ALERT [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
        "resolved": "✅ RESOLVED [{severity}] {alertname}",  # emoji-ok: user-designated IM alert format
    },
}
ALERT_TRIGGERED_AT = {
    "zh": "触发时间 {time}",
    "en": "triggered {time}",
}
ALERT_JUMP_LINK = "→ {url}/insights/alerts"
# One webhook group (the instances of one rule) is one message: the head carries the count and
# the body lists the first instances' summaries.
ALERT_GROUP_COUNT = " ×{n}"  # noqa: RUF001 — the user-designated count mark
ALERT_GROUP_MORE = {
    "zh": "…另有 {n} 个",
    "en": "… and {n} more",
}
