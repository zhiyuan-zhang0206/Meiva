"use client";

// meiva:note
// The firing-alerts bell leads the sidebar's icon row, ahead of the Fleet shortcut: it is a
// standing state of the cluster, not a control over what the reader is looking at, and a
// badge in the header took a permanent slot from the conversation (user 2026-10-21). With
// the sidebar collapsed the row is gone, so the same signal rides the restore button at the
// bar's left edge and stays visible without opening the rail (user 2026-10-21).
// The count is what Grafana reports firing now (`useFiringAlerts`); clicking opens Grafana's
// firing-alert list in a new tab, since Meiva keeps no alert view of its own (user 2026-11-24).
// Nothing renders at zero — a zero badge is noise — but a Grafana that cannot be reached
// renders a grey "?" in place of the count: that state is unknown, not zero.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.md

import { Bell } from "lucide-react";
import { useTranslations } from "next-intl";

import { FLEX, ICON_SIZE_CLASS } from "@/lib/layout/layout";
import {
  GRAFANA_FIRING_ALERTS_URL,
  alertsSignal,
  useFiringAlerts,
  type AlertsSignal,
} from "@/lib/notifications/use-firing-alerts";
import { cn } from "@/lib/format/utils";

// meiva:note
// One bubble for both surfaces that show the signal: the sidebar's bell and the collapsed
// rail's restore button. Past 99 the digits would widen the bubble past the icon it sits
// on, so it reads "99+" (the exact count is in Grafana). An unknown status is a grey "?" of
// the same size, never a number.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.md
export function AlertsCountPill({ signal, testId = "alerts-count-pill" }: { signal: NonNullable<AlertsSignal>; testId?: string }) {
  const unknown = signal.kind === "unknown";
  return (
    <span
      data-testid={testId}
      className={cn(
        "absolute -right-0.5 -top-0.5 rounded-full px-1 text-[10px] font-semibold tabular-nums leading-4",
        unknown ? "bg-muted text-muted-foreground" : "bg-destructive text-white",
      )}
    >
      {signal.kind === "unknown" ? "?" : signal.count > 99 ? "99+" : signal.count}
    </span>
  );
}

export function useAlertsLabel(): (signal: NonNullable<AlertsSignal>) => string {
  const t = useTranslations("alerts");
  return (signal) => (signal.kind === "unknown" ? t("unreachable") : t("firingAlerts", { count: signal.count }));
}

export function AlertsBadge() {
  const { data } = useFiringAlerts();
  const label = useAlertsLabel();
  const signal = alertsSignal(data);
  if (signal === null) return null;

  return (
    <a
      href={GRAFANA_FIRING_ALERTS_URL}
      target="_blank"
      rel="noopener"
      aria-label={label(signal)}
      title={label(signal)}
      data-testid="alerts-badge"
      className={cn(
        "relative shrink-0 rounded p-1.5 text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground",
        FLEX,
      )}
    >
      <Bell className={ICON_SIZE_CLASS} aria-hidden />
      <AlertsCountPill signal={signal} testId="alerts-badge-count" />
    </a>
  );
}
