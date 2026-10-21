"use client";

// meiva:note
// The alerts count leads the sidebar's icon row, ahead of the Fleet shortcut: it is a
// standing state of the cluster, not a control over what the reader is looking at, and a
// badge in the header took a permanent slot from the conversation (user 2026-10-21). With
// the sidebar collapsed the row is gone, so the same count rides the restore button at the
// bar's left edge and the count stays visible without opening the rail (user 2026-10-21).
// Clicking jumps to the alert section, and nothing renders at zero — a zero badge is noise.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { Bell } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";

import { FLEX } from "@/lib/layout/layout";
import { useAlerts } from "@/lib/notifications/use-alerts";
import { cn } from "@/lib/format/utils";

// meiva:note
// One count bubble for both surfaces that show it: the sidebar's bell and the collapsed
// rail's restore button. Past 99 the digits would widen the bubble past the icon it sits
// on, so it reads "99+" (the exact count is in the alert section).
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
export function AlertsCountPill({ count, testId = "alerts-count-pill" }: { count: number; testId?: string }) {
  return (
    <span
      data-testid={testId}
      className="absolute -right-0.5 -top-0.5 rounded-full bg-destructive px-1 text-[10px] font-semibold tabular-nums leading-4 text-white"
    >
      {count > 99 ? "99+" : count}
    </span>
  );
}

export function AlertsBadge() {
  const t = useTranslations("alerts");
  const { data } = useAlerts();
  const unresolved = data?.meta.unresolved_count ?? 0;
  if (unresolved === 0) return null;

  return (
    <Link
      href="/insights#alerts"
      aria-label={t("unresolvedAlerts", { count: unresolved })}
      data-testid="alerts-badge"
      className={cn(
        "relative shrink-0 rounded p-1.5 text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground",
        FLEX,
      )}
    >
      <Bell className="size-4" aria-hidden />
      <AlertsCountPill count={unresolved} testId="alerts-badge-count" />
    </Link>
  );
}
