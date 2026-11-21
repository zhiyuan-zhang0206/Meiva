"use client";

import { useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";

import {
  ImageGateStore,
  TimelineImageScopeContext,
  TimelineItemImageBatchContext,
  type TimelineImageScopeValue,
} from "@/components/content/zoomable-image";

// meiva:note
// Safety ceilings so a dead or stalled image URL can never keep the initial
// timeline hidden or hold a live message's image batch invisible indefinitely.
export const INITIAL_IMAGE_GATE_TIMEOUT_MS = 2500;
export const ITEM_IMAGE_BATCH_TIMEOUT_MS = 3000;

export function useTimelineInitialImageGate(threadKey: string | undefined, placementVisible: boolean) {
  const [gateState, setGateState] = useState<{
    threadKey: string | undefined;
    store: ImageGateStore;
  }>(() => ({
    threadKey,
    store: new ImageGateStore(INITIAL_IMAGE_GATE_TIMEOUT_MS),
  }));
  const [revealState, setRevealState] = useState<{
    threadKey: string | undefined;
    revealed: boolean;
  }>({
    threadKey,
    revealed: false,
  });

  let activeStore = gateState.store;
  let initialRevealed = revealState.threadKey === threadKey ? revealState.revealed : false;
  if (gateState.threadKey !== threadKey) {
    activeStore = new ImageGateStore(INITIAL_IMAGE_GATE_TIMEOUT_MS);
    setGateState({ threadKey, store: activeStore });
    setRevealState({ threadKey, revealed: false });
    initialRevealed = false;
  }

  useEffect(() => () => activeStore.dispose(), [activeStore]);

  const snap = useSyncExternalStore(
    activeStore.subscribe,
    activeStore.getSnapshot,
    activeStore.getSnapshot,
  );
  const imagesSettled = !snap.hasPending || snap.timedOut;
  const visible = placementVisible && (initialRevealed || imagesSettled);

  if (visible && !initialRevealed) {
    setRevealState({ threadKey, revealed: true });
    initialRevealed = true;
  }

  const scopeValue = useMemo<TimelineImageScopeValue>(
    () => ({ initialRevealed, initialGate: activeStore }),
    [initialRevealed, activeStore],
  );

  return { visible, scopeValue };
}

export function TimelineImageScope({
  value,
  children,
}: {
  value: TimelineImageScopeValue;
  children: ReactNode;
}) {
  return (
    <TimelineImageScopeContext.Provider value={value}>
      {children}
    </TimelineImageScopeContext.Provider>
  );
}

export function TimelineItemImageBatch({ children }: { children: ReactNode }) {
  const [store] = useState(() => new ImageGateStore(ITEM_IMAGE_BATCH_TIMEOUT_MS));
  useEffect(() => () => store.dispose(), [store]);
  return (
    <TimelineItemImageBatchContext.Provider value={store}>
      {children}
    </TimelineItemImageBatchContext.Provider>
  );
}
