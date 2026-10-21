"use client";

// meiva:note
// The alerts count sits in the sidebar's icon row beside Settings rather than in the
// conversation header: it is a standing state of the cluster, not a control over what
// the reader is looking at, and a badge in the header took a permanent slot from the
// conversation (user 2026-10-21). Clicking jumps to the alert section, and nothing
// renders at zero — a zero badge is noise.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { Bell } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";

import { FLEX } from "@/lib/layout/layout";
import { useAlerts } from "@/lib/notifications/use-alerts";
import { cn } from "@/lib/format/utils";

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
      <span
        data-testid="alerts-badge-count"
        className="absolute -right-0.5 -top-0.5 rounded-full bg-destructive px-1 text-[10px] font-semibold tabular-nums leading-4 text-white"
      >
        {unresolved > 99 ? "99+" : unresolved}
      </span>
    </Link>
  );
}
