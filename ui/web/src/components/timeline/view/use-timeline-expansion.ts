import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { useContentToggle, useContentToggleReset } from "@/lib/layout/content-toggle-store";
import { BAR_HEIGHT_PX } from "@/lib/layout/layout";

import { readTimelineExpansion, rememberTimelineExpansion } from "../model/reading-position";
import { findClosestStuckHeaderId } from "../run-block";

export function useTimelineExpansion({
  scrollMemoryKey,
  threadKey,
  items,
  wrapperRef,
  viewportRef,
}: {
  scrollMemoryKey: string | undefined;
  threadKey: string | undefined;
  items: BackendTimelineItem[];
  wrapperRef: RefObject<HTMLDivElement | null>;
  viewportRef: RefObject<HTMLElement | null>;
}) {
  const stuckRafRef = useRef<number | null>(null);
  const [activeStuckHeaderId, setActiveStuckHeaderId] = useState<string | null>(null);
  const [activeStuckChildId, setActiveStuckChildId] = useState<string | null>(null);

  const updateStuckHeader = useCallback(() => {
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;
    const { topId, childId } = findClosestStuckHeaderId(viewport, BAR_HEIGHT_PX);
    setActiveStuckHeaderId((prev) => (prev === topId ? prev : topId));
    setActiveStuckChildId((prev) => (prev === childId ? prev : childId));
  }, [viewportRef, wrapperRef]);

  const { detailsMode, isLoading: detailsModeLoading } = useContentToggle();
  const effectiveDetailsMode = detailsModeLoading ? "none" : detailsMode;

  const [turnOverrides, setTurnOverrides] = useState<ReadonlyMap<string, boolean>>(
    () => readTimelineExpansion(scrollMemoryKey, effectiveDetailsMode)?.turns ?? new Map(),
  );
  const toggleTurn = useCallback((id: string, currentlyExpanded: boolean) => {
    setTurnOverrides((prev) => {
      const next = new Map(prev);
      next.set(id, !currentlyExpanded);
      return next;
    });
  }, []);

  const [overrides, setOverrides] = useState<ReadonlySet<string>>(
    () => readTimelineExpansion(scrollMemoryKey, effectiveDetailsMode)?.items ?? new Set(),
  );
  const toggleExpanded = useCallback(
    (id: string, _kind: BackendTimelineItem["kind"]) => {
      setOverrides((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
    },
    [],
  );

  const [prevDetailsMode, setPrevDetailsMode] = useState(effectiveDetailsMode);
  if (prevDetailsMode !== effectiveDetailsMode) {
    setPrevDetailsMode(effectiveDetailsMode);
    setOverrides(readTimelineExpansion(scrollMemoryKey, effectiveDetailsMode)?.items ?? new Set());
    setTurnOverrides(readTimelineExpansion(scrollMemoryKey, effectiveDetailsMode)?.turns ?? new Map());
    setActiveStuckHeaderId(null);
    setActiveStuckChildId(null);
  }

  const [prevThreadKey, setPrevThreadKey] = useState(threadKey);
  if (prevThreadKey !== threadKey) {
    setPrevThreadKey(threadKey);
    setOverrides(new Set());
    setTurnOverrides(new Map());
    setActiveStuckHeaderId(null);
    setActiveStuckChildId(null);
  }

  const resetPending = useContentToggleReset((s) => s.resetPending);
  const consumeReset = useContentToggleReset((s) => s.consumeReset);
  const [prevResetPending, setPrevResetPending] = useState(false);
  if (prevResetPending !== resetPending) {
    setPrevResetPending(resetPending);
    if (resetPending) {
      setOverrides(new Set());
      setTurnOverrides(new Map());
      setActiveStuckHeaderId(null);
      setActiveStuckChildId(null);
    }
  }
  useLayoutEffect(() => {
    if (!resetPending) return;
    consumeReset();
  }, [resetPending, consumeReset]);

  useLayoutEffect(() => {
    updateStuckHeader();
  }, [updateStuckHeader, turnOverrides, overrides, effectiveDetailsMode, items]);

  useEffect(() => () => {
    if (stuckRafRef.current !== null) cancelAnimationFrame(stuckRafRef.current);
    stuckRafRef.current = null;
  }, []);

  useLayoutEffect(() => {
    if (!detailsModeLoading) {
      rememberTimelineExpansion(scrollMemoryKey, {
        mode: effectiveDetailsMode,
        items: overrides,
        turns: turnOverrides,
      });
    }
  }, [scrollMemoryKey, effectiveDetailsMode, detailsModeLoading, overrides, turnOverrides]);

  return {
    stuckRafRef,
    activeStuckHeaderId,
    activeStuckChildId,
    updateStuckHeader,
    effectiveDetailsMode,
    turnOverrides,
    toggleTurn,
    overrides,
    toggleExpanded,
  };
}
