"use client";

import { useQuery } from "@tanstack/react-query";
import type { BackendTimelineItem } from "../contracts/types";
import type { UserMessage } from "../contracts/types";
import { useApi } from "../transport/transport-context";

export function useUserMessages(agentId: number | null) {
  const api = useApi();
  return useQuery({
    queryKey: ["user-messages", agentId],
    queryFn: ({ signal }) => {
      if (agentId === null) throw new Error("User messages require an agent");
      return api.getUserMessages(agentId, signal);
    },
    enabled: agentId !== null,
    staleTime: Infinity,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });
}

export function withUserMessages(items: BackendTimelineItem[], messages: readonly UserMessage[]): BackendTimelineItem[] {
  const byKey = new Map(messages.map((message) => [message.source_key, message]));
  const attached = new Set<number>();
  const result = items.map((item) => {
    const block = item.item_id.split(".").at(-1);
    const key = item.source_message_id ? `assistant:${item.source_message_id}:0.${block}` : `harness:${item.item_id}`;
    const message = byKey.get(key);
    if (!message) return item;
    attached.add(message.id);
    return { ...item, userDelivery: message };
  });
  for (const message of messages) {
    if (attached.has(message.id) || message.content === null) continue;
    const publication: BackendTimelineItem = {
      item_id: `publication:${message.id}`, kind: "agent_chat", source: null,
      payload: message.content, created_at: message.created_at, inbound_id: null,
      user_facing: true, show_timestamp: true, userDelivery: message,
    };
    const after = result.findIndex((item) => item.created_at !== null && Date.parse(item.created_at) > Date.parse(message.created_at));
    if (after < 0) result.push(publication);
    else result.splice(after, 0, publication);
  }
  return result;
}
