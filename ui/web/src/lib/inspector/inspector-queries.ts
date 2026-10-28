import type { Api } from "../transport/api";

export { inspectLiveQueryKey } from "../contracts/types";

export const inspectWindowedQueryKey = (agentId: number, hours: number | null) =>
  ["agent-inspect", agentId, hours] as const;

export const inspectWidgetsQueryKey = (agentId: number) =>
  ["agent-inspect-widgets", agentId] as const;

export const fetchWindowedInspect = (
  client: Api,
  agentId: number,
  hours: number | null,
  signal?: AbortSignal,
) => client.getAgentInspectStatistics(agentId, hours, signal);
