"use client";

import { useQuery } from "@tanstack/react-query";
import { useApi } from "../transport/transport-context";
import { ATTENTION_COUNTS_KEY } from "../fold";

// meiva:note
// Only the phone requests this exact snapshot; errors never become a known zero.
export function useMobileAttention(enabled: boolean) {
  const api = useApi();
  const { data, isError } = useQuery({
    queryKey: ATTENTION_COUNTS_KEY,
    enabled,
    queryFn: () => api.getAttentionCounts(),
  });
  return {
    count: data ? data.unread_agents + data.open_questions : null,
    state: isError ? "error" as const : data ? "ready" as const : "loading" as const,
  };
}
