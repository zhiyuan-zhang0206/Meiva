import { useQuery } from "@tanstack/react-query";

import { API_BASE } from "../transport/api";
import { useApi } from "../transport/transport-context";
import type { FiringAlerts } from "../contracts/types";

export const FIRING_ALERTS_QUERY_KEY = ["alerts", "firing"] as const;

const REFRESH_MS = 30_000;
const STALE_MS = 15_000;

export const GRAFANA_FIRING_ALERTS_URL = `${API_BASE}/grafana/alerting/list?search=state:firing`;

export type AlertsSignal = { kind: "count"; count: number } | { kind: "unknown" } | null;

export function alertsSignal(alerts: FiringAlerts | undefined): AlertsSignal {
  if (alerts === undefined || alerts.state === "not_configured") return null;
  if (alerts.state === "unavailable" || alerts.firing == null) return { kind: "unknown" };
  return alerts.firing > 0 ? { kind: "count", count: alerts.firing } : null;
}

export function useFiringAlerts() {
  const api = useApi();
  return useQuery<FiringAlerts>({
    queryKey: FIRING_ALERTS_QUERY_KEY,
    queryFn: () => api.getFiringAlerts(),
    staleTime: STALE_MS,
    refetchInterval: REFRESH_MS,
  });
}
