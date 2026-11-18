import { useCallback, useLayoutEffect, useState } from "react";

import { readTimelineExpansion, rememberTimelineExpansion } from "../model/reading-position";

export function useTimelineExpansion({
  scrollMemoryKey,
  threadKey,
}: {
  scrollMemoryKey: string | undefined;
  threadKey: string | undefined;
}) {
  const [turnOverrides, setTurnOverrides] = useState<ReadonlyMap<string, boolean>>(
    () => readTimelineExpansion(scrollMemoryKey)?.turns ?? new Map(),
  );
  const toggleTurn = useCallback((id: string, currentlyExpanded: boolean) => {
    setTurnOverrides((prev) => {
      const next = new Map(prev);
      next.set(id, !currentlyExpanded);
      return next;
    });
  }, []);

  const [overrides, setOverrides] = useState<ReadonlyMap<string, boolean>>(
    () => readTimelineExpansion(scrollMemoryKey)?.items ?? new Map(),
  );
  const toggleExpanded = useCallback((id: string, currentlyExpanded: boolean) => {
    setOverrides((prev) => {
      const next = new Map(prev);
      next.set(id, !currentlyExpanded);
      return next;
    });
  }, []);

  const [prevThreadKey, setPrevThreadKey] = useState(threadKey);
  if (prevThreadKey !== threadKey) {
    setPrevThreadKey(threadKey);
    setOverrides(new Map());
    setTurnOverrides(new Map());
  }

  useLayoutEffect(() => {
    rememberTimelineExpansion(scrollMemoryKey, {
      items: overrides,
      turns: turnOverrides,
    });
  }, [scrollMemoryKey, overrides, turnOverrides]);

  return {
    turnOverrides,
    toggleTurn,
    overrides,
    toggleExpanded,
  };
}
