// meiva:note
// Fleet-page route parameters — the deep links into the supervision surface.
//
// `?task=` selects a task and reveals the Tasks view. Ids parse defensively: a
// malformed value routes nowhere rather than to a wrong row.

/** A positive-int id, or null when the parameter is absent or malformed. */
function routeId(raw: string | null): number | null {
  const parsed = raw === null ? Number.NaN : Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

export interface FleetRouteIds {
  taskId: number | null;
}

export function readFleetRouteIds(search: URLSearchParams): FleetRouteIds {
  return {
    taskId: routeId(search.get("task")),
  };
}
