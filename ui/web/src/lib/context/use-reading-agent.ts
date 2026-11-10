"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";
import type { UserMessage } from "../contracts/types";
import { useDocumentVisible } from "../layout/use-document-visible";
import { useStore } from "../state/store";
import { useApi } from "../transport/transport-context";

export const READ_DWELL_MS = 400;
export const READ_RETRY_DELAYS_MS: readonly number[] = [1_000, 3_000, 9_000];

export function unreadHighWater(messages: readonly UserMessage[]): number | null {
  if (!messages.some((message) => message.read_at === null)) return null;
  return messages.reduce((highest, message) => Math.max(highest, message.id), 0);
}

// meiva:note
// The reader has read an agent's deliveries when its conversation is open, the document is
// visible and the timeline rests at the latest position. While that holds, the agent is the
// "reading" agent (the sidebar shows no unread dot for it) and, after a short dwell, every
// delivery up to the newest record the page has fetched is marked read in one request.
// The bound is the page's own high-water mark, so a record the page has not received yet is
// never marked. A failed request is retried with growing waits; once those are spent it waits
// for the next trigger: the conditions toggling, a newer record, or the feed refetching.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
export function useReadingAgent({
  agentId,
  atLatest,
  messages,
  fetchedAt,
}: {
  agentId: number | null;
  atLatest: boolean;
  messages: readonly UserMessage[];
  fetchedAt: number;
}): void {
  const api = useApi();
  const cache = useQueryClient();
  const visible = useDocumentVisible();
  const setReadingAgentId = useStore((state) => state.setReadingAgentId);
  const reading = agentId !== null && visible && atLatest;
  const upTo = useMemo(() => unreadHighWater(messages), [messages]);
  const operationKeys = useRef(new Map<string, string>());

  useEffect(() => {
    setReadingAgentId(reading ? agentId : null);
    return () => setReadingAgentId(null);
  }, [agentId, reading, setReadingAgentId]);

  useEffect(() => {
    if (!reading || upTo === null) return;
    const ended = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const intent = `${agentId}:${upTo}`;

    const send = async () => {
      let key = operationKeys.current.get(intent);
      if (key === undefined) {
        key = api.newOperationKey();
        operationKeys.current.set(intent, key);
      }
      try {
        await api.readUserMessages(agentId, { up_to_id: upTo }, key);
      } catch {
        const delay = READ_RETRY_DELAYS_MS.at(failures++);
        if (!ended.signal.aborted && delay !== undefined) timer = setTimeout(() => void send(), delay);
        return;
      }
      operationKeys.current.delete(intent);
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["user-messages", agentId] }),
        cache.invalidateQueries({ queryKey: ["agents"] }),
      ]);
    };

    timer = setTimeout(() => void send(), READ_DWELL_MS);
    return () => {
      ended.abort();
      clearTimeout(timer);
    };
  }, [api, cache, reading, agentId, upTo, fetchedAt]);
}
