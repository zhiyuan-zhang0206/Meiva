"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { BackendTimelineItem, UserMessage } from "../contracts/types";
import { useDocumentVisible } from "../layout/use-document-visible";
import { useStore } from "../state/store";
import { useApi } from "../transport/transport-context";

// meiva:note
// Waits before each retry of a failed advance; once they are spent the advance waits for the next trigger.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
export const SEEN_RETRY_DELAYS_MS: readonly number[] = [1_000, 3_000, 9_000];
// meiva:note
// Older pages loaded, at most, to bring the first unseen delivery into the timeline.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
export const REVEAL_MAX_OLDER_PAGES = 5;

export interface RevealRequest {
  itemId: string;
  token: number;
}

export interface UnseenTarget {
  itemId: string | null;
  complete: boolean;
  any: boolean;
}

export function newestDeliveryId(messages: readonly UserMessage[]): number | null {
  return messages.reduce<number | null>((top, message) => (top === null ? message.id : Math.max(top, message.id)), null);
}

// meiva:note
// The reader's position is one cursor per agent: the newest delivery id they have seen. The first
// unseen content is the loaded item of the lowest delivery id above the cursor. `any` says a
// delivery lies past the cursor; `complete` says the item found belongs to the oldest such delivery.
// Deliveries with no loaded item (older pages, or PREFIX/NAIVE records that match no row) leave
// `complete` false, and the caller decides how far to look for them.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
export function unseenTarget(
  items: readonly BackendTimelineItem[],
  messages: readonly UserMessage[],
  seenId: number,
): UnseenTarget {
  const unseen = messages.filter((message) => message.content !== null && message.id > seenId);
  const oldest = unseen.reduce<number | null>((low, message) => (low === null ? message.id : Math.min(low, message.id)), null);
  let target: BackendTimelineItem | null = null;
  for (const item of items) {
    const delivery = item.userDelivery;
    if (delivery?.content == null || delivery.id <= seenId) continue;
    if (target === null || delivery.id < (target.userDelivery?.id ?? Infinity)) target = item;
  }
  return {
    itemId: target?.item_id ?? null,
    complete: target !== null && target.userDelivery?.id === oldest,
    any: oldest !== null,
  };
}

export interface SeenFeed {
  seenId: number | undefined;
  messages: readonly UserMessage[];
  updatedAt: number;
}

interface Session {
  agentId: number;
  look: number;
  baseline: number;
  entry: number | null;
  decided: boolean;
  olderPages: number;
  waiting: readonly BackendTimelineItem[] | null;
  reveal: RevealRequest | null;
}

// meiva:note
// Opening an agent's conversation in a visible page marks everything delivered as seen, wherever the
// timeline is scrolled; deliveries arriving while it stays open and visible are seen on arrival. A
// hidden page sees nothing, and a return to the foreground starts a new look. A look first takes the
// cursor the feed held on entry (the first feed newer than the one the page held when the look began),
// reveals the first delivery past it, and only then lets the cursor advance to the newest delivery
// the page has. The advance is a monotonic server write; a failed one is retried with growing waits,
// then waits for the next trigger. While the agent is open and visible its row shows no unread dot.
// The look is derived while rendering, from the agent, the page's visibility and the feed.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
export function useSeenPosition({
  agentId,
  feed,
  items,
  timelineLoading,
  hasMoreOlder,
  loadingOlder,
  loadOlder,
}: {
  agentId: number | null;
  feed: SeenFeed;
  items: readonly BackendTimelineItem[];
  timelineLoading: boolean;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  loadOlder: () => void;
}): RevealRequest | null {
  const api = useApi();
  const cache = useQueryClient();
  const visible = useDocumentVisible();
  const setReadingAgentId = useStore((state) => state.setReadingAgentId);
  const open = agentId !== null && visible;
  const { seenId, messages, updatedAt } = feed;
  const [looks, setLooks] = useState(0);
  const [session, setSession] = useState<Session | null>(null);
  const operationKeys = useRef(new Map<string, string>());

  if (open ? session?.agentId !== agentId : session !== null) {
    setLooks(looks + 1);
    setSession(
      open
        ? { agentId, look: looks + 1, baseline: updatedAt, entry: null, decided: false, olderPages: 0, waiting: null, reveal: null }
        : null,
    );
  } else if (session !== null && session.entry === null && seenId !== undefined && updatedAt > session.baseline) {
    setSession({ ...session, entry: seenId });
  } else if (session?.entry != null && !session.decided && !timelineLoading) {
    const target = unseenTarget(items, messages, session.entry);
    if (!target.any) {
      setSession({ ...session, decided: true });
    } else if (!target.complete && hasMoreOlder && session.olderPages < REVEAL_MAX_OLDER_PAGES) {
      if (!loadingOlder && items !== session.waiting) {
        setSession({ ...session, olderPages: session.olderPages + 1, waiting: items });
      }
    } else {
      setSession({
        ...session,
        decided: true,
        reveal: target.itemId === null ? null : { itemId: target.itemId, token: session.look },
      });
    }
  }

  useEffect(() => {
    setReadingAgentId(open ? agentId : null);
    return () => setReadingAgentId(null);
  }, [agentId, open, setReadingAgentId]);

  useEffect(() => {
    if (open) void cache.invalidateQueries({ queryKey: ["user-messages", agentId] });
  }, [agentId, open, cache]);

  const olderPages = session?.olderPages ?? 0;
  useEffect(() => {
    if (olderPages > 0) loadOlder();
  }, [olderPages, loadOlder]);

  const ready = session !== null && session.agentId === agentId && session.entry !== null;
  const newest = newestDeliveryId(messages);
  useEffect(() => {
    if (!ready || seenId === undefined || newest === null || newest <= seenId) return;
    const ended = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const intent = `${agentId}:${newest}`;

    const send = async () => {
      let key = operationKeys.current.get(intent);
      if (key === undefined) {
        key = api.newOperationKey();
        operationKeys.current.set(intent, key);
      }
      try {
        await api.markUserMessagesSeen(agentId, { up_to_id: newest }, key);
      } catch {
        const delay = SEEN_RETRY_DELAYS_MS.at(failures++);
        if (!ended.signal.aborted && delay !== undefined) timer = setTimeout(() => void send(), delay);
        return;
      }
      operationKeys.current.delete(intent);
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["user-messages", agentId] }),
        cache.invalidateQueries({ queryKey: ["agents"] }),
      ]);
    };

    void send();
    return () => {
      ended.abort();
      clearTimeout(timer);
    };
  }, [api, cache, ready, agentId, seenId, newest, updatedAt]);

  return session?.agentId === agentId ? session.reveal : null;
}
