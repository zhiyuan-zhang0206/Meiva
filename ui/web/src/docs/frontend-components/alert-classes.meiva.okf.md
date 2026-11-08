---
type: doc
title: Warning and Error Counts
description: The compact, windowed Warning / Error event totals in the sidebar status panel, separate from formal Grafana alerts.
tags:
- frontend
---

# Warning and Error Counts

The Warnings / Errors card in the Status popover (`agent-sidebar/footer.tsx`) displays
`warnings / errors` from `GET /api/stats/dashboard` over the selected
window. These are event totals; `errors` includes critical events. The card is a
compact cell like the other status cards, with no nested class/sample list.
Changing the window uses the same loading and mismatch placeholders as its peers.

Both totals count only classes that are still happening: a warning or error class that
recorded nothing for the gateway's `alert_class_quiet_seconds` reads as resolved, so the card
stops showing it. At zero the card drops its alert tone and says no warning or error is still
happening, and its tooltip names how many were resolved (`warnings_resolved` /
`errors_resolved` in the same payload), so a number falling to zero stays explainable.

The event-class diagnostics and dismissal APIs remain available to operators;
they neither replace these totals nor become formal alert data.
The backend contract: [[gateway/cluster/docs/alert-classes-api.meiva.okf.md|Alert Classes]].

Formal Grafana alerts have their own header badge and Insights history. Their
SSE stream invalidates TanStack Query snapshots, so the server owns ordering,
filters and uncapped counts. All unresolved instances remain visible regardless
of age; the history window selects recently resolved episodes by recovery time.
Grouping and recovery belong to Grafana / Alertmanager, not frontend time buckets.
