"use client";

// /insights#ops — links to the merged Meiva Ops dashboard and to Grafana's firing-alert list,
// both through the gateway's /grafana proxy. Meiva keeps no alert view of its own.

import { useTranslations } from "next-intl";

import { GRAFANA_FIRING_ALERTS_URL } from "@/lib/notifications/use-firing-alerts";
import { API_BASE } from "@/lib/transport/api";

const OPS_DASHBOARD_URL = `${API_BASE}/grafana/d/meiva-ops-main?from=now-24h&to=now`;

export default function OpsPage() {
  const t = useTranslations("insights.opsPanel");
  return (
    <div className="space-y-4">
      <div id="ops-metrics" className="scroll-mt-4">
        <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
          {t("metrics")}
        </h3>
        <a
          href={OPS_DASHBOARD_URL}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-muted-foreground hover:text-foreground underline underline-offset-2"
        >
          {t("openDashboard")}
        </a>
      </div>
      <div id="ops-alerts" className="scroll-mt-4">
        <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
          {t("alerts")}
        </h3>
        <a
          href={GRAFANA_FIRING_ALERTS_URL}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-muted-foreground hover:text-foreground underline underline-offset-2"
        >
          {t("openAlerts")}
        </a>
      </div>
    </div>
  );
}
