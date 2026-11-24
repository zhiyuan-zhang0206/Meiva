// meiva:note
// Old deep links point at /insights/alerts (the URL the retired alert IM messages
// carried). Meiva keeps no alert history of its own any more — Grafana owns it, and the
// link to it sits in the Insights Ops section (/insights#ops), where this route forwards.

import type { Route } from "next";
import { redirect } from "next/navigation";

// meiva:note
// This route is the redirect's composition root: it hands `redirect` to the view, so a test passes
// a spy instead of replacing `next/navigation`.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.md
export default function AlertsRedirect() {
  return <AlertsRedirectView redirect={redirect} />;
}

export interface AlertsRedirectViewProps {
  redirect: (href: Route) => void;
}

export function AlertsRedirectView({ redirect }: AlertsRedirectViewProps) {
  redirect("/insights#ops");
  return null;
}
