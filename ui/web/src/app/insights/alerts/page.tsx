// IM deep links point at /insights/alerts (the URL the alert IM messages
// carry). The alert history lives as a section of the Insights page
// (/insights#alerts) — this route forwards the link there.

import type { Route } from "next";
import { redirect } from "next/navigation";

// meiva:note
// This route is the redirect's composition root: it hands `redirect` to the view, so a test passes
// a spy instead of replacing `next/navigation`.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
export default function AlertsRedirect() {
  return <AlertsRedirectView redirect={redirect} />;
}

export interface AlertsRedirectViewProps {
  redirect: (href: Route) => void;
}

export function AlertsRedirectView({ redirect }: AlertsRedirectViewProps) {
  redirect("/insights#alerts");
  return null;
}
