import { api } from "../transport/api";

export { inspectLiveQueryKey } from "../contracts/types";

export const inspectWindowedQueryKey = (agentId: number, hours: number | null) =>
  ["agent-inspect", agentId, hours] as const;

export const inspectWidgetsQueryKey = (agentId: number) =>
  ["agent-inspect-widgets", agentId] as const;

export const fetchWindowedInspect = (
  agentId: number,
  hours: number | null,
  signal?: AbortSignal,
) => api.getAgentInspectStatistics(agentId, hours, signal);
