import { useCallback, useLayoutEffect, useState } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";

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

  const [overrides, setOverrides] = useState<ReadonlySet<string>>(
    () => readTimelineExpansion(scrollMemoryKey)?.items ?? new Set(),
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

  const [prevThreadKey, setPrevThreadKey] = useState(threadKey);
  if (prevThreadKey !== threadKey) {
    setPrevThreadKey(threadKey);
    setOverrides(new Set());
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
